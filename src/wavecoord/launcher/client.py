"""Client side of the launcher: probe, placement, launch, and the attempt commands."""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor

from wavecoord import zipapp

from . import coord as coordmod
from .common import (FINAL, TASK_RE, LauncherError, Unreachable, ensure_dir, git_run, now, rand4, read_json,
                     wc_home, write_json)
from .transport import Host

POLL_S = 10.0


def warn(msg):
    sys.stderr.write("wavecoord: %s\n" % msg)


def parallel(fn, items, workers=8):
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
        return list(ex.map(fn, items))


# ---- placement ---------------------------------------------------------------------------------

def why_ineligible(p):
    """Why a probed host cannot take a worker now, or None. 'full' is the only transient reason."""
    if not p.get("ok"):
        return "unreachable: %s" % (p.get("error") or "?")[-200:]
    if not p.get("repo_ok"):
        return "repo_path is not a git checkout"
    c = p.get("claude") or {}
    if not c.get("installed"):
        return "claude is not installed"
    if c.get("auth") == "no":
        return "claude is not logged in (run `claude auth login` on the host)"
    if p.get("free", 0) <= 0:
        return "full (%s/%s slots)" % (p.get("active"), p.get("slots"))
    return None


def place(probes):
    """Eligible host names, best first: most free slots, then most free memory, then name.

    -> (ordered names, {name: why not})"""
    ok, why = [], {}
    for name, p in probes.items():
        r = why_ineligible(p)
        if r:
            why[name] = r
        else:
            ok.append(name)
    ok.sort(key=lambda n: (-probes[n].get("free", 0), -(probes[n].get("mem_avail_gib") or 0), n))
    return ok, why


# ---- the client --------------------------------------------------------------------------------

