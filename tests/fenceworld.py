"""The fence test world (tests/fence/README.md, section 1). Stdlib only.

A real coordinator (core.Coord + server.Server) with a fake clock behind a
fault proxy on loopback, one git repo with one worktree per simulated
worker, a lead and an "other" session in the main checkout, and an oracle.

The oracle is independent of the code under test:
- its own glob matcher (`omatch`, a direct reading of the rules of section 2.1),
- its own fold of the event log (`OState`),
- its own idea of which file a write really touches: the inode that the OS
  resolves for the target (`World.truth`), never the hook's normalization.
"""

import io
import json
import os
import random
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import _coord  # noqa: F401  (sys.path)
import _paths
from wavecoord import fence, gatecheck, guard, hooks
from wavecoord.core import ApiError, Ctx, lead_ctx
from wavecoord.server import Server, Tokens
from wavecoord.globs import globs_overlap  # noqa: F401  (used only by the defect patches)

TOKENS = {"lead-secret": ("lead", "lead"), "agent-secret": ("agent", "host:local"), "gate-secret": ("gate", "gate")}
FAULTS = ("down", "delay", "trickle", "400", "403", "500", "503", "421", "401", "notjson", "wrongschema", "truncated")
TTL_S = 5

TREE = ["a/x.py", "a/y.py", "a/1.md", "a/q.md", "a/b/x.py", "a/b/z.py", "a/b/q.md", "a/b/2.md", "a/bc/w.py",
        "a/bc/x.py", "a/xyz/q.go", "ab/k.py", "ab/x.py", "ab/1.md", "docs/api.md", "docs/1.md", "docs/guide/x.py",
        "x[1].py", "x1.py", ".claude/notes.md", "README.md", "src/m.py", "src/n.py", "src/a/x.py", "lib/x.py",
        "lib/y.md", "b/x.py", "b/c/d.py", "src/authz/x.go", "src/auth.py"]
DIRS = ["a", "a/b", "a/bc", "ab", "docs", ".claude", "src", "lib", "b", "A"]
NAMES = ["x.py", "y.py", "q.md", "1.md", "z.py", "k.py"]


# ----------------------------------------------------------------- the oracle: globs

def omatch(g, p):
    """Does glob g match all of path p? A direct, recursive reading of the rules."""
    if not g:
        return not p
    if g.startswith("**/"):
        rest = g[3:]
        if omatch(rest, p):
            return True
        i = p.find("/")
        while i != -1:
            if omatch(rest, p[i + 1:]):
                return True
            i = p.find("/", i + 1)
        return False
    if g.startswith("**"):
        return any(omatch(g[2:], p[k:]) for k in range(len(p) + 1))
    c = g[0]
    if c == "*":
        k = 0
        while True:
            if omatch(g[1:], p[k:]):
                return True
            if k >= len(p) or p[k] == "/":
                return False
            k += 1
    if c == "?":
        return bool(p) and p[0] != "/" and omatch(g[1:], p[1:])
    return bool(p) and p[0] == c and omatch(g[1:], p[1:])


def ocovers(g, p, fold=False):
    g = g.rstrip("/") or g
    if fold:
        g, p = g.casefold(), p.casefold()
    parts = p.split("/")
    return any(omatch(g, "/".join(parts[:k])) for k in range(1, len(parts) + 1))


# ----------------------------------------------------------------- the oracle: the fold

