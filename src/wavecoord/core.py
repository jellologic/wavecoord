"""The coordinator core: the operations behind REST and MCP.

`Coord` owns the event log, the folded state, the sqlite cache and the
in-memory liveness. Every write runs under one lock:

    check the request against the state -> build events -> log.append ->
    state.apply -> store.save

then waits (outside the lock) until the group commit holds its last event, and
only then answers. So concurrent requests share one commit, and an answer
always means the events are durable in git.
"""

import json
import os
import threading
import time

from wavecoord import __version__, const, fencecore, schemaload
from wavecoord.globs import globs_overlap, valid_glob
from wavecoord.log import EventLog, LogError
from wavecoord.store import REVERTIBLE, State, Store, fold

RESERVED_IDS = ("lead", "ui")
READ_ROLES = ("agent", "lead", "ui")
PARKED_FAILED = "finish:failed"  # the reason of a lease parked by a failed run with pushed commits
CONFIG_REL_PATH = os.path.join(".claude", "wavecoord.json")


class ApiError(Exception):
    STATUS = {"bad_request": 400, "unauthorized": 401, "forbidden": 403, "not_found": 404,
              "method_not_allowed": 405, "held": 409, "agent_busy": 409, "conflict": 409, "too_large": 413,
              "misdirected": 421, "unavailable": 503, "clock": 503}

    def __init__(self, code, message, **extra):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.status = self.STATUS[code]
        self.extra = extra


class Ctx(object):
    """The caller of one request: role, principal and the agent it names."""

    def __init__(self, role, principal, agent=None, session_id=None):
        self.role = role
        self.principal = principal
        self.session_id = session_id
        if agent is None and role in ("lead", "ui"):
            agent = role
        self.agent = agent

    @property
    def host(self):
        """The host of an `agent` token (principal `host:<name>`)."""
        if self.role == "agent" and self.principal.startswith("host:"):
            return self.principal[5:]
        return None


def lead_ctx():
    return Ctx("lead", "lead")


def load_consumer_config(repo):
    path = os.path.join(repo, CONFIG_REL_PATH)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def effective_config(repo, cfg):
    """The consumer config with the defaults of docs/adapters.md filled in."""
    out = {
        "coord_url": "http://127.0.0.1:%d" % const.DEFAULT_PORT,
        "project": os.path.basename(os.path.abspath(repo)),
        "log_branch": const.DEFAULT_LOG_BRANCH,
        "plan": {"adapter": "builtin", "dir": "plan/tasks"},
        "hosts": [{"name": "local", "slots": 2, "repo_path": os.path.abspath(repo)}],
        "merge_cmd": ["gh", "pr", "merge", "{pr}", "--merge", "--match-head-commit", "{sha}"],
        "pr_cmd": ["gh", "pr", "create", "--head", "{branch}", "--base", "{base}", "--title", "{title}",
                   "--body-file", "{body_file}"],
        "check_cmd": None,
        "protected_paths_cmd": None,
        "branch_prefix": "wave",
        "auto_resolve": False,
        "auto_steer": "draft",
    }
    out.update({k: v for k, v in cfg.items() if k != "$schema"})
    if "wip_cap" not in out:
        out["wip_cap"] = sum(h.get("slots", 1) for h in out["hosts"])
    return out


# report_request fields kept in the report.post event besides the report itself
REPORT_STATS = ("cost_usd", "turns", "session_id", "cost_basis", "usage", "model_usage", "provider",
                "throttled", "throttle", "next_provider", "pushed", "pr_url", "continue_suggested")


def _schema_check(body, ref):
    if not isinstance(body, dict):
        raise ApiError("bad_request", "the body must be a JSON object")
    errs = schemaload.errors(body, "api", "#/definitions/" + ref)
    if errs:
        raise ApiError("bad_request", "; ".join(errs[:3]))


def _int(v, what):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ApiError("bad_request", "%s must be an integer" % what)


class LogHistory(object):
    def __init__(self, coord):
        self.coord = coord

    def get(self, seq):
        ev = self.coord.log.events
        return ev[seq - 1] if 0 < seq <= len(ev) else None

    def prefix_state(self, seq):
        return self.coord.state_at(seq)


