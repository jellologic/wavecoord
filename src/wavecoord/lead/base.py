"""The lead's operations on the coordinator machine: plan reads and edits, dispatch.

`Lead` runs inside the server process (the lead's machine, the "client" of the
launcher). It reads the coordinator state in process and writes through the
same `Coord` operations as REST, with the lead role. It never holds the
coordinator's write lock while it runs git, a host command or a configured
command.
"""

import os
import threading

from wavecoord import const
from wavecoord.core import ApiError, Ctx
from wavecoord.launcher.common import LauncherError
from wavecoord.plan import graph as g
from wavecoord.plan.adapters import BuiltinPlan, CommandPlan, DirSource, GitSource, PlanError, plan_dir
from wavecoord.plan.edit import EditError
from wavecoord.plan.edit import plan_edit as git_plan_edit

IN_FLIGHT = ("dispatched", "running")
DEFAULTS = {"PROVIDER_BACKOFF_S": 900, "PLAN_FETCH_S": 15, "MAX_RESOLVES": 2}


def lconst(name):
    """A lead constant (docs/contract.md section 5), overridable by WAVECOORD_<NAME>."""
    raw = os.environ.get("WAVECOORD_" + name, "").strip()
    try:
        return float(raw) if raw else DEFAULTS[name]
    except ValueError:
        return DEFAULTS[name]


class Refused(ApiError):
    """A dispatch the lead tools decline, with a machine-readable reason."""

    def __init__(self, reason, message):
        ApiError.__init__(self, "conflict", message, reason=reason)
        self.reason = reason


def lead_ctx():
    return Ctx("lead", "lead")