class OState(object):
    """Leases and reservations after a prefix of the event log (the oracle's own fold)."""

    def __init__(self):
        self.leases = {}
        self.res = {}
        self.seq = 0

    def copy(self):
        o = OState()
        o.leases = {k: dict(v) for k, v in self.leases.items()}
        o.res = {k: dict(v) for k, v in self.res.items()}
        o.seq = self.seq
        return o

    def _end(self, lid):
        l = self.leases.get(lid)
        if l and l["state"] != "released":
            l["state"] = "released"
            for rid in [r for r, v in self.res.items() if v["lease_id"] == lid]:
                del self.res[rid]

    def _add(self, lease, reservations, origin):
        self.leases[lease["id"]] = {"id": lease["id"], "task": lease["task"], "holder": lease["holder"],
                                    "epoch": lease["epoch"], "state": "active"}
        for r in reservations or []:
            self.res[r["id"]] = {"lease_id": r["lease_id"], "glob": r["glob"], "mode": r["mode"],
                                 "origin": r.get("origin") or origin}

    def apply(self, ev, events):
        k, p = ev["kind"], ev["payload"]
        if k == "lease.grant":
            self._add(p["lease"], p.get("reservations"), "claim")
        elif k == "lease.steal":
            self._end(p["old_lease_id"])
            self._add(p["lease"], p.get("reservations"), "claim")
        elif k == "lease.release":
            self._end(p["lease_id"])
        elif k in ("lease.park", "lease.stale", "lease.revive"):
            l = self.leases.get(p["lease_id"])
            if l and l["state"] != "released":
                if k == "lease.park" and l["state"] in ("active", "stale"):
                    l["state"] = "parked"
                elif k == "lease.stale" and l["state"] == "active":
                    l["state"] = "stale"
                elif k == "lease.revive" and l["state"] == "stale":
                    l["state"] = "active"
        elif k == "reservation.add":
            for r in p.get("reservations", []):
                if (self.leases.get(r["lease_id"]) or {}).get("state", "released") != "released":
                    self.res[r["id"]] = {"lease_id": r["lease_id"], "glob": r["glob"], "mode": r["mode"],
                                         "origin": r.get("origin") or "hot"}
        elif k == "reservation.remove":
            self.res.pop(p["reservation_id"], None)
        elif k == "revert" and "to_seq" in p:
            prefix = ofold(events, p["to_seq"])
            new = prefix.copy()
            for lid, l in self.leases.items():
                old = new.leases.get(lid)
                if old is None:
                    new.leases[lid] = dict(l, state="released")
                elif l["state"] == "released":
                    new.leases[lid] = dict(l)
            new.res = {rid: r for rid, r in new.res.items()
                       if new.leases.get(r["lease_id"], {}).get("state", "released") != "released"}
            self.leases, self.res = new.leases, new.res
        self.seq = ev["seq"]

    def live(self, l):
        return l is not None and l["state"] != "released"

    def lease_of(self, agent):
        for l in self.leases.values():
            if l["holder"] == agent and l["state"] != "released":
                return l
        return None

    def task_lease(self, task):
        for l in self.leases.values():
            if l["task"] == task and l["state"] != "released":
                return l
        return None

    def ok(self, agent, task, epoch, path, fold=False):
        """ok(W) of section 2.2 for a worker write (agent, claim (task, epoch), path)."""
        L = self.lease_of(agent)
        if L is None or L["task"] != task:
            return False
        cur = self.task_lease(task)
        if epoch != L["epoch"] or cur is None or cur["epoch"] != epoch:
            return False
        if not any(ocovers(r["glob"], path) for r in self.res.values()
                   if r["lease_id"] == L["id"] and r["origin"] in ("claim", "lead")):
            return False
        return not self.foreign(path, fold, exclude=L["id"])

    def foreign(self, path, fold=False, exclude=None):
        for r in self.res.values():
            if r["lease_id"] == exclude or r["mode"] != "exclusive":
                continue
            if self.live(self.leases.get(r["lease_id"])) and ocovers(r["glob"], path, fold):
                return True
        return False


_FOLD_CACHE = {}  # {"events": the list object, "state": OState, "last": events[state.seq - 1]}