class Coord(object):
    def __init__(self, repo, home=None, branch=None, project=None, clock=None, push=True,
                 commit_window_ms=None, snapshot_every=None, threads=True):
        self.repo = os.path.abspath(repo)
        self.cfg = load_consumer_config(self.repo)
        self.project = project or self.cfg.get("project") or os.path.basename(self.repo)
        self.home = home or const.home_dir(self.project)
        self.branch = branch or self.cfg.get("log_branch") or const.DEFAULT_LOG_BRANCH
        self.clock = clock or time.time
        self.threads = threads
        self.log = EventLog(self.repo, os.path.join(self.home, "log"), branch=self.branch, project=self.project,
                            commit_window_ms=commit_window_ms, snapshot_every=snapshot_every, push=push)
        self.store = None
        self.state = None
        self.wlock = threading.RLock()
        self.ev_cv = threading.Condition()
        self.started = None
        # In-memory liveness: never logged.
        self.seen = {}        # agent -> {"t": float, "tool": str}
        self.renew = {}       # lease id -> {"renewed": float, "expires": float}
        self.hb_seq = {}      # (agent, source) -> last seq
        self.last_tick = None
        self.clock_bad = False
        self._stop = threading.Event()
        self._reaper = None
        self.server_url = None  # set by the HTTP server; the lead tools give it to workers
        self.fence_counts = {}  # (source, verdict or kind) -> count; in memory only (GET /api/live)
        self.check_trace = None  # a list in tests: one record per check, with the decision seq

    # ----- lifecycle -----

    def open(self):
        self.log.open(start_threads=self.threads)
        os.makedirs(self.home, exist_ok=True)
        self.store = Store(os.path.join(self.home, "cache.sqlite"))
        st = self.store.load()
        meta = st.d["meta"]
        if meta.get("last_seq", 0) != self.log.last_seq or meta.get("last_hash") != self.log.last_hash \
                or any("origin" not in r for r in st.values("reservations")):
            st = self.state_at(self.log.last_seq)
            self.store.save(st, full=True)
        self.state = st
        now = self.clock()
        self.started = now
        self.last_tick = now
        ttl = const.get("LEASE_TTL_S")
        for l in st.values("leases"):
            if l["state"] == "active":
                self.renew[l["id"]] = {"renewed": const.parse_ts(l["renewed"]), "expires": now + ttl}
        self.write(Ctx("lead", "server"), None, lambda: [
            self._ev("server.start", {"version": __version__, "api": const.API_VERSION,
                                      "schema": const.SCHEMA_VERSION})], actor_agent=None)
        if self.threads:
            self._reaper = threading.Thread(target=self._reap_loop, daemon=True)
            self._reaper.start()
        return self

    def close(self):
        self._stop.set()
        if self._reaper:
            self._reaper.join(5)
        self.log.close()
        if self.store:
            self.store.close()
        with self.ev_cv:
            self.ev_cv.notify_all()

    def state_at(self, seq):
        """The fold of events 1..seq, from the newest snapshot at or before seq."""
        snap_seq, snap = self.log.snapshot_before(seq)
        start = State.from_dump(snap) if snap else State()
        return fold(self.log.events[snap_seq:seq], start=start, history=LogHistory(self))

    def state_at_sha(self, sha):
        """`rebuild(at_sha)` without side effects: the fold of the log prefix in commit `sha`."""
        return self.state_at(self.log.last_seq_at(sha))

    # ----- the write path -----

    def now(self):
        return self.clock()

    def _ev(self, kind, payload, task=None, attempt=None, primary=False):
        return {"kind": kind, "payload": payload, "task": task, "attempt": attempt, "primary": primary}

    def _actor(self, ctx, actor_agent="ctx"):
        agent = ctx.agent if actor_agent == "ctx" else actor_agent
        a = self.state.get("agents", agent) if agent else None
        host = (a or {}).get("host") or ctx.host or ("local" if ctx.role in ("lead", "ui") else None)
        return {"agent": agent, "host": host, "session_id": ctx.session_id, "principal": ctx.principal}

    def write(self, ctx, idem, build, actor_agent="ctx", expect=None):
        """Run `build()` under the write lock and log its events.

        `build` returns a list of partial events (see _ev), or a tuple
        (events, result) where result is returned as is. Return (events, result,
        duplicate). With an `idem` already in the log, return the logged event
        and log nothing.
        """
        if idem is not None and (not isinstance(idem, str) or not 0 < len(idem) <= 128):
            raise ApiError("bad_request", "the idempotency key must be 1..128 characters")
        with self.wlock:
            self._tick()
            if idem:
                ev = self.log.find_idem(idem)
                if ev is not None:
                    if expect and ev["kind"] not in expect:
                        raise ApiError("conflict", "the idempotency key belongs to a different request")
                    self._wait(ev["seq"])
                    return [ev], None, True
            out = build()
            evs, result = out if isinstance(out, tuple) else (out, None)
            primary = next((i for i, e in enumerate(evs) if e.get("primary")), 0)
            logged = []
            for i, e in enumerate(evs):
                # a secondary event may carry its own key (report proposals: "<report idem>#p<i>")
                logged.append(self._log(ctx, e, idem if i == primary else e.get("idem"), actor_agent))
            if logged:
                logged.insert(0, logged.pop(primary))
        if logged:
            self._wait(logged[-1]["seq"])
        return logged, result, False

    def _log(self, ctx, e, idem, actor_agent="ctx"):
        ev = {"ts": const.fmt_ts(self.now()), "kind": e["kind"], "actor": self._actor(ctx, actor_agent),
              "task": e.get("task"), "attempt": e.get("attempt"), "payload": e["payload"], "idem": idem}
        try:
            full, created = self.log.append(ev)
        except ValueError as err:
            raise ApiError("bad_request", str(err))
        except LogError as err:
            raise ApiError("unavailable", str(err))
        if created:
            self.state.apply(full, LogHistory(self))
            self.store.save(self.state)
            if self.log.snapshot_every and full["seq"] % self.log.snapshot_every == 0:
                self.log.add_snapshot(full["seq"], self.state.dump())
        return full

    def _wait(self, seq):
        try:
            self.log.wait(seq)
        except LogError as err:
            raise ApiError("unavailable", str(err))
        with self.ev_cv:
            self.ev_cv.notify_all()

    # ----- clock and expiry -----

    def _tick(self):
        """Detect a backward clock step or a sleep gap. Called under the write lock."""
        now = self.now()
        back_s = const.get("CLOCK_BACK_MS") / 1000.0
        if self.last_tick is None:
            self.last_tick = now
            return
        if now < self.last_tick - back_s:
            if not self.clock_bad:
                self.clock_bad = True
                self._log(Ctx("lead", "server"), self._ev(
                    "server.clock", {"state": "back", "by_ms": int((self.last_tick - now) * 1000)}), None, None)
            return
        if self.clock_bad:
            self.clock_bad = False
            self._log(Ctx("lead", "server"), self._ev("server.clock", {"state": "ok"}), None, None)
        gap = now - self.last_tick
        if gap > const.get("GAP_S"):
            for r in self.renew.values():
                r["expires"] += gap
            self._log(Ctx("lead", "server"), self._ev("server.gap", {"gap_ms": int(gap * 1000)}), None, None)
        self.last_tick = max(self.last_tick, now)

    def reap(self):
        """Log lease.stale for every active lease past its expiry. Return the stale lease ids."""
        with self.wlock:
            self._tick()
            now = self.now()
            stale = []
            for l in self.state.values("leases"):
                if l["state"] == "active" and self._expires(l) is not None and self._expires(l) < now:
                    stale.append(self._log(Ctx("lead", "server"), self._ev(
                        "lease.stale", {"lease_id": l["id"]}, l["task"], l["attempt"]), None, None))
                    self.renew.pop(l["id"], None)
        if stale:
            self._wait(stale[-1]["seq"])
        return [e["payload"]["lease_id"] for e in stale]

    def _reap_loop(self):
        while not self._stop.wait(const.get("REAPER_S")):
            try:
                self.reap()
            except Exception:  # the reaper must never die
                pass

    def _expires(self, l):
        r = self.renew.get(l["id"])
        if r and l["state"] in ("active", "stale"):
            return r["expires"]
        return const.parse_ts(l["expires_at"]) if l["expires_at"] else None

    # ----- rendering -----

    def render_lease(self, l):
        if l is None:
            return None
        out = {k: l[k] for k in ("id", "task", "attempt", "holder", "epoch", "state", "acquired", "renewed",
                                 "expires_at", "released_at", "released_by", "reason")}
        r = self.renew.get(l["id"])
        if r and l["state"] in ("active", "stale"):
            out["renewed"] = const.fmt_ts(r["renewed"])
            out["expires_at"] = const.fmt_ts(r["expires"])
        return out

    def render_agent(self, a):
        s = self.seen.get(a["id"], {})
        last = s.get("t", const.parse_ts(a["started"]))
        if a.get("gone"):
            state = "gone"
        else:
            state = "live" if self.now() - last <= const.get("AGENT_LIVE_S") else "stale"
        return {"id": a["id"], "kind": a["kind"], "host": a["host"], "session_id": a.get("session_id"),
                "task": a.get("task"), "attempt": a.get("attempt"), "branch": a.get("branch"),
                "worktree": a.get("worktree"), "started": a["started"], "last_seen": const.fmt_ts(last),
                "last_tool": s.get("tool"), "state": state}

    def render_message(self, m, agent=None):
        out = dict(m)
        out["acked"] = bool(agent and self.state.get("acks", "%d/%s" % (m["id"], agent)))
        return out

    def ts(self):
        return const.fmt_ts(self.now())

    # ----- access helpers -----

    def _require(self, ctx, *roles):
        if ctx.role not in roles:
            raise ApiError("forbidden", "role %s may not do this" % ctx.role)

    def _check_agent(self, ctx, agent, host=None):
        """May `ctx` act for `agent` (and bind it to `host`)?"""
        if ctx.role == "lead":
            return
        if ctx.role != "agent":
            raise ApiError("forbidden", "role %s may not act for an agent" % ctx.role)
        if agent in RESERVED_IDS:
            raise ApiError("forbidden", "%s is a reserved id" % agent)
        if host is not None and host != ctx.host:
            raise ApiError("forbidden", "a token of host %s may not act on host %s" % (ctx.host, host))
        a = self.state.get("agents", agent)
        if a is not None and a["host"] != ctx.host:
            raise ApiError("forbidden", "agent %s is bound to host %s" % (agent, a["host"]))

    def _register_events(self, ctx, agent, host, kind="worker", fields=None):
        """agent.register when the agent is new, gone, or its static fields changed."""
        a = self.state.get("agents", agent)
        if a is not None and a["host"] != host:
            raise ApiError("forbidden", "agent %s is bound to host %s" % (agent, a["host"]))
        doc = {"id": agent, "kind": kind, "host": host, "session_id": None, "task": None, "attempt": None,
               "branch": None, "worktree": None, "started": self.ts()}
        if a is not None:
            doc.update({k: a.get(k) for k in ("kind", "session_id", "task", "attempt", "branch", "worktree")})
            doc["started"] = a["started"]
        for k, v in (fields or {}).items():
            if v is not None:
                doc[k] = v
        if a is not None and not a.get("gone") and all(a.get(k) == doc[k] for k in doc if k != "started"):
            return []
        return [self._ev("agent.register", {"agent": doc}, doc["task"], doc["attempt"])]

    # ----- read operations -----

    def health(self):
        return {"now": self.ts(), "ok": self.log.failed is None, "api": const.API_VERSION,
                "schema": const.SCHEMA_VERSION, "version": "wavecoord " + __version__,
                "started": const.fmt_ts(self.started)}

    def live(self, ctx):
        self._require(ctx, *READ_ROLES)
        with self.wlock:
            st = self.state
            drift_s = const.get("PARKED_DRIFT_S")
            leases = [self.render_lease(l) for l in st.values("leases") if l["state"] != "released"]
            return {
                "now": self.ts(),
                "agents": [self.render_agent(a) for a in st.values("agents")],
                "leases": leases,
                "reservations": st.live_reservations(),
                "attempts": [a for a in st.values("attempts") if a["state"] in ("dispatched", "running")],
                "drift": [l for l in leases if l["state"] == "parked"
                          and self.now() - const.parse_ts(l["renewed"]) > drift_s],
                "notices": st.values("notices")[-50:],
                "fence": self.fence_metrics(),
                "server": {"last_seq": self.log.committed_seq, "pushed_seq": self.log.pushed_seq,
                           "push_error": self.log.push_error, "clock_ok": not self.clock_bad,
                           "branch": self.branch, "project": self.project},
            }

    def leases(self, ctx, include_released=False):
        self._require(ctx, "agent", "lead", "ui", "gate")
        with self.wlock:
            ls = [self.render_lease(l) for l in self.state.values("leases")
                  if include_released or l["state"] != "released"]
            return {"now": self.ts(), "leases": ls, "last_epoch": dict(self.state.d["epochs"])}

    def task_lease(self, ctx, task):
        self._require(ctx, "agent", "lead", "ui", "gate")
        with self.wlock:
            return {"now": self.ts(), "task": task, "last_epoch": self.state.last_epoch(task),
                    "lease": self.render_lease(self.state.task_lease(task))}

    def conflicts(self, ctx, globs=None, lease_id=None, mode="exclusive"):
        self._require(ctx, *READ_ROLES)
        with self.wlock:
            out = {"now": self.ts(), "open": [c for c in self.state.values("conflicts") if c["state"] == "open"]}
            if globs:
                for g in globs:
                    if not valid_glob(g):
                        raise ApiError("bad_request", "invalid glob %r" % g)
                if mode not in ("exclusive", "shared"):
                    raise ApiError("bad_request", "mode must be exclusive or shared")
                out["conflicts"] = self.state.conflicts_for([{"glob": g, "mode": mode} for g in globs], lease_id)
            else:
                out["conflicts"] = [{"glob": c["a"]["glob"], "lease_id": c["a"]["lease_id"], "task": c["a"]["task"],
                                     "other_glob": c["b"]["glob"], "other_lease_id": c["b"]["lease_id"],
                                     "other_task": c["b"]["task"], "mode": c["b"]["mode"]}
                                    for c in self.state.path_conflicts()]
            return out

    def fence_modes(self):
        """The fence config of the consumer repo (docs/design/edit-fence.md, section 8)."""
        return fencecore.parse_modes(self.cfg.get("fence"))

    def fence_metrics(self):
        out = {}
        for (src, what), n in sorted(self.fence_counts.items()):
            out.setdefault(src, {})[what] = n
        return {"checks": out, "modes": self.fence_modes()}

    def _count(self, source, violations):
        kinds = sorted(set(v["kind"] for v in violations)) or ["allow"]
        for k in kinds:
            self.fence_counts[(source, k)] = self.fence_counts.get((source, k), 0) + 1

    def check(self, ctx, body):
        """The fence check (check_request -> check_response). Logs nothing.

        The predicate is fencecore.evaluate (docs/design/edit-fence.md, section 2.2):
        no_lease, stale_epoch, outside_lease and foreign_exclusive. With
        source "gate", the writer is the holder of attempt `task`/`attempt`.
        `seq` in the answer is the decision point: the last event seq under the lock.
        """
        self._require(ctx, "agent", "lead", "gate")
        _schema_check(body, "check_request")
        source = body["source"]
        if (ctx.role == "gate" and source != "gate") or (source == "gate" and ctx.role not in ("gate", "lead")):
            raise ApiError("forbidden", "role %s may not send a %s check" % (ctx.role, source))
        for p in body["paths"]:
            why = fencecore.path_syntax_error(p)
            if why:
                raise ApiError("bad_request", why)
        agent = body.get("agent")
        if ctx.role == "agent" and agent:
            self._check_agent(ctx, agent)
        fold = bool(body.get("fold_case"))
        out = {"now": None, "violations": []}
        with self.wlock:
            st = self.state
            if source == "gate":
                if body.get("task") is None or body.get("attempt") is None or body.get("epoch") is None:
                    raise ApiError("bad_request", "a gate check needs task, attempt and epoch")
                att, lease = fencecore.attempt_lease(st, body["task"], body["attempt"])
                if att is None:
                    raise ApiError("not_found", "no attempt %s/%d" % (body["task"], body["attempt"]))
                agent = att.get("holder")
                epoch, from_epoch = body["epoch"], None
                if fencecore.lineage_ok(st, body["task"], body["attempt"], body["epoch"], lease):
                    # resolver lineage: a newer lease of the same attempt (same branch) took over
                    epoch, from_epoch = lease["epoch"], body["epoch"]
                out["lease"] = None if lease is None else {
                    "id": lease["id"], "task": lease["task"], "epoch": lease["epoch"], "state": lease["state"],
                    "holder": lease["holder"]}
                if from_epoch is not None:
                    out["lease"]["from_epoch"] = from_epoch
                viol = fencecore.evaluate(st, agent, body["paths"], body["task"], epoch, fold)
            else:
                viol = fencecore.evaluate(st, agent, body["paths"], body.get("task"), body.get("epoch"), fold)
            seq = self.log.last_seq
            self._count(source, viol)
            if self.check_trace is not None:
                self.check_trace.append({"body": dict(body, agent=agent), "seq": seq, "violations": viol})
            out.update(now=self.ts(), violations=viol, seq=seq)
        return out

    def inbox(self, ctx, agent=None, limit=None):
        self._require(ctx, *READ_ROLES)
        agent = agent or ctx.agent
        if not agent:
            raise ApiError("bad_request", "name the agent (?agent= or X-Wavecoord-Agent)")
        if ctx.role == "agent":
            self._check_agent(ctx, agent)
        limit = limit or const.get("INBOX_MAX")
        with self.wlock:
            msgs = self._inbox(agent)
            return {"now": self.ts(), "messages": [self.render_message(m, agent) for m in msgs[:limit]],
                    "more": len(msgs) > limit}

    def _inbox(self, agent):
        l = self.state.agent_lease(agent)
        a = self.state.get("agents", agent) or {}
        return self.state.inbox(agent, (l or {}).get("task") or a.get("task"))

    def attempts(self, ctx, task=None, state=None):
        self._require(ctx, *READ_ROLES)
        with self.wlock:
            out = [a for a in self.state.values("attempts")
                   if (task is None or a["task"] == task) and (state is None or a["state"] == state)]
        return {"now": self.ts(), "attempts": out}

    def attempt(self, ctx, task, n):
        self._require(ctx, *READ_ROLES)
        with self.wlock:
            a = self.state.get("attempts", "%s/%d" % (task, n))
        if a is None:
            raise ApiError("not_found", "no attempt %s/%d" % (task, n))
        return {"now": self.ts(), "attempt": a}

    def proposals(self, ctx):
        self._require(ctx, *READ_ROLES)
        with self.wlock:
            return {"now": self.ts(), "proposals": self.state.values("proposals")}

    def requests(self, ctx):
        self._require(ctx, *READ_ROLES)
        with self.wlock:
            return {"now": self.ts(), "requests": self.state.values("requests")}

    def config(self, ctx):
        self._require(ctx, *READ_ROLES)
        return {"now": self.ts(), "config": effective_config(self.repo, self.cfg)}

    # ----- heartbeat and finish -----

    def hb(self, ctx, body):
        _schema_check(body, "hb_request")
        self._require(ctx, "agent", "lead")
        agent, source, seq = body["agent"], body["source"], body["seq"]
        self._check_agent(ctx, agent, body["host"])
        dup = {}

        def build():
            last = self.hb_seq.get((agent, source), 0)
            if seq <= last:
                dup["yes"] = True
                return []
            self.hb_seq[(agent, source)] = seq
            now = self.now()
            s = self.seen.setdefault(agent, {})
            s["t"] = now
            # Real activity (for the watchdog): a hook or MCP beat, or a launcher beat with a new tool.
            real = source != "launcher" or bool(body.get("last_tool") and body["last_tool"] != s.get("tool"))
            if real:
                s["act"] = now
            if body.get("last_tool"):
                s["tool"] = body["last_tool"]
            evs = self._register_events(ctx, agent, body["host"], body["kind"], {
                k: body.get(k) for k in ("session_id", "task", "attempt", "branch", "worktree")})
            l = self.state.agent_lease(agent)
            if l is not None:
                evs += self._activity(l, now, real)
            return evs

        self.write(ctx, None, build, actor_agent=agent)
        with self.wlock:
            msgs = self._inbox(agent)
            a = self.state.get("agents", agent)
            out = {"now": self.ts(), "agent": self.render_agent(a),
                   "lease": self.render_lease(self.state.agent_lease(agent)),
                   "lost": self.state.lost(agent),
                   "inbox": [self.render_message(m, agent) for m in msgs[:const.get("INBOX_MAX")]],
                   "more": len(msgs) > const.get("INBOX_MAX")}
        if dup:
            out["duplicate"] = True
        return out

    def _activity(self, l, now, real=True):
        """Renew an active lease in memory; revive a stale one (logged).

        A lease the watchdog made stale revives only on real activity, not on
        a launcher heartbeat alone (watchdog.py).
        """
        ttl = const.get("LEASE_TTL_S")
        if l["state"] == "active":
            self.renew[l["id"]] = {"renewed": now, "expires": now + ttl}
            return []
        if l["state"] == "stale":
            if l.get("stale_by") == "watchdog" and not real:
                return []
            self.renew[l["id"]] = {"renewed": now, "expires": now + ttl}
            return [self._ev("lease.revive", {"lease_id": l["id"], "expires_at": const.fmt_ts(now + ttl)},
                             l["task"], l["attempt"])]
        return []

    def finish(self, ctx, agent, body, idem=None):
        _schema_check(body, "finish_request")
        self._require(ctx, "agent", "lead")
        self._check_agent(ctx, agent)
        dup = {}

        def build():
            a = self.state.get("agents", agent)
            if a is None:
                raise ApiError("not_found", "unknown agent %s" % agent)
            key = (agent, "finish:" + body["source"])
            if body["seq"] <= self.hb_seq.get(key, 0):
                dup["yes"] = True
                return []
            self.hb_seq[key] = body["seq"]
            evs = []
            if not a.get("gone"):
                evs.append(self._ev("agent.gone", {"agent_id": agent, "state": body["state"]},
                                    a.get("task"), a.get("attempt")))
            l = self.state.agent_lease(agent)
            if l is not None and l["state"] in ("active", "stale"):
                self.renew.pop(l["id"], None)
                if body["pushed"] and body["state"] == "done":
                    evs.append(self._ev("lease.park", {"lease_id": l["id"]}, l["task"], l["attempt"]))
                elif body["pushed"] and body["state"] == "failed":
                    # A failed run with pushed commits keeps its reservations: a --continue run of the
                    # same attempt resumes the lease, so no other lease can take its paths in between.
                    evs.append(self._ev("lease.park", {"lease_id": l["id"], "reason": PARKED_FAILED},
                                        l["task"], l["attempt"]))
                else:
                    evs.append(self._ev("lease.release", {"lease_id": l["id"], "reason": "finish:" + body["state"],
                                                          "by": agent}, l["task"], l["attempt"]))
            return evs

        logged, _, was_dup = self.write(ctx, idem, build, actor_agent=agent, expect=("agent.gone", "lease.park", "lease.release"))
        with self.wlock:
            a = self.state.get("agents", agent)
            lease = None
            for e in logged:
                if e["kind"].startswith("lease."):
                    lease = self.state.lease(e["payload"]["lease_id"])
            out = {"now": self.ts(), "agent": self.render_agent(a), "lease": self.render_lease(lease)}
        if dup or was_dup:
            out["duplicate"] = True
        return out

    # ----- leases and reservations -----

    def _enforce_grants(self, items, exclude_lease, force):
        """With the fence in enforce mode, refuse a grant that overlaps a grant of another lease (409)."""
        if force or not items or not fencecore.any_enforce(self.fence_modes()):
            return
        bad = fencecore.grant_conflicts(self.state, items, exclude_lease)
        if bad:
            raise ApiError("conflict", "the fence is in enforce mode: %s overlaps %s of task %s (the lead may force it)"
                           % (bad[0]["glob"], bad[0]["other_glob"], bad[0]["task"]), conflicts=bad)

    def _check_reservations(self, items):
        for it in items:
            if not valid_glob(it["glob"]):
                raise ApiError("bad_request", "invalid glob %r" % it["glob"])

    def claim(self, ctx, body, idem=None):
        _schema_check(body, "claim_request")
        self._require(ctx, "agent", "lead")
        task, attempt, holder, host = body["task"], body["attempt"], body["holder"], body["host"]
        steal = bool(body.get("steal"))
        if steal and ctx.role != "lead":
            raise ApiError("forbidden", "only the lead may steal a lease")
        if body.get("user_set") and ctx.role != "lead":
            raise ApiError("forbidden", "only the lead may set user_set")
        force = bool(body.get("force"))
        if force and ctx.role != "lead":
            raise ApiError("forbidden", "only the lead may force an overlapping grant")
        resume = bool(body.get("resume"))
        self._check_agent(ctx, holder, host)
        given = body.get("reservations", [])
        self._check_reservations(given)

        def build():
            if self.clock_bad:
                raise ApiError("clock", "the server clock went back; grants wait until it catches up")
            now = self.now()
            evs = self._register_events(ctx, holder, host)
            cur = self.state.task_lease(task)
            mine = self.state.agent_lease(holder)
            if cur is not None and cur["holder"] == holder and cur["attempt"] == attempt and not steal:
                evs += self._activity(cur, now)
                res = [r for r in self.state.values("reservations") if r["lease_id"] == cur["id"]]
                return evs, {"lease_id": cur["id"], "reservations": res}
            if mine is not None and (cur is None or mine["id"] != cur["id"]):
                raise ApiError("agent_busy", "%s has a %s lease on %s" % (holder, mine["state"], mine["task"]),
                               lease=self.render_lease(mine))
            # A parked lease hands over without a steal: to a --continue run of the same attempt
            # (`resume`), and, when its run failed, to a newer attempt of the task.
            takeover = cur is not None and cur["state"] == "parked" and (
                (resume and cur["attempt"] == attempt) or
                (cur.get("reason") == PARKED_FAILED and attempt > cur["attempt"]))
            if cur is not None and not steal and not takeover:
                raise ApiError("held", "%s has a %s lease (holder %s)" % (task, cur["state"], cur["holder"]),
                               lease=self.render_lease(cur))
            items = given
            if not items and cur is not None and cur["attempt"] == attempt:
                # the same attempt again (a --continue run): it keeps the paths of its earlier lease
                items = [{"glob": r["glob"], "mode": r["mode"]} for r in fencecore.grant_set(self.state, cur)]
            lid = self.state.next_id("lease")
            last = self.state.last_epoch(task) or 0
            epoch = max(last + 1, int(now * 1000))
            ts = const.fmt_ts(now)
            lease = {"id": lid, "task": task, "attempt": attempt, "holder": holder, "epoch": epoch,
                     "state": "active", "acquired": ts, "renewed": ts,
                     "expires_at": const.fmt_ts(now + const.get("LEASE_TTL_S")),
                     "released_at": None, "released_by": None, "reason": None}
            if body.get("user_set"):
                lease["user_set"] = True  # the watchdog never touches it
            rid = self.state.next_id("reservation")
            res = [{"id": rid + i, "lease_id": lid, "task": task, "glob": it["glob"], "mode": it["mode"],
                    "origin": "claim"} for i, it in enumerate(items)]
            self._enforce_grants(items, cur["id"] if cur else None, force)
            conflicts = self.state.conflicts_for(items, exclude_lease=cur["id"] if cur else None)
            if cur is not None:
                conflicts = [c for c in conflicts if c["lease_id"] != cur["id"]]
                self.renew.pop(cur["id"], None)
                evs.append(self._ev("lease.steal", {"old_lease_id": cur["id"], "lease": lease,
                                                    "reservations": res, "conflicts": conflicts}, task, attempt,
                                    primary=True))
            else:
                evs.append(self._ev("lease.grant", {"lease": lease, "reservations": res, "conflicts": conflicts},
                                    task, attempt, primary=True))
            self.renew[lid] = {"renewed": now, "expires": now + const.get("LEASE_TTL_S")}
            return evs, {"lease_id": lid, "reservations": res}

        logged, result, dup = self.write(ctx, idem, build, expect=("agent.register", "lease.grant", "lease.steal", "lease.revive"))
        with self.wlock:
            if dup:
                p = logged[0]["payload"]
                lid = p["lease"]["id"] if "lease" in p else p.get("lease_id")
                result = {"lease_id": lid, "reservations": p.get("reservations")
                          or [r for r in self.state.values("reservations") if r["lease_id"] == lid]}
            lease = self.state.lease(result["lease_id"]) if result else None
            if lease is None:
                raise ApiError("conflict", "the idempotency key belongs to a different request")
            res = [r for r in result["reservations"] if self.state.get("reservations", r["id"])]
            out = {"now": self.ts(), "lease": self.render_lease(lease), "reservations": res,
                   "conflicts": self.state.conflicts_for(res, exclude_lease=lease["id"])}
        return out

    def release(self, ctx, lease_id, body=None, idem=None):
        body = body or {}
        reason = body.get("reason") or "released"
        if not isinstance(reason, str) or len(reason) > 200:
            raise ApiError("bad_request", "reason must be a short string")

        def build():
            l = self.state.lease(lease_id)
            if l is None:
                raise ApiError("not_found", "no lease %s" % lease_id)
            if ctx.role == "ui":
                if l["state"] != "stale":
                    raise ApiError("forbidden", "the ui may release only a stale lease")
            elif ctx.role == "agent":
                self._check_agent(ctx, l["holder"])
            elif ctx.role != "lead":
                raise ApiError("forbidden", "role %s may not release" % ctx.role)
            if l["state"] == "released":
                return []
            self.renew.pop(l["id"], None)
            by = ctx.agent or ctx.principal
            if ctx.role == "agent" and not ctx.agent:
                by = l["holder"]
            return [self._ev("lease.release", {"lease_id": l["id"], "reason": reason, "by": by},
                             l["task"], l["attempt"])]

        self.write(ctx, idem, build, expect=("lease.release",))
        with self.wlock:
            return {"now": self.ts(), "lease": self.render_lease(self.state.lease(lease_id))}

    def reserve(self, ctx, body, idem=None):
        if isinstance(body, dict) and "glob" in body:  # reservation_request: one glob
            _schema_check(body, "reservation_request")
            one = {"glob": body["glob"], "mode": body["mode"]}
            if body.get("origin"):
                one["origin"] = body["origin"]
            body = {"lease_id": body["lease_id"], "reservations": [one], "force": body.get("force")}
        if not isinstance(body, dict) or not isinstance(body.get("reservations"), list):
            raise ApiError("bad_request", "need {lease_id, glob, mode} or {lease_id, reservations: [{glob, mode}]}")
        lease_id = _int(body.get("lease_id"), "lease_id")
        items = body["reservations"]
        for it in items:
            if not isinstance(it, dict) or it.get("mode") not in ("exclusive", "shared") \
                    or not isinstance(it.get("glob"), str) or it.get("origin") not in (None, "lead", "hot"):
                raise ApiError("bad_request", "each reservation needs glob, mode (exclusive|shared), origin? (lead|hot)")
        self._check_reservations(items)
        force = bool(body.get("force"))
        if ctx.role != "lead":
            if force:
                raise ApiError("forbidden", "only the lead may force an overlapping grant")
            for it in items:
                if not fencecore.agent_reservation_ok(it):
                    raise ApiError("forbidden", "an agent token may add only shared hot-file marks "
                                                "(origin hot); a grant needs the lead")
        items = [dict(it, origin=it.get("origin") or ("lead" if ctx.role == "lead" else "hot")) for it in items]

        def build():
            l = self.state.lease(lease_id)
            if l is None or l["state"] == "released":
                raise ApiError("not_found", "no live lease %s" % lease_id)
            if ctx.role != "lead":
                self._require(ctx, "agent")
                self._check_agent(ctx, l["holder"])
            rid = self.state.next_id("reservation")
            res = [{"id": rid + i, "lease_id": lease_id, "task": l["task"], "glob": it["glob"], "mode": it["mode"],
                    "origin": it["origin"]} for i, it in enumerate(items)]
            self._enforce_grants([it for it in items if it["origin"] != "hot"], lease_id, force)
            conflicts = self.state.conflicts_for(items, exclude_lease=lease_id)
            return [self._ev("reservation.add", {"reservations": res, "conflicts": conflicts},
                             l["task"], l["attempt"])], res

        logged, res, dup = self.write(ctx, idem, build, expect=("reservation.add",))
        if dup:
            res = logged[0]["payload"].get("reservations", [])
        with self.wlock:
            return {"now": self.ts(), "reservations": res,
                    "conflicts": self.state.conflicts_for(res, exclude_lease=lease_id)}

    def unreserve(self, ctx, rid, idem=None):
        def build():
            r = self.state.get("reservations", rid)
            if r is None:
                raise ApiError("not_found", "no reservation %s" % rid)
            l = self.state.lease(r["lease_id"])
            if ctx.role != "lead":
                self._require(ctx, "agent")
                self._check_agent(ctx, l["holder"])
            return [self._ev("reservation.remove", {"reservation_id": r["id"]}, r["task"], l["attempt"])]

        self.write(ctx, idem, build, expect=("reservation.remove",))
        return {"now": self.ts(), "removed": rid}

    # ----- messages, notices, proposals, requests, reports -----

    def _sender(self, ctx):
        if ctx.role == "agent":
            if not ctx.agent:
                raise ApiError("bad_request", "an agent token must name the sender in X-Wavecoord-Agent")
            self._check_agent(ctx, ctx.agent)
        return ctx.agent or ctx.principal

    def send(self, ctx, body, idem=None):
        self._require(ctx, *READ_ROLES)
        if isinstance(body, dict) and isinstance(body.get("body"), str) \
                and len(body["body"].encode("utf-8")) > const.get("MSG_MAX_BYTES"):
            raise ApiError("too_large", "the message body is longer than %d bytes" % const.get("MSG_MAX_BYTES"))
        _schema_check(body, "message_request")
        to = body["to"]
        if to["kind"] in ("agent", "task") and not to.get("id"):
            raise ApiError("bad_request", "to.id is required for kind %s" % to["kind"])
        sender = self._sender(ctx)

        def build():
            rt = body.get("reply_to")
            if rt is not None and self.state.get("messages", rt) is None:
                raise ApiError("not_found", "no message %s" % rt)
            m = {"id": self.state.next_id("message"), "ts": self.ts(), "sender": sender, "to": dict(to),
                 "body": body["body"], "reply_to": rt}
            task = to["id"] if to["kind"] == "task" else None
            return [self._ev("message.new", {"message": m}, task)]

        logged, _, _ = self.write(ctx, idem, build, expect=("message.new",))
        m = logged[0]["payload"]["message"]
        return {"now": self.ts(), "message": dict(m, acked=False)}

    def ack(self, ctx, mid, body=None, idem=None):
        self._require(ctx, *READ_ROLES)
        agent = (body or {}).get("agent") or ctx.agent
        if not agent:
            raise ApiError("bad_request", "name the agent")
        if ctx.role == "agent":
            self._check_agent(ctx, agent)
        elif ctx.role == "ui" and agent != "ui":
            raise ApiError("forbidden", "the ui may ack only its own messages")

        def build():
            if self.state.get("messages", mid) is None:
                raise ApiError("not_found", "no message %s" % mid)
            if self.state.get("acks", "%d/%s" % (mid, agent)):
                return []
            return [self._ev("message.ack", {"message_id": mid, "agent": agent})]

        self.write(ctx, idem, build, expect=("message.ack",))
        return {"now": self.ts(), "message_id": mid, "agent": agent, "acked": True}

    def notify(self, ctx, body, idem=None):
        """A notice for the lead (notify_request)."""
        self._require(ctx, "agent", "lead")
        if isinstance(body, dict) and isinstance(body.get("text"), str) \
                and len(body["text"].encode("utf-8")) > const.get("MSG_MAX_BYTES"):
            raise ApiError("too_large", "the notice is longer than %d bytes" % const.get("MSG_MAX_BYTES"))
        _schema_check(body, "notify_request")
        if ctx.role == "agent":
            self._check_agent(ctx, body["agent"])
        sender = ctx.agent or ctx.principal
        task, attempt = body.get("task"), body.get("attempt")

        def build():
            n = {"id": self.state.next_id("notice"), "ts": self.ts(), "sender": sender, "kind": body["kind"],
                 "agent": body["agent"], "task": task, "attempt": attempt, "text": body["text"],
                 "detail": body.get("detail") or {}}
            return [self._ev("notify.post", {"notice": n}, task, attempt)]

        logged, _, _ = self.write(ctx, idem, build, expect=("notify.post",))
        return {"now": self.ts(), "notice": logged[0]["payload"]["notice"]}

    def propose(self, ctx, body, idem=None):
        self._require(ctx, "agent", "lead")
        if not isinstance(body, dict) or not isinstance(body.get("summary"), str) or not body["summary"]:
            raise ApiError("bad_request", "need {summary, kind?, task?, attempt?, detail?}")
        kind = body.get("kind", "other")
        if kind not in ("plan_edit", "new_task", "split", "dependency", "other"):
            raise ApiError("bad_request", "invalid proposal kind")
        sender = self._sender(ctx)
        task = body.get("task")
        attempt = body.get("attempt")

        def build():
            p = {"id": self.state.next_id("proposal"), "ts": self.ts(), "sender": sender, "kind": kind,
                 "task": task, "attempt": attempt, "summary": body["summary"],
                 "detail": str(body.get("detail") or ""), "state": "open", "note": None, "decided_by": None,
                 "decided_ts": None}
            return [self._ev("proposal.new", {"proposal": p}, task, attempt)]

        logged, _, _ = self.write(ctx, idem, build, expect=("proposal.new",))
        pid = logged[0]["payload"]["proposal"]["id"]
        with self.wlock:
            return {"now": self.ts(), "proposal": self.state.get("proposals", pid) or logged[0]["payload"]["proposal"]}

    def decide(self, ctx, pid, body, idem=None):
        self._require(ctx, "lead")
        if not isinstance(body, dict) or body.get("decision") not in ("accepted", "rejected", "deferred", "done"):
            raise ApiError("bad_request", "decision must be accepted, rejected, deferred or done")

        def build():
            p = self.state.get("proposals", pid)
            if p is None:
                raise ApiError("not_found", "no proposal %s" % pid)
            return [self._ev("proposal.decide", {"proposal_id": pid, "decision": body["decision"],
                                                 "note": body.get("note"), "by": ctx.agent or ctx.principal},
                             p.get("task"), p.get("attempt"))]

        self.write(ctx, idem, build, expect=("proposal.decide",))
        with self.wlock:
            return {"now": self.ts(), "proposal": self.state.get("proposals", pid)}

    def request(self, ctx, body, idem=None):
        self._require(ctx, *READ_ROLES)
        if not isinstance(body, dict) or not isinstance(body.get("task"), str):
            raise ApiError("bad_request", "need {task, note?}")
        if schemaload.errors(body["task"], "api", "#/definitions/task_id"):
            raise ApiError("bad_request", "invalid task id")
        sender = self._sender(ctx)

        def build():
            r = {"id": self.state.next_id("request"), "ts": self.ts(), "sender": sender, "task": body["task"],
                 "note": str(body.get("note") or "")[:1000], "state": "open"}
            return [self._ev("dispatch.request", {"request": r}, body["task"])]

        logged, _, _ = self.write(ctx, idem, build, expect=("dispatch.request",))
        return {"now": self.ts(), "request": logged[0]["payload"]["request"]}

    def _report_proposals(self, ctx, key, task, n, report, a):
        """proposal.new events for report.proposals[], one per item, idempotent by "<report idem>#p<i>".

        The task defaults to the attempt's task; the source is the attempt (`attempt`, `source`)."""
        sender = (a or {}).get("holder") or ctx.agent or ctx.principal
        evs, pid = [], self.state.next_id("proposal")
        for i, item in enumerate(report.get("proposals") or []):
            pidem = ("%s#p%d" % (key, i))[-128:]
            if self.log.find_idem(pidem) is not None:
                continue
            ptask = item.get("task") or task
            if schemaload.errors(ptask, "api", "#/definitions/task_id"):
                ptask = task
            p = {"id": pid, "ts": self.ts(), "sender": sender, "kind": item.get("kind") or "other",
                 "task": ptask, "attempt": n if ptask == task else None, "summary": item["summary"],
                 "detail": str(item.get("detail") or ""), "state": "open", "note": None, "decided_by": None,
                 "decided_ts": None, "source": "%s-%d" % (task, n)}
            ev = self._ev("proposal.new", {"proposal": p}, ptask, p["attempt"])
            ev["idem"] = pidem
            evs.append(ev)
            pid += 1
        return evs

    def report(self, ctx, task, n, body, idem=None):
        """Post the attempt's report (report_request); the launcher's stats ride along."""
        self._require(ctx, "agent", "lead")
        if not isinstance(body, dict) or not isinstance(body.get("report"), dict):
            raise ApiError("bad_request", "need {report, cost_usd?, turns?, session_id?}")
        _schema_check(body, "report_request")
        errs = schemaload.errors(body["report"], "report")
        if errs:
            raise ApiError("bad_request", "invalid report: " + "; ".join(errs[:3]))

        def build():
            a = self.state.get("attempts", "%s/%d" % (task, n))
            if ctx.role == "agent":
                if a is None or not a.get("holder"):
                    raise ApiError("not_found", "no attempt %s/%d" % (task, n))
                self._check_agent(ctx, a["holder"])
            payload = {"report": body["report"]}
            for k in REPORT_STATS:
                if body.get(k) is not None:
                    payload[k] = body[k]
            if isinstance(body.get("changed_paths"), list):
                payload["fence"] = self._report_fence(task, n, body["changed_paths"], body.get("lease_epoch"))
            evs = [self._ev("report.post", payload, task, n, primary=True)]
            evs += self._report_proposals(ctx, key, task, n, body["report"], a)
            return evs

        key = idem or "report:%s:%d" % (task, n)
        self.write(ctx, key, build, expect=("report.post",))
        return self.attempt(ctx, task, n)

    # ----- lead records (dispatch, attempts, conflicts, resolve, refinery) -----

    RECORD_KINDS = ("dispatch.decide", "attempt.start", "attempt.end", "conflict.open", "conflict.close",
                    "resolve.start", "resolve.end", "refinery.queue", "refinery.merge", "refinery.block",
                    "spec.status", "spec.status_blocked")

    def record(self, ctx, kind, payload, task=None, attempt=None, idem=None):
        """Log one lead-side transition. `payload` is a dict, or a function(state) -> dict
        (or None to log nothing) that runs under the write lock. -> the event or None."""
        self._require(ctx, "lead")
        if kind not in self.RECORD_KINDS:
            raise ApiError("bad_request", "kind %s is not a lead record" % kind)

        def build():
            p = payload(self.state) if callable(payload) else payload
            return [] if p is None else [self._ev(kind, p, task, attempt)]

        logged, _, _ = self.write(ctx, idem, build, expect=(kind,))
        return logged[0] if logged else None

    def open_conflict(self, ctx, conflict, task=None, attempt=None, idem=None):
        """conflict.open with the next conflict id, unless the same open conflict exists. -> the conflict."""
        def payload(st):
            for c in st.values("conflicts"):
                if c["state"] == "open" and c.get("kind") == conflict.get("kind") and \
                        c.get("task") == task and c.get("attempt") == attempt:
                    return None
            c = dict(conflict, id=st.next_id("conflict"), state="open", ts=self.ts(), task=task, attempt=attempt)
            return {"conflict": c}

        ev = self.record(ctx, "conflict.open", payload, task, attempt, idem)
        with self.wlock:
            if ev:
                return self.state.get("conflicts", ev["payload"]["conflict"]["id"])
            for c in self.state.values("conflicts"):
                if c["state"] == "open" and c.get("kind") == conflict.get("kind") and \
                        c.get("task") == task and c.get("attempt") == attempt:
                    return c
        return None

    def attempt_end(self, ctx, task, n, body, idem=None):
        """POST /api/attempts/<task>/<n>/end (lead): the collector's final view of an attempt."""
        self._require(ctx, "lead")
        if not isinstance(body, dict) or body.get("state") not in ("reported", "failed", "timed_out", "stopped",
                                                                   "lost", "abandoned"):
            raise ApiError("bad_request", "need {state, pr_url?, pr_error?, conflicts?}")
        keep = ("state",) + State.END_KEYS
        self.record(ctx, "attempt.end", {k: body[k] for k in keep if k in body}, task, n, idem)
        if body["state"] == "abandoned":
            # an abandoned attempt keeps no lease (a parked one of a failed run included)
            with self.wlock:
                _, lease = fencecore.attempt_lease(self.state, task, n)
            if lease is not None and lease["state"] != "released" and lease["attempt"] == n:
                self.release(ctx, lease["id"], {"reason": "abandoned"},
                             idem=None if idem is None else idem + ":release")
        return self.attempt(ctx, task, n)

    def _report_fence(self, task, n, paths, epoch):
        """E4: the fence verdict on the net diff that the launcher reports. A record only."""
        att, lease = fencecore.attempt_lease(self.state, task, n)
        agent = (att or {}).get("holder")
        if epoch is None and lease is not None:
            epoch = lease["epoch"]
        bad = [p for p in paths if fencecore.path_syntax_error(p)]
        good = [p for p in paths if p not in bad]
        viol = [{"path": p, "kind": "outside_lease", "lease_id": None, "task": task, "holder": agent, "glob": None}
                for p in bad]
        if good:
            viol += fencecore.evaluate(self.state, agent, good, task, epoch)
        self._count("report", viol)
        return {"verdict": fencecore.verdict(viol), "violations": viol[:50], "count": len(viol),
                "seq": self.log.last_seq, "epoch": epoch}

    # ----- rollback -----

    def revert(self, ctx, body, idem=None):
        self._require(ctx, "lead")
        body = body or {}
        reason = str(body.get("reason") or "")[:500]

        def build():
            if body.get("at_sha"):
                try:
                    to_seq = self.log.last_seq_at(str(body["at_sha"]))
                except LogError as e:
                    raise ApiError("bad_request", str(e))
                if to_seq >= self.log.last_seq:
                    raise ApiError("conflict", "nothing to roll back: %s is the head" % body["at_sha"])
                return [self._ev("revert", {"to_seq": to_seq, "at_sha": str(body["at_sha"]), "reason": reason})]
            seq = _int(body.get("seq"), "seq")
            target = self.log.events[seq - 1] if 0 < seq <= self.log.last_seq else None
            if target is None:
                raise ApiError("not_found", "no event %d" % seq)
            if target["kind"] not in REVERTIBLE:
                raise ApiError("conflict", "a %s event cannot be reverted; use its own transition" % target["kind"])
            if self.state.get("reverted", seq) is not None:
                raise ApiError("conflict", "event %d is already reverted" % seq)
            return [self._ev("revert", {"target_seq": seq, "reason": reason}, target["task"], target["attempt"])]

        logged, _, _ = self.write(ctx, idem, build, expect=("revert",))
        if logged and "to_seq" in logged[0]["payload"]:
            with self.wlock:
                for lid in list(self.renew):
                    l = self.state.lease(lid)
                    if l is None or l["state"] == "released":
                        self.renew.pop(lid, None)
        return {"now": self.ts(), "event": logged[0] if logged else None}

    def rebuild(self, ctx):
        """Drop the cache and replay the log. The state must not change."""
        self._require(ctx, "lead")
        with self.wlock:
            before = self.state.dump()
            st = self.state_at(self.log.last_seq)
            self.store.save(st, full=True)
            self.state = st
            return {"now": self.ts(), "last_seq": self.log.last_seq, "unchanged": st.dump() == before}

    # ----- events -----

    def events_after(self, seq, limit=1000):
        """Committed events with seq > `seq`."""
        top = min(self.log.committed_seq, len(self.log.events))
        return self.log.events[seq:min(top, seq + limit)]

    def wait_events(self, seq, timeout):
        with self.ev_cv:
            if self.log.committed_seq <= seq and not self._stop.is_set():
                self.ev_cv.wait(timeout)
