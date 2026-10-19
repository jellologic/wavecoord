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
from .providers import ANTHROPIC, cost_basis
from .common import (FINAL, TASK_RE, LauncherError, Unreachable, ensure_dir, git_run, now, rand4, read_json,
                     run_cmd, wc_home, write_json)
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

def attempt_auth(p, provider=ANTHROPIC):
    """How a probed host authenticates a worker of `provider`. -> (label, None) or (None, why not).

    A profile other than built-in `anthropic` authenticates with its env file on the
    host (present, mode 600), so the label is `provider:<name>` and no claude login
    is needed. Built-in `anthropic` needs the claude login (`logged-in`, `env`, `keychain?`)."""
    c = p.get("claude") or {}
    prov = (p.get("providers") or {}).get(provider)
    if provider != ANTHROPIC or (prov and not prov.get("builtin")):
        if not prov:
            return None, "provider %s is not on this host" % provider
        if not prov.get("present"):
            return None, "provider %s: %s" % (provider, prov.get("why"))
        return "provider:%s" % provider, None
    if c.get("auth") == "no":
        return None, "claude is not logged in (run `claude auth login` on the host)"
    return c.get("auth") or "?", None


def _base_why(p):
    if not p.get("ok"):
        return "unreachable: %s" % (p.get("error") or "?")[-200:]
    if not p.get("repo_ok"):
        return "repo_path is not a git checkout"
    if not (p.get("claude") or {}).get("installed"):
        return "claude is not installed"
    return None


def host_provider(p, chain):
    """The first provider of `chain` that a probed host can authenticate. -> (provider, None) or (None, why not).

    It does not look at free slots."""
    why = _base_why(p)
    if why:
        return None, why
    whys = []
    for prov in chain:
        label, w = attempt_auth(p, prov)
        if label:
            return prov, None
        whys.append(w)
    return None, "; ".join(whys) or "no provider"


def why_ineligible(p, provider=ANTHROPIC, fallback=()):
    """Why a probed host cannot take a worker of `provider` (or of a `fallback` provider) now, or None.

    'full' is the only transient reason."""
    _, why = host_provider(p, [provider] + [x for x in fallback or () if x != provider])
    if why:
        return why
    if p.get("free", 0) <= 0:
        return "full (%s/%s slots)" % (p.get("active"), p.get("slots"))
    return None


def placement(probes, chain, allowed=None):
    """Eligible hosts, best first, each with the provider it runs the attempt with.

    A host is eligible when it can authenticate one provider of `chain` (the attempt's
    provider, then its fallbacks) and has a free slot. Order: the earlier provider of
    `chain` first (the primary provider wins whenever an eligible host can run it), then
    most free slots, then most free memory, then the name. `allowed`
    ({name: provider names}, the host's `providers` in the config) limits `chain` per host.
    -> ([(name, provider)], {name: why not})"""
    chain = [c for i, c in enumerate(chain) if c not in chain[:i]]
    ok, why = [], {}
    for name, p in probes.items():
        mine = [c for c in chain if allowed is None or c in allowed.get(name, ())]
        prov, r = host_provider(p, mine) if mine else (None, "provider %s is not on this host" % ", ".join(chain))
        if not r and p.get("free", 0) <= 0:
            r = "full (%s/%s slots)" % (p.get("active"), p.get("slots"))
        if r:
            why[name] = r
        else:
            ok.append((name, prov))
    ok.sort(key=lambda np: (chain.index(np[1]), -probes[np[0]].get("free", 0),
                            -(probes[np[0]].get("mem_avail_gib") or 0), np[0]))
    return ok, why


def rest_of(fallback, provider, chain):
    """The fallbacks of an attempt placed with `provider`: all of them for the primary, else those after it."""
    if provider == chain[0]:
        return list(fallback)
    return list(fallback[fallback.index(provider) + 1:])