def ofold(events, seq):
    """The oracle state after events[:seq] (cached for one event list, extended incrementally).

    The cache keeps a strong reference to the list it folded and is used only for that same
    object, with the same last folded event. It was keyed by id(events): a pool process runs
    several seeds, a later seed's list can get the id of a freed one, and the oracle then
    folded that seed's log on top of another seed's leases (seed 44 on CI, 0.1.3).
    """
    c = _FOLD_CACHE
    st = None
    if c.get("events") is events and c["state"].seq <= min(seq, len(events)):
        n = c["state"].seq
        if n == 0 or events[n - 1] is c["last"]:
            st = c["state"].copy()
    if st is None:
        st = OState()
    for ev in events[st.seq:seq]:
        st.apply(ev, events)
    if c.get("events") is not events or st.seq >= c["state"].seq:
        c.clear()
        c.update(events=events, state=st.copy(), last=events[st.seq - 1] if st.seq else None)
    return st


# ----------------------------------------------------------------- the fault proxy

class FaultProxy(object):
    """An HTTP proxy in front of the real server that can fail in the ways of the README."""

    def __init__(self, target_port, client_timeout):
        self.target_port = target_port
        self.fault = None
        self.client_timeout = client_timeout
        proxy = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                f = proxy.fault
                if f == "down":
                    self.close_connection = True
                    return
                if f == "delay":
                    time.sleep(proxy.client_timeout * 2 + 0.05)
                if f in ("400", "403", "500", "503", "421", "401"):
                    return self._send(int(f), json.dumps({"now": "2026-10-18T12:00:00Z", "error": {
                        "code": "unavailable", "message": "fault %s" % f}}).encode())
                if f == "notjson":
                    return self._send(200, b"{not json")
                if f == "wrongschema":
                    return self._send(200, json.dumps({"now": "x", "violations": "none"}).encode())
                status, data = self._forward(method, body)
                if f == "truncated":
                    return self._send(status, data[:max(1, len(data) // 2)], length=len(data))
                if f == "trickle":
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    for b in data[:8]:
                        time.sleep(proxy.client_timeout * 0.6)
                        try:
                            self.wfile.write(bytes([b]))
                            self.wfile.flush()
                        except OSError:
                            return
                    self.close_connection = True
                    return
                return self._send(status, data)

            def _forward(self, method, body):
                import http.client
                c = http.client.HTTPConnection("127.0.0.1", proxy.target_port, timeout=30)
                hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length")}
                hdrs["Host"] = "127.0.0.1:%d" % proxy.target_port
                try:
                    c.request(method, self.path, body=body or None, headers=hdrs)
                    r = c.getresponse()
                    return r.status, r.read()
                except OSError:
                    self.close_connection = True
                    return 502, b""
                finally:
                    c.close()

            def _send(self, status, data, length=None):
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(length if length is not None else len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    self.wfile.flush()
                except OSError:
                    pass
                if length is not None:
                    self.close_connection = True

            def do_GET(self):
                self._go("GET")

            def do_POST(self):
                self._go("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ----------------------------------------------------------------- git helpers

GIT_ENV = dict(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.com", GIT_CONFIG_NOSYSTEM="1")


def git(cwd, *args, check=True):
    env = dict(os.environ, **GIT_ENV)
    p = subprocess.run(["git", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false",
                        "-c", "core.hooksPath=/dev/null"] + list(args), cwd=cwd, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if check and p.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args), p.stderr))
    return p.stdout


def tree_of(cwd, rev):
    """{path: blob sha} of a commit (ls-tree), the oracle's view of a tree."""
    out = git(cwd, "ls-tree", "-r", "-z", rev)
    res = {}
    for ent in out.split("\0"):
        if ent:
            meta, path = ent.split("\t", 1)
            res[path] = meta.split()[2]
    return res


def index_of(cwd):
    out = git(cwd, "ls-files", "-s", "-z")
    res = {}
    for ent in out.split("\0"):
        if ent:
            meta, path = ent.split("\t", 1)
            res[path] = meta.split()[1]
    return res


def changed(a, b):
    return sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))


def synthetic_report(task):
    return {"status": "done", "summary": "sim attempt of %s" % task, "commits": [], "files": [], "checks_run": [],
            "acceptance_met": True, "questions": [], "proposals": [], "plan_feedback": "", "risks_hit": []}


# ----------------------------------------------------------------- the world

class Worker(object):
    def __init__(self, i, wt):
        self.i = i
        self.agent = "w%d" % i
        self.wt = wt
        self.k = 0
        self.task = None
        self.attempt = 1
        self.epoch = None
        self.lease_id = None
        self.globs = []
        self.branch = None
        self.pushed = None
        self.committed = False
        self.env_offline = False
        self.cfg_trick = False


class World(object):
    def __init__(self, seed, n_workers=None, ci=None, enforce=True):
        self.rng = random.Random(seed)
        self.seed = seed
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="wavecoord-fence-"))
        self.clock = _coord.Clock()
        self.failures = []
        self.log = []
        self.ci_forced = ci
        self.enforce = enforce
        self.journal_sent = set()
        self.forced = set()
        self.journaled = set()
        self.timeout = 0.5
        self._setup(n_workers or self.rng.randint(3, 6))

    # -- setup --
    def _setup(self, n):
        r = self.root
        self.remote = os.path.join(r, "remote.git")
        self.repo = os.path.join(r, "repo")
        self.home = os.path.join(r, "home")
        self.chome = os.path.join(r, "coordhome")
        self.outside = os.path.join(r, "outside")
        for d in (self.home, self.chome, self.outside):
            os.makedirs(d)
        git(r, "init", "-q", "--bare", self.remote)
        git(r, "init", "-q", self.repo)
        git(self.repo, "remote", "add", "origin", self.remote)
        for p in TREE:
            fp = os.path.join(self.repo, p)
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w") as f:
                f.write(p + "\n")
        os.symlink("../a", os.path.join(self.repo, "docs", "link"))
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write("build/\n*.log\n")
        mode = "enforce" if self.enforce else "advisory"
        with open(os.path.join(self.repo, ".claude", "wavecoord.json"), "w") as f:
            json.dump({"project": "sim", "fence": {"edit": mode, "commit": mode, "push": mode, "merge": mode,
                                                   "lead": "ask"}}, f)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "tree")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "fetch", "-q", "origin")
        self.modes = {"edit": mode, "commit": mode, "push": mode, "merge": mode, "lead": "ask"}
        self.tokfiles = {}
        for t, (role, _p) in TOKENS.items():
            fp = os.path.join(r, role + ".token")
            with open(fp, "w") as f:
                f.write(t + "\n")
            self.tokfiles[role] = fp
        self.start_server()
        self.proxy = FaultProxy(self.server.port, self.timeout)
        self.workers = []
        for i in range(n):
            wt = os.path.join(r, "wt", "w%d" % i)
            git(self.repo, "worktree", "add", "-q", "-b", "sim/w%d-0" % i, wt, "origin/main")
            w = Worker(i, wt)
            self.workers.append(w)
            self.dispatch(w)
        self.lead_sid = "lead-session"
        self.other_sid = "other-session"
        self.thieves = 0
        self.dirty = True
        self._index = None

    def start_server(self):
        from wavecoord.core import Coord
        self.coord = Coord(self.repo, home=self.chome, clock=self.clock, push=False, threads=False,
                           commit_window_ms=0).open()
        self.coord.check_trace = []
        data = {"version": 1, "tokens": {__import__("hashlib").sha256(t.encode()).hexdigest():
                                         {"principal": p, "role": ro, "created": "2026-10-18T12:00:00Z"}
                                         for t, (ro, p) in TOKENS.items()}}
        self.server = Server(self.coord, port=0, tokens=Tokens(data=data)).start()

    def restart_server(self):
        self.server.stop()
        self.coord.close()
        self.start_server()
        self.proxy.target_port = self.server.port

    def close(self):
        try:
            self.proxy.close()
            self.server.stop()
            self.coord.close()
        finally:
            shutil.rmtree(self.root, ignore_errors=True)

    # -- envs --
    def base_env(self, role):
        e = {k: v for k, v in os.environ.items()
             if not k.startswith(("WAVECOORD_", "CLAUDE_PLUGIN_OPTION_", "CLAUDE_CODE_", "GIT_", "CLAUDECODE"))}
        e.update(WAVECOORD_URL=self.proxy.url, WAVECOORD_TOKEN_FILE=self.tokfiles[role],
                 WAVECOORD_HOME=self.home, WAVECOORD_CLIENT_TIMEOUT_S=str(self.timeout),
                 WAVECOORD_FENCE_BUDGET_S="1.5", WAVECOORD_HOST="local", GIT_CONFIG_NOSYSTEM="1",
                 WAVECOORD_HOOK_MIN_INTERVAL_S="3600")
        e.update(GIT_ENV)
        return e

    def worker_env(self, w):
        e = self.base_env("agent")
        e.update(WAVECOORD_AGENT_ID=w.agent, WAVECOORD_TASK=w.task, WAVECOORD_ATTEMPT=str(w.attempt),
                 WAVECOORD_WORKTREE=w.wt, WAVECOORD_FENCE=json.dumps(self.modes),
                 WAVECOORD_STATE_DIR=os.path.join(self.root, "state", w.agent))
        if w.epoch:
            e["WAVECOORD_LEASE_EPOCH"] = str(w.epoch)
        if w.env_offline:
            e["WAVECOORD_FENCE_OFFLINE"] = "1"
        return e

    def commit_env(self, w):
        """The env of a `git commit` that a worker runs with Bash: not a reliable identity."""
        e = self.base_env("agent")
        e["WAVECOORD_STATE_DIR"] = os.path.join(self.root, "state", w.agent)
        return e

    # -- leases --
    def random_globs(self):
        out = []
        for _ in range(self.rng.randint(1, 3)):
            form = self.rng.randrange(6)
            d = self.rng.choice(DIRS)
            if form == 0:
                g = d + "/**"
            elif form == 1:
                g = d + "/*.py"
            elif form == 2:
                g = self.rng.choice(TREE)
            elif form == 3:
                g = d + "/?.md"
            elif form == 4:
                g = "**/" + self.rng.choice(NAMES)
            else:
                g = d
            excl = self.rng.random() < (0.2 if form == 4 else 0.5)
            out.append({"glob": g, "mode": "exclusive" if excl else "shared"})
        return out

    def assign(self, w, items):
        """A new task for worker w with exactly these reservations (forced: for the table tests)."""
        self.dispatch(w, items)

    def dispatch(self, w, fixed=None):
        """A new task and lease for worker w, and a fresh branch from origin/main."""
        cur = self.coord.state.agent_lease(w.agent)
        if cur is not None:
            self.coord.release(lead_ctx(), cur["id"])
        w.k += 1
        w.task = "t%d-%d" % (w.i, w.k)
        w.attempt = 1
        for _ in range(6):
            items = self.random_globs() if fixed is None else [{"glob": g, "mode": m} for g, m in fixed]
            body = {"task": w.task, "attempt": 1, "holder": w.agent, "host": "local", "reservations": items}
            if fixed is not None:
                body["force"] = True
            try:
                out = self.coord.claim(lead_ctx(), body)
                break
            except ApiError as e:
                if e.code == "conflict" and self.rng.random() < 0.3:
                    body["force"] = True
                    out = self.coord.claim(lead_ctx(), body)
                    self.forced.add(out["lease"]["id"])
                    break
                if e.code in ("conflict", "clock"):
                    if e.code == "clock":
                        self.clock.advance(15)
                    continue
                raise
        else:
            body = {"task": w.task, "attempt": 1, "holder": w.agent, "host": "local",
                    "reservations": [{"glob": "nowhere-%s/**" % w.task, "mode": "shared"}], "force": True}
            out = self.coord.claim(lead_ctx(), body)
            self.forced.add(out["lease"]["id"])
        w.epoch, w.lease_id = out["lease"]["epoch"], out["lease"]["id"]
        if body.get("force"):
            self.forced.add(w.lease_id)
        w.globs = [r["glob"] for r in out["reservations"]]
        w.branch = "sim/%s" % w.task
        w.pushed = None
        git(w.wt, "checkout", "-q", "-f", "-B", w.branch, "origin/main")
        git(w.wt, "clean", "-fdq")
        guard.install(w.wt, out=io.StringIO(), env=self.worker_env(w))
        self.dirty = True

    # -- truth (the oracle's own path resolution) --
    def fold_case_path(self, p):
        """Case-insensitive lookup of each existing component (only when the FS probe is forced on)."""
        parts = p.split(os.sep)
        cur = os.sep
        for i, name in enumerate(parts[1:], 1):
            nxt = os.path.join(cur, name)
            if not os.path.lexists(nxt) and os.path.isdir(cur):
                m = [n for n in os.listdir(cur) if n.lower() == name.lower()]
                if m:
                    nxt = os.path.join(cur, m[0])
            cur = nxt
        return cur

    def index(self):
        if self._index is not None and not self.dirty:
            return self._index
        files, dirs = {}, {}
        locs = [("main", self.repo)] + [("w%d" % w.i, w.wt) for w in self.workers]
        for loc, top in locs:
            for d, dn, fn in os.walk(top):
                rel = os.path.relpath(d, top).replace(os.sep, "/")
                rel = "" if rel == "." else rel
                if ".git" in rel.split("/"):
                    dn[:] = []
                    continue
                st = os.lstat(d)
                dirs[(st.st_dev, st.st_ino)] = (loc, rel)
                dn[:] = [x for x in dn if x != ".git"]
                for f in fn:
                    if f == ".git":
                        continue
                    try:
                        st = os.lstat(os.path.join(d, f))
                    except OSError:
                        continue
                    files[(st.st_dev, st.st_ino)] = (loc, (rel + "/" + f) if rel else f)
        self._index = (files, dirs)
        self.dirty = False
        return self._index

    def truth(self, target, cwd):
        """-> (loc, rel): loc is main, w<i>, git, home, outside. Resolved by the OS, not by the hook."""
        p = target if os.path.isabs(target) else os.path.join(cwd, target)
        if self.ci_forced:
            p = self.fold_case_path(os.path.abspath(p)) if ".." not in p.split(os.sep) else p
        real = os.path.realpath(p)
        if "/.git/" in (real + "/")[len(self.root):]:
            return "git", None
        if real == self.home or real.startswith(self.home + os.sep):
            return "home", None
        files, dirs = self.index()
        try:
            st = os.stat(p)
            hit = files.get((st.st_dev, st.st_ino)) or dirs.get((st.st_dev, st.st_ino))
            if hit:
                return hit
            return "outside", None
        except OSError:
            pass
        # a new file: the nearest existing directory above it, then the rest as given
        rest, d = [os.path.basename(p)], os.path.dirname(p)
        while True:
            try:
                st = os.stat(d)
                break
            except OSError:
                if os.path.dirname(d) == d:
                    return "outside", None
                rest.insert(0, os.path.basename(d))
                d = os.path.dirname(d)
        hit = dirs.get((st.st_dev, st.st_ino))
        if not hit:
            return "outside", None
        if ".." in rest:
            return "outside", None  # the OS would not create this path
        leaf = "/".join(rest)
        return hit[0], (hit[1] + "/" + leaf) if hit[1] else leaf

    @staticmethod
    def ignored(rel):
        return rel.startswith("build/") or rel.endswith(".log")

    def ostate(self, seq):
        return ofold(self.coord.log.events, seq)

    def fold(self):
        return bool(self.ci_forced) or fence.case_insensitive(self.repo)

    def fail(self, what):
        self.failures.append("seed %s op %d: %s" % (self.seed, len(self.log), what))