class LeadBase(object):
    def __init__(self, coord, cfg=None):
        self.coord = coord
        self.repo = coord.repo
        self._cfg = cfg
        self._cfg_key = None
        self._launcher = None
        self.dispatch_lock = threading.RLock()
        self.collect_lock = threading.Lock()
        self.refinery_lock = threading.Lock()
        self._fetch_lock = threading.Lock()
        self._fetched = 0.0
        self.ctx = lead_ctx()

    # ----- config and the launcher -----

    def cfg(self):
        if self._cfg is not None and self._cfg_key is None:
            return self._cfg  # injected (tests)
        from wavecoord.launcher.config import CONFIG_REL_PATH, load_config
        path = os.path.join(self.repo, CONFIG_REL_PATH)
        try:
            key = os.stat(path).st_mtime_ns
        except OSError:
            key = 0
        if self._cfg is None or key != self._cfg_key:
            try:
                self._cfg = load_config(cwd=self.repo)
            except LauncherError as e:
                raise ApiError("bad_request", "consumer config: %s" % e)
            self._cfg_key = key
            self._launcher = None
        return self._cfg

    def coord_url(self):
        from wavecoord.cli import resolve_coord_url
        return resolve_coord_url(cwd=self.repo) or getattr(self.coord, "server_url", None)

    def launcher(self):
        from wavecoord.launcher.client import Launcher
        cfg = self.cfg()
        if self._launcher is None or self._launcher.cfg is not cfg:
            self._launcher = Launcher(cfg, coord_url=self.coord_url())
        return self._launcher

    def root(self):
        return self.cfg().root

    # ----- git -----

    def fetch(self, force=False):
        """`git fetch origin` in the consumer repo, at most once per PLAN_FETCH_S unless forced."""
        from wavecoord.launcher.worktrees import fetch
        with self._fetch_lock:
            if not force and self.coord.now() - self._fetched < lconst("PLAN_FETCH_S"):
                return True
            try:
                fetch(self.repo)
            except LauncherError:
                return False
            self._fetched = self.coord.now()
            return True

    def default_branch(self):
        from wavecoord.launcher.worktrees import default_branch
        try:
            return default_branch(self.repo)
        except LauncherError as e:
            raise ApiError("unavailable", str(e))

    # ----- coordinator state -----

    def snapshot(self):
        """(attempts, leases, live reservations, notices, proposals, requests) copies."""
        st = self.coord.state
        with self.coord.wlock:
            return {"attempts": [dict(a) for a in st.values("attempts")],
                    "leases": [dict(l) for l in st.values("leases")],
                    "live": [dict(r) for r in st.live_reservations()],
                    "notices": [dict(n) for n in st.values("notices")],
                    "proposals": [dict(p) for p in st.values("proposals")],
                    "requests": [dict(r) for r in st.values("requests")],
                    "conflicts": [dict(c) for c in st.values("conflicts")],
                    "refinery": [dict(r) for r in st.values("refinery")]}

    def states(self, snap=None):
        """task -> effective state from the coordinator: merged -> done; a parked lease -> review;
        a live lease or an attempt in flight -> active."""
        snap = snap or self.snapshot()
        out = {}
        for l in snap["leases"]:
            if l["state"] in ("active", "stale"):
                out[l["task"]] = "active"
            elif l["state"] == "parked":
                out.setdefault(l["task"], "review")
        for a in snap["attempts"]:
            if a["state"] in IN_FLIGHT:
                out[a["task"]] = "active"
        for a in snap["attempts"]:
            if a["state"] == "merged":
                out[a["task"]] = "done"
        return out

    def in_flight(self, snap=None):
        snap = snap or self.snapshot()
        return [a for a in snap["attempts"] if a["state"] in IN_FLIGHT]

    # ----- the plan -----

    def plan(self):
        cfg = self.cfg()
        p = cfg.raw.get("plan") or {}
        if p.get("adapter") == "commands":
            return CommandPlan(p.get("commands"), self.root())
        d = plan_dir(p)
        self.fetch()
        from wavecoord.launcher.worktrees import REMOTE
        try:
            ref = "refs/remotes/%s/%s" % (REMOTE, self.default_branch())
            src = GitSource(self.repo, ref, d)
            if src.names():
                return BuiltinPlan(src)
        except ApiError:
            pass
        return BuiltinPlan(DirSource(os.path.join(self.root(), d)))

    def _plan_call(self, fn):
        try:
            return fn()
        except PlanError as e:
            code = "not_found" if str(e).startswith("no task") else "bad_request"
            raise ApiError(code, str(e))

    def plan_ready(self):
        snap = self.snapshot()
        out = self._plan_call(lambda: self.plan().ready(self.states(snap), snap["live"]))
        cap = self.cfg().wip_cap
        out["wip"] = {"in_flight": len(self.in_flight(snap)), "cap": cap}
        return out

    def plan_get(self, task):
        snap = self.snapshot()
        spec = self._plan_call(lambda: self.plan().get(task))
        state = g.effective(spec["state"], self.states(snap).get(task))
        lease = next((l for l in snap["leases"] if l["task"] == task and l["state"] != "released"), None)
        return {"spec": spec, "state": state,
                "proposals": [p for p in snap["proposals"] if p.get("task") == task and p["state"] == "open"],
                "attempts": [a for a in snap["attempts"] if a["task"] == task],
                "lease": self.coord.render_lease(lease) if lease else None}

    def plan_graph(self):
        snap = self.snapshot()
        return self._plan_call(lambda: self.plan().graph(self.states(snap)))

    def plan_validate(self, spec=None):
        return self._plan_call(lambda: self.plan().validate(spec))

    def plan_edit(self, edits, branch=None, title=None, body=None):
        """Anchored edits on a plan/<n> branch; the PR is opened here, on the client side."""
        cfg = self.cfg()
        p = cfg.raw.get("plan") or {}
        if p.get("adapter") == "commands":
            return self._plan_call(lambda: CommandPlan(p.get("commands"), self.root()).edit(edits, branch))
        from wavecoord.launcher.client import run_pr_cmd
        L = self.launcher()

        def pr(br, base, t, b):
            return run_pr_cmd(cfg, br, base, t, b, os.path.join(L.index_dir(), br.replace("/", "-") + ".pr-body.md"))

        try:
            out = git_plan_edit(self.repo, plan_dir(p), edits, branch=branch, pr=pr, title=title, body=body,
                                wt_root=os.path.join(L.index_dir(), "plan-wt"))
        except EditError as e:
            raise ApiError("bad_request", str(e))
        except LauncherError as e:
            raise ApiError("unavailable", str(e))
        self._fetched = 0.0
        return out

    # ----- dispatch -----

    def host_candidates(self, spec, host=None):
        cfg = self.cfg()
        needs = set((spec.get("exec") or {}).get("host_needs") or [])
        names = [host] if host else sorted(cfg.hosts)
        out = []
        for n in names:
            h = cfg.hosts.get(n)
            if h is None:
                raise ApiError("bad_request", "unknown host %r (hosts: %s)" % (n, ", ".join(sorted(cfg.hosts))))
            if not host and not cfg.auto_dispatch(n):
                continue  # `dispatch: false`: only an explicit host places a task here
            have = set([n] + list(h.get("labels") or []))
            if needs <= have:
                out.append(n)
        return out

    def backoff(self, provider, snap=None):
        """Why `provider` is backed off now (a recent rate limit), or None."""
        snap = snap or self.snapshot()
        now, window = self.coord.now(), lconst("PROVIDER_BACKOFF_S")
        for a in snap["attempts"]:
            if a.get("provider") == provider and a.get("throttled") and a.get("ended") and \
                    now - const.parse_ts(a["ended"]) < window:
                return "attempt %s/%s was rate limited at %s" % (a["task"], a["n"], a["ended"])
        for n in snap["notices"]:
            if n.get("kind") == "rate_limit" and (n.get("detail") or {}).get("provider") == provider and \
                    now - const.parse_ts(n["ts"]) < window:
                return "rate-limit notice #%s at %s" % (n["id"], n["ts"])
        return None

    def choose_provider(self, spec, hosts, overrides, snap):
        cfg = self.cfg()
        ex = spec.get("exec") or {}
        fallback = list(overrides.get("provider_fallback") or ex.get("provider_fallback") or [])
        base = overrides.get("provider") or ex.get("provider") or cfg.default_provider
        chain = [base] + [p for p in fallback if p != base]
        why = []
        for i, p in enumerate(chain):
            if not cfg.known_provider(p):
                why.append("%s: not configured" % p)
                continue
            if not [h for h in hosts if p in cfg.host_providers(h)]:
                why.append("%s: no candidate host has it" % p)
                continue
            b = None if (i == 0 and overrides.get("provider")) else self.backoff(p, snap)
            if b:
                why.append("%s: backed off (%s)" % (p, b))
                continue
            # a later provider of the chain that is backed off now is not used for placement
            avoid = [q for q in chain[i + 1:] if cfg.known_provider(q) and self.backoff(q, snap)]
            return p, fallback, why, avoid
        reason = "rate_limited" if any("backed off" in w for w in why) else "no_provider"
        raise Refused(reason, "no provider can run %s now: %s" % (spec["id"], "; ".join(why)))

    def decide(self, spec, host=None, overrides=None, snap=None, check_ready=True, resolver=False):
        """The dispatch decision for one task, or Refused."""
        overrides = overrides or {}
        snap = snap or self.snapshot()
        task = spec["id"]
        if check_ready and not overrides.get("force"):
            r = self.plan().ready(self.states(snap), snap["live"])
            if task not in [t["id"] for t in r["tasks"]]:
                raise Refused("not_ready", "%s is not ready: %s" % (task, r["why_not"].get(task) or "state %s" % (
                    g.effective(spec["state"], self.states(snap).get(task)))))
        cap = self.cfg().wip_cap
        flying = len(self.in_flight(snap))
        if flying >= cap:
            raise Refused("wip_cap", "wip_cap reached: %d attempts in flight, cap %d" % (flying, cap))
        hosts = self.host_candidates(spec, host)
        if not hosts:
            raise Refused("host_needs", "no host has %s" % ", ".join((spec.get("exec") or {}).get("host_needs") or []))
        provider, fallback, why, avoid = self.choose_provider(spec, hosts, overrides, snap)
        reason = "ready; wip %d/%d; provider %s" % (flying + 1, cap, provider)
        if why:
            reason += " (skipped %s)" % "; ".join(why)
        if resolver:
            reason = "resolver; " + reason
        return {"task": task, "hosts": hosts, "provider": provider, "provider_fallback": fallback, "reason": reason,
                "placement_avoid": avoid}

    def _claim(self, steal, holder):
        def claim(task, attempt, agent, host, reservations):
            body = {"task": task, "attempt": attempt, "holder": agent, "host": host}
            if reservations:
                body["reservations"] = reservations
            if steal:
                body["steal"] = True
            try:
                out = self.coord.claim(self.ctx, body)
            except ApiError as e:
                raise LauncherError("lease claim for %s refused (%s): %s" % (task, e.code, e.message))
            holder["lease"] = out["lease"]
            return out["lease"], "claimed"
        return claim

    def launch(self, spec, decision, overrides=None, prompt=None, cont=False, attempt=None, host=None,
               record=None, request_id=None):
        """Claim the lease and start the attempt with the launcher. -> the dispatch result."""
        from wavecoord.launcher.client import prompt_from_spec
        overrides = overrides or {}
        ex = dict(spec.get("exec") or {})
        for k in ("model", "effort", "max_turns", "budget_usd", "timeout_min", "fallback_model"):
            if overrides.get(k) is not None:
                ex[k] = overrides[k]
        opts = {"task": spec["id"], "hosts": decision["hosts"], "host": host, "model": ex.get("model"),
                "effort": ex.get("effort"), "max_turns": ex.get("max_turns"), "max_budget_usd": ex.get("budget_usd"),
                "fallback_model": ex.get("fallback_model"), "timeout_min": ex.get("timeout_min"),
                "provider": decision["provider"], "provider_fallback": decision["provider_fallback"],
                "placement_avoid": decision.get("placement_avoid") or [],
                "reservations": [{"glob": x, "mode": "exclusive"} for x in spec.get("paths") or []] +
                                [{"glob": x, "mode": "shared"} for x in spec.get("shared_paths") or []],
                "prompt": prompt or overrides.get("prompt") or prompt_from_spec(spec),
                "claude_flags": overrides.get("claude_flags"), "continue": cont, "attempt": attempt,
                "record": record}
        holder = {}
        opts["claim"] = self._claim(steal=cont, holder=holder)
        try:
            rec = self.launcher().launch(opts)
        except LauncherError as e:
            lease = holder.get("lease")
            if lease:
                try:
                    self.coord.release(self.ctx, lease["id"], {"reason": "dispatch failed"})
                except ApiError:
                    pass
            reason = "hosts_full" if "full (" in str(e) else "launch_failed"
            raise Refused(reason, "dispatch of %s failed: %s" % (spec["id"], e))
        reason = decision["reason"]
        if rec.get("provider") and rec["provider"] != decision["provider"]:
            reason += "; host %s runs fallback provider %s" % (rec["host"], rec["provider"])
        if rec.get("placement_skipped"):
            reason += "; hosts skipped: %s" % "; ".join(
                "%s: %s" % (n, r[:120]) for n, r in sorted(rec["placement_skipped"].items()))
        payload = {"host": rec["host"], "branch": rec["branch"], "provider": rec.get("provider"),
                   "agent": rec["agent"], "reason": reason, "lease_epoch": rec.get("lease_epoch")}
        if request_id is not None:
            payload["request_id"] = request_id
        if record and record.get("resolver"):
            payload["resolver"] = True
        self.coord.record(self.ctx, "dispatch.decide", payload, spec["id"], rec["attempt"],
                          idem="dispatch:%s" % rec["agent"])
        start = {"state": "running", "host": rec["host"], "branch": rec["branch"], "holder": rec["agent"],
                 "provider": rec.get("provider")}
        if record:
            start.update({k: record[k] for k in ("resolver", "conflict_id") if k in record})
        self.coord.record(self.ctx, "attempt.start", start, spec["id"], rec["attempt"],
                          idem="attempt-start:%s" % rec["agent"])
        return {"task": spec["id"], "attempt": rec["attempt"], "id": rec["id"], "host": rec["host"],
                "branch": rec["branch"], "agent": rec["agent"], "provider": rec.get("provider"),
                "lease_id": rec.get("lease_id"), "lease_epoch": rec.get("lease_epoch"), "reason": reason}

    def _open_request(self, task, snap):
        for r in snap["requests"]:
            if r["task"] == task and r["state"] == "open":
                return r["id"]
        return None

    def dispatch(self, task, host=None, overrides=None, dry_run=False):
        """Start one attempt of one task (dispatch tool)."""
        overrides = dict(overrides or {})
        spec = self._plan_call(lambda: self.plan().get(task))
        with self.dispatch_lock:
            snap = self.snapshot()
            d = self.decide(spec, host, overrides, snap)
            if dry_run:
                return dict(d, dry_run=True)
            return self.launch(spec, d, overrides, host=host, request_id=self._open_request(task, snap))

    def dispatch_wave(self, tasks=None, dry_run=False):
        """Start every ready, non-overlapping task up to wip_cap (dispatch_wave tool)."""
        with self.dispatch_lock:
            snap = self.snapshot()
            ready = self._plan_call(lambda: self.plan().ready(self.states(snap), snap["live"]))
            cands = ready["tasks"]
            skipped = []
            if tasks:
                want = list(tasks)
                ids = [t["id"] for t in cands]
                skipped += [{"task": t, "why": ready["why_not"].get(t) or "not ready"} for t in want if t not in ids]
                cands = [t for t in cands if t["id"] in want]
            requested = [r["task"] for r in snap["requests"] if r["state"] == "open"]
            cands.sort(key=lambda s: 0 if s["id"] in requested else 1)  # stable: plan order otherwise
            cap = self.cfg().wip_cap - len(self.in_flight(snap))
            picked = []
            for s in cands:
                if len(picked) >= cap:
                    skipped.append({"task": s["id"], "why": "wip_cap"})
                    continue
                hit = next((p for p in picked if g.specs_overlap(s, p)), None)
                if hit:
                    skipped.append({"task": s["id"], "why": "overlaps %s in this wave" % hit["id"]})
                    continue
                picked.append(s)
            done, stop = [], None
            for s in picked:
                if stop:
                    skipped.append({"task": s["id"], "why": stop})
                    continue
                try:
                    d = self.decide(s, None, {}, self.snapshot(), check_ready=False)
                    if dry_run:
                        done.append(dict(d, dry_run=True))
                        continue
                    done.append(self.launch(s, d, request_id=self._open_request(s["id"], snap)))
                except Refused as e:
                    skipped.append({"task": s["id"], "why": e.message, "reason": e.reason})
                    if e.reason in ("hosts_full", "wip_cap"):
                        stop = e.reason
            return {"dispatched": done, "skipped": skipped, "dry_run": bool(dry_run),
                    "wip": {"in_flight": len(self.in_flight()), "cap": self.cfg().wip_cap}}