def place(probes, provider=ANTHROPIC, fallback=()):
    """Eligible host names, best first: most free slots, then most free memory, then name.

    -> (ordered names, {name: why not})"""
    ok, why = placement(probes, [provider] + list(fallback or ()))
    return [n for n, _ in ok], why


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

    def host_profiles(self, name):
        """{provider: profile or None} that host `name` has (paths and models only; no secrets)."""
        return {p: self.cfg.profile(p) for p in self.cfg.host_providers(name)}

    def allowed(self, names):
        """{host: the provider names its config lists} for placement."""
        return {n: set(self.cfg.host_providers(n)) for n in names}

    def probe(self, names=None):
        names = names or sorted(self.hosts)

        def one(n):
            payload = dict(self.base_payload(), providers=self.host_profiles(n))
            return n, self.hosts[n].safe_call("probe", payload, timeout=120)
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
    def claim(self, task, attempt, agent, host, reservations, resume=False):
        """Claim the task's lease. `resume`: a --continue run takes over the attempt's parked lease.
        -> (lease dict or None, note)."""
        if not self.coord_url:
            return None, "no coordinator configured"
        c = coordmod.Coord(self.coord_url, agent=agent)
        body = {"task": task, "attempt": attempt, "holder": agent, "host": host}
        if reservations:
            body["reservations"] = reservations
        if resume:
            body["resume"] = True
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

    def local_token_file(self, name):
        """A local host's agent token file (~/.wavecoord/hosts/<name>.token), or None.

        A local host shares ~/.wavecoord/token with the lead's own CLI, so its workers
        get their agent token by path instead of a copy (security.md, "Tokens")."""
        p = os.path.join(wc_home(), "hosts", name + ".token")
        return p if os.path.isfile(p) else None

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

    # -- placement
    def select_host(self, opts):
        """Pick the host and the provider of one attempt (docs/launcher.md, "Hosts and placement").

        -> {host, provider, chain, fallback (the fallbacks after `provider`), all_fallback, probes,
            skipped: {host: why not}}"""
        provider = opts.get("provider") or self.cfg.default_provider
        fallback = list(opts.get("provider_fallback") or [])
        for p in [provider] + fallback:
            if not self.cfg.known_provider(p):
                raise LauncherError("unknown provider %r (configured: %s)"
                                    % (p, ", ".join(sorted(set(self.cfg.providers) | {ANTHROPIC}))))
        # the providers this attempt may be placed with: its provider, then its fallbacks
        # (less the ones the lead backed off); the spec keeps the whole fallback list
        avoid = set(opts.get("placement_avoid") or ())
        chain = [provider] + [p for p in fallback if p != provider and p not in avoid]
        if opts.get("host"):
            names = [opts["host"]]
        else:
            # a host with `dispatch: false` takes an attempt only by an explicit --host
            names = [n for n in sorted(opts.get("hosts") or self.hosts) if self.cfg.auto_dispatch(n)]
            if not names:
                raise LauncherError("no host for automatic placement (every host has dispatch: false; use --host)")
        for n in names:
            self.host(n)
        names = [n for n in names if set(chain) & set(self.cfg.host_providers(n))]
        if not names:
            raise LauncherError("no host has provider %s" % ", ".join(chain))
        announced = None
        while True:
            probes = self.probe(names)
            order, why = placement(probes, chain, self.allowed(names))
            if order:
                break
            transient = any(r.startswith("full") for r in why.values())
            if not (opts.get("wait") and transient):
                raise LauncherError("no host can take %s: %s" % (opts["task"], "; ".join("%s: %s" % kv for kv in sorted(why.items()))))
            summary = "; ".join("%s: %s" % kv for kv in sorted(why.items()))
            if summary != announced:
                warn("waiting for a free slot (%s)" % summary)
                announced = summary
            time.sleep(float(os.environ.get("WAVECOORD_POLL_S", POLL_S)))
        name, provider = order[0]
        return {"host": name, "provider": provider, "chain": chain, "fallback": rest_of(fallback, provider, chain),
                "all_fallback": fallback, "probes": probes, "skipped": dict(why)}

    # -- launch
    def launch(self, opts):
        task = opts["task"]
        if not TASK_RE.match(task or ""):
            raise LauncherError("bad task id %r" % task)
        sel = self.select_host(opts)
        name, provider, chain, fallback, probes = sel["host"], sel["provider"], sel["chain"], sel["all_fallback"], sel["probes"]
        h, probe, skipped = self.hosts[name], probes[name], sel["skipped"]

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
            if set(chain) & set(self.cfg.host_providers(rec["host"])):
                p_prev = probes.get(rec["host"]) or self.probe([rec["host"]])[rec["host"]]
                prev = placement({rec["host"]: p_prev}, chain, self.allowed([rec["host"]]))[0]
                if prev:
                    name, provider = prev[0]
                    h, probe = self.hosts[name], p_prev
        fallback = rest_of(fallback, provider, chain)
        branch = "%s/%s" % (self.cfg.branch_prefix, aid)
        agent = "worker-%s-%s" % (aid, rand4())
        lease_id, epoch = opts.get("lease_id"), opts.get("lease_epoch")
        lease_note = "given" if epoch else None
        reservations = opts.get("reservations")
        if not reservations and opts.get("continue"):
            # a --continue run without --spec: the paths of the earlier run of the attempt
            reservations = rec.get("reservations") or None
        if not epoch and not opts.get("no_claim"):
            if opts.get("claim"):
                lease, lease_note = opts["claim"](task, attempt, agent, name, reservations)
            else:
                lease, lease_note = self.claim(task, attempt, agent, name, reservations,
                                               resume=bool(opts.get("continue")))
            if lease:
                lease_id, epoch = lease["id"], lease["epoch"]
            else:
                warn("running %s without a lease: %s" % (aid, lease_note))
        spec = {k: opts.get(k) for k in ("model", "effort", "max_turns", "max_budget_usd", "max_tokens", "fallback_model",
                                         "timeout_min", "claude_flags")}
        spec.update({"task": task, "attempt": attempt, "attempt_id": aid, "branch": branch, "agent": agent,
                     "session_id": str(uuid.uuid4()), "prompt": opts["prompt"], "continue": bool(opts.get("continue")),
                     "lease_id": lease_id, "lease_epoch": epoch, "provider": provider, "provider_fallback": fallback,
                     "provider_profile": self.cfg.profile(provider),
                     "cost_basis": cost_basis(provider, self.cfg.profile(provider))})
        if h.local:
            tf = self.local_token_file(name)
            if tf:
                spec["token_file"] = tf
        self.ensure_plugin(h, probe)
        self.ensure_token(h, probe)
        self.ensure_tunnel(h, probe)
        # PRs are opened on the client (open_pr), never on the host: the host only pushes.
        payload = dict(self.base_payload(), spec=spec)
        out = h.call("launch", payload, timeout=900)
        if out.get("need_plugin"):
            self.ensure_plugin(h, {})
            out = h.call("launch", payload, timeout=900)
        if not out.get("ok"):
            raise LauncherError(out.get("error") or "launch failed")
        old = self.index_get(aid) or {}
        rec = {"id": aid, "host": name, "task": task, "attempt": attempt, "branch": branch, "agent": agent,
               "session_id": spec["session_id"], "lease_id": lease_id, "lease_epoch": epoch, "lease_note": lease_note,
               "worktree": out.get("worktree"), "provider": provider, "provider_fallback": fallback, "created": now(),
               "continue": bool(opts.get("continue")), "collected": False,
               "reservations": reservations or old.get("reservations") or []}
        if skipped:
            rec["placement_skipped"] = {n: r[:200] for n, r in sorted(skipped.items())}
        if opts.get("continue") and old.get("pr_url"):
            rec["pr_url"] = old["pr_url"]  # a --continue run keeps the PR of the earlier run
        for k, v in (opts.get("record") or {}).items():
            rec[k] = v
        self.index_put(rec)
        rec["state_dir"] = out.get("state_dir")
        return rec

    # -- the PR, on the client side
    def open_pr(self, res):
        """Open the PR of a finished attempt with pr_cmd, in the consumer repo root on this machine.

        Only for a `reported`, pushed attempt whose merge-tree is clean (`pr_ready` in the
        host result). A --continue run keeps the PR of the earlier run. -> (url or None, error or None)."""
        aid = res.get("id") or "%s-%s" % (res.get("task"), res.get("attempt"))
        rec = self.index_get(aid) or {}
        if rec.get("pr_url"):
            return rec["pr_url"], None
        if not res.get("pr_ready"):
            return None, "not ready for a PR (state %s, pushed %s, conflicts %s)" % (
                res.get("state"), res.get("pushed"), ", ".join((res.get("conflicts") or [])[:5]) or "none")
        title = res.get("pr_title") or "%s: attempt %s" % (res.get("task"), res.get("attempt"))
        body = res.get("pr_body") or "Task: %s\nAttempt: %s\n" % (res.get("task"), res.get("attempt"))
        url, err = run_pr_cmd(self.cfg, res["branch"], res.get("default_branch") or "main", title, body,
                              os.path.join(self.index_dir(), aid + ".pr-body.md"),
                              task=res.get("task"), attempt=res.get("attempt"))
        if url:
            rec = self.index_get(aid) or {"id": aid}
            rec["pr_url"] = url
            self.index_put(rec)
        return url, err

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