class Launcher(object):
    def __init__(self, cfg, coord_url=None, plugin_dir=None):
        self.cfg = cfg
        self.hosts = {n: Host(h) for n, h in cfg.hosts.items()}
        self.coord_url = coord_url if coord_url is not None else cfg.coord_url()
        self.plugin_dir = plugin_dir or zipapp.default_plugin_dir()
        self._plugin = None
        self.project = re.sub(r"[^A-Za-z0-9._-]+", "-", cfg.project or "default").strip("-")[:64] or "default"

    # -- plugin copy
    def plugin(self):
        """(hash, tar bytes) of the pinned plugin copy, or (None, None) without a plugin dir."""
        if self._plugin is None:
            if self.plugin_dir and os.path.isdir(self.plugin_dir):
                data = zipapp.plugin_tar(self.plugin_dir)
                self._plugin = (zipapp.content_hash(data), data)
            else:
                self._plugin = (None, None)
        return self._plugin

    def host(self, name):
        if name not in self.hosts:
            raise LauncherError("unknown host %r (hosts: %s)" % (name, ", ".join(sorted(self.hosts))))
        return self.hosts[name]

    def base_payload(self):
        return {"project": self.project, "coord_url": self.coord_url, "plugin_hash": self.plugin()[0],
                "log_branch": self.cfg.log_branch}

    def probe(self, names=None):
        names = names or sorted(self.hosts)

        def one(n):
            return n, self.hosts[n].safe_call("probe", self.base_payload(), timeout=120)
        return dict(parallel(one, names))

    # -- attempt numbers
    def existing_attempts(self, task):
        """Attempt numbers of `task` seen as branches on origin, local branches or host state."""
        prefix = "%s/%s-" % (self.cfg.branch_prefix, task)
        nums = set()
        rc, out, _ = git_run(["ls-remote", "--heads", "origin", "refs/heads/%s*" % prefix], self.cfg.root, timeout=60)
        rc2, out2, _ = git_run(["for-each-ref", "--format=%(refname)", "refs/heads/" + prefix.rstrip("-")],
                               self.cfg.root, timeout=60)
        for line in (out if rc == 0 else "").splitlines() + (out2 if rc2 == 0 else "").splitlines():
            m = re.search(r"refs/heads/%s(\d+)$" % re.escape(prefix), line)
            if m:
                nums.add(int(m.group(1)))
        idx = os.path.join(self.index_dir())
        for n in os.listdir(idx):
            m = re.match(r"^%s-(\d+)\.json$" % re.escape(task), n)
            if m:
                nums.add(int(m.group(1)))
        return nums

    def index_dir(self):
        return ensure_dir(os.path.join(wc_home(), "client", self.project))

    def index_put(self, rec):
        write_json(os.path.join(self.index_dir(), rec["id"] + ".json"), rec)

    def index_get(self, aid):
        return read_json(os.path.join(self.index_dir(), aid + ".json"))

    # -- lease
    def claim(self, task, attempt, agent, host, reservations):
        """Claim the task's lease. -> (lease dict or None, note)."""
        if not self.coord_url:
            return None, "no coordinator configured"
        c = coordmod.Coord(self.coord_url, agent=agent)
        body = {"task": task, "attempt": attempt, "holder": agent, "host": host}
        if reservations:
            body["reservations"] = reservations
        try:
            status, out = c.request("POST", "/api/leases", body)
        except Unreachable as e:
            return None, str(e)
        if status in (200, 201) and isinstance(out.get("lease"), dict):
            for cf in out.get("conflicts") or []:
                warn("reservation conflict: %s overlaps %s of task %s" % (cf.get("glob"), cf.get("other_glob"), cf.get("task")))
            return out["lease"], "claimed"
        err = (out.get("error") or {})
        raise LauncherError("lease claim for %s refused (%s %s): %s" % (task, status, err.get("code"), err.get("message")))

    # -- uploads
    def ensure_plugin(self, h, probe):
        ph, data = self.plugin()
        if ph and not probe.get("plugin_present"):
            out = h.call_raw("plugin-install", data, args=(ph,), timeout=300)
            if not out.get("ok"):
                raise LauncherError("%s: plugin upload failed: %s" % (h.name, out.get("error")))

    def ensure_token(self, h, probe):
        """Copy this host's agent token (~/.wavecoord/hosts/<name>.token) when the host lacks it."""
        if h.local:
            return
        src = os.path.join(wc_home(), "hosts", h.name + ".token")
        try:
            with open(src, "rb") as f:
                tok = f.read().strip()
        except OSError:
            return
        import hashlib
        if hashlib.sha256(tok + b"\n").hexdigest() == probe.get("token_sha"):
            return
        out = h.call_raw("token-install", tok, timeout=60)
        if not out.get("ok"):
            warn("%s: token copy failed: %s" % (h.name, out.get("error")))

    def ensure_tunnel(self, h, probe):
        """A loopback coordinator is reached from a remote host through `ssh -R` (security.md)."""
        if h.local or not self.coord_url or probe.get("coord_reachable"):
            return
        u = urllib.parse.urlparse(self.coord_url)
        if u.hostname not in ("127.0.0.1", "localhost", "::1"):
            return
        port = u.port or 80
        argv = h.ssh_argv()[:-1] + ["-f", "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ControlMaster=no",
                                    "-o", "ControlPath=none", "-R", "%d:127.0.0.1:%d" % (port, port), h.cfg["ssh"]]
        try:
            subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            warn("%s: coordinator tunnel failed: %s (reports are spooled on the host)" % (h.name, e))

    # -- launch
    def launch(self, opts):
        task = opts["task"]
        if not TASK_RE.match(task or ""):
            raise LauncherError("bad task id %r" % task)
        names = [opts["host"]] if opts.get("host") else sorted(self.hosts)
        for n in names:
            self.host(n)
        announced = None
        while True:
            probes = self.probe(names)
            order, why = place(probes)
            if order:
                break
            transient = any(r.startswith("full") for r in why.values())
            if not (opts.get("wait") and transient):
                raise LauncherError("no host can take %s: %s" % (task, "; ".join("%s: %s" % kv for kv in sorted(why.items()))))
            summary = "; ".join("%s: %s" % kv for kv in sorted(why.items()))
            if summary != announced:
                warn("waiting for a free slot (%s)" % summary)
                announced = summary
            time.sleep(float(os.environ.get("WAVECOORD_POLL_S", POLL_S)))
        name = order[0]
        h, probe = self.hosts[name], probes[name]

        existing = self.existing_attempts(task)
        if opts.get("attempt"):
            attempt = int(opts["attempt"])
        elif opts.get("continue"):
            if not existing:
                raise LauncherError("--continue: no earlier attempt of %s found" % task)
            attempt = max(existing)
        else:
            attempt = (max(existing) + 1) if existing else 1
        aid = "%s-%d" % (task, attempt)
        rec = self.index_get(aid) or {}
        if opts.get("continue") and rec.get("host") and not opts.get("host") and rec["host"] in self.hosts:
            # continue where the earlier run is, so its worktree and session are reused
            if not why_ineligible(probes.get(rec["host"]) or self.probe([rec["host"]])[rec["host"]]):
                name, h = rec["host"], self.hosts[rec["host"]]
                probe = probes.get(name) or self.probe([name])[name]
        branch = "%s/%s" % (self.cfg.branch_prefix, aid)
        agent = "worker-%s-%s" % (aid, rand4())
        lease_id, epoch = opts.get("lease_id"), opts.get("lease_epoch")
        lease_note = "given" if epoch else None
        if not epoch and not opts.get("no_claim"):
            lease, lease_note = self.claim(task, attempt, agent, name, opts.get("reservations"))
            if lease:
                lease_id, epoch = lease["id"], lease["epoch"]
            else:
                warn("running %s without a lease: %s" % (aid, lease_note))
        spec = {k: opts.get(k) for k in ("model", "effort", "max_turns", "max_budget_usd", "fallback_model",
                                         "timeout_min", "claude_flags")}
        spec.update({"task": task, "attempt": attempt, "attempt_id": aid, "branch": branch, "agent": agent,
                     "session_id": str(uuid.uuid4()), "prompt": opts["prompt"], "continue": bool(opts.get("continue")),
                     "lease_id": lease_id, "lease_epoch": epoch})
        self.ensure_plugin(h, probe)
        self.ensure_token(h, probe)
        self.ensure_tunnel(h, probe)
        payload = dict(self.base_payload(), spec=spec, pr_cmd=self.cfg.pr_cmd)
        out = h.call("launch", payload, timeout=900)
        if out.get("need_plugin"):
            self.ensure_plugin(h, {})
            out = h.call("launch", payload, timeout=900)
        if not out.get("ok"):
            raise LauncherError(out.get("error") or "launch failed")
        rec = {"id": aid, "host": name, "task": task, "attempt": attempt, "branch": branch, "agent": agent,
               "session_id": spec["session_id"], "lease_id": lease_id, "lease_epoch": epoch, "lease_note": lease_note,
               "worktree": out.get("worktree"), "created": now()}
        self.index_put(rec)
        rec["state_dir"] = out.get("state_dir")
        return rec

    # -- attempts
    def locate(self, aid):
        rec = self.index_get(aid)
        if rec and rec.get("host") in self.hosts:
            return rec["host"]
        for n, rows in self.ls().items():
            for r in rows:
                if r["id"] == aid:
                    return n
        raise LauncherError("no attempt %r" % aid)

    def ls(self, names=None):
        names = names or sorted(self.hosts)

        def one(n):
            out = self.hosts[n].safe_call("ls", {"project": self.project}, timeout=60)
            if not out.get("ok"):
                warn("%s: %s" % (n, out.get("error")))
                return n, []
            return n, out["attempts"]
        return dict(parallel(one, names))

    def result(self, aid):
        n = self.locate(aid)
        return self.hosts[n].call("result", {"project": self.project}, args=(aid,), timeout=60)

    def stop(self, aid):
        n = self.locate(aid)
        return self.hosts[n].call("stop", {"project": self.project}, args=(aid,), timeout=180)

    def logs(self, aid, follow=False, tail=None, raw=False):
        n = self.locate(aid)
        return self.hosts[n].stream("logs", {"project": self.project, "follow": follow, "tail": tail, "raw": raw},
                                    args=(aid,))

    def gc(self, dry_run=False, names=None):
        names = names or sorted(self.hosts)

        def one(n):
            return n, self.hosts[n].safe_call("gc", {"project": self.project, "dry_run": dry_run}, timeout=600)
        return dict(parallel(one, names))

    def wait(self, aid, timeout=None, poll=None):
        poll = poll or float(os.environ.get("WAVECOORD_POLL_S", "2"))
        end = now() + timeout if timeout else None
        while True:
            r = self.result(aid)
            if r.get("ok") and r.get("state") in FINAL:
                return r
            if end and now() > end:
                return r
            time.sleep(poll)


def prompt_from_spec(spec):
    s = spec.get("sections") or {}
    parts = ["# Task %s%s" % (spec["id"], (": " + spec["title"]) if spec.get("title") else "")]
    if s.get("context"):
        parts += ["## Context", s["context"].strip()]
    if s.get("acceptance"):
        parts += ["## Acceptance", "\n".join("- `%s`" % a for a in s["acceptance"])]
    if spec.get("paths"):
        parts += ["## Paths you may change", "\n".join("- `%s`" % p for p in spec["paths"])]
    parts += ["## Prompt", (s.get("prompt") or "").strip()]
    return "\n\n".join(parts) + "\n"


def load_spec(path):
    from wavecoord import jsonschema_lite
    spec = read_json(path)
    if not isinstance(spec, dict):
        raise LauncherError("cannot read task spec %s" % path)
    schema = json.loads(zipapp.load_schema_bytes("task-spec.schema.json").decode("utf-8"))
    errs = jsonschema_lite.validate(spec, schema)
    if errs:
        raise LauncherError("invalid task spec %s: %s" % (path, "; ".join(errs[:5])))
    return spec