def run_pr_cmd(cfg, branch, base, title, body, body_file, task=None, attempt=None):
    """Run the configured pr_cmd in the consumer repo root (docs/adapters.md, "PR command").

    -> (url or None, error or None). The last http(s) line of stdout is the URL."""
    cmd = cfg.pr_cmd
    if not cmd:
        return None, "no pr_cmd configured"
    with open(body_file, "w", encoding="utf-8") as f:
        f.write(body)
    title = title[:100]
    values = {"branch": branch, "base": base, "title": title, "body_file": body_file,
              "task": str(task or ""), "attempt": str(attempt or "")}
    if isinstance(cmd, list):
        cmd = [fill(a, values) for a in cmd]
    env = dict(os.environ, WAVECOORD_BRANCH=branch, WAVECOORD_BASE=base, WAVECOORD_PR_TITLE=title,
               WAVECOORD_PR_BODY_FILE=body_file, WAVECOORD_TASK=str(task or ""), WAVECOORD_ATTEMPT=str(attempt or ""))
    rc, out, err = run_cmd(cmd, cfg.root, env=env, timeout=300)
    if rc != 0:
        return None, "pr_cmd exit %s: %s" % (rc, (err or out).strip()[-400:])
    urls = [l.strip() for l in out.splitlines() if l.strip().startswith(("http://", "https://"))]
    url = urls[-1] if urls else ((out.strip().splitlines() or [None])[-1])
    return url, None if url else "pr_cmd printed no PR URL"


def fill(arg, values):
    """Replace `{name}` placeholders in one argument of an array command."""
    for k, v in values.items():
        arg = arg.replace("{%s}" % k, v)
    return arg


def prompt_from_spec(spec):
    s = spec.get("sections") or {}
    parts = ["# Task %s%s" % (spec["id"], (": " + spec["title"]) if spec.get("title") else "")]
    if s.get("context"):
        parts += ["## Context", s["context"].strip()]
    t = spec.get("tree")
    if t:  # a task of the spec tree: its requirement and its contracts (docs/specs.md)
        req = ["Requirement `%s`%s." % (t["requirement"], (": " + t["requirement_title"]) if t.get(
            "requirement_title") else "")]
        if t.get("statement"):
            req += ["", t["statement"].strip()]
        for sc in t.get("scenarios") or []:
            req += ["", "Scenario: %s" % sc["name"]] + ["- %s %s" % (w, x) for w, x in sc["steps"]]
        parts += ["## Requirement", "\n".join(req)]
        if t.get("inputs"):
            parts += ["## Inputs (use them; do not change them)", "\n".join(_artifact_line(a) for a in t["inputs"])]
        if t.get("outputs"):
            parts += ["## Outputs (hand these over, as promised)", "\n".join(_artifact_line(a) for a in t["outputs"])]
        if t.get("verify"):
            parts += ["## Also run (the requirement's checks)", "\n".join("- `%s`" % c for c in t["verify"])]
    if s.get("acceptance"):
        parts += ["## Acceptance", "\n".join("- `%s`" % a for a in s["acceptance"])]
    if spec.get("paths"):
        parts += ["## Paths you may change", "\n".join("- `%s`" % p for p in spec["paths"])]
    parts += ["## Prompt", (s.get("prompt") or "").strip()]
    return "\n\n".join(parts) + "\n"


def _artifact_line(a):
    src = (" from `%s`" % a["from"]) if a.get("from") else (" (external)" if a.get("external") else "")
    return "- `%s` (%s)%s%s" % (a["id"], a["kind"], src, (": " + a["contract"]) if a.get("contract") else "")


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
