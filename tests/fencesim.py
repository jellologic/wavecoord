"""The randomized fence simulation (tests/fence/README.md, section 1).

`run(seed, ops)` builds a World, runs random operations and checks the six
assertions after every operation. It returns the list of failures ([] when
the fence holds). A failure names the seed and the operation.
"""

import http.client
import io
import json
import os
import random  # noqa: F401

from fenceworld import (FAULTS, NAMES, TREE, DIRS, TTL_S, World, changed, git, ocovers, synthetic_report,
                        tree_of)
from wavecoord import fence, gatecheck, hooks
from wavecoord.core import ApiError, Ctx, lead_ctx

OUTAGES = ("down", "delay", "trickle", "500", "503", "truncated")
UNIVERSE = sorted(set(TREE + ["%s/%s" % (d, n) for d in DIRS for n in NAMES] +
                      ["%s/new.py" % d for d in DIRS] + ["x[1].py", "x1.py", "a/bcd.py"]))


class Sim(object):
    def __init__(self, seed, ci=None, n_workers=None):
        self.w = World(seed, n_workers=n_workers, ci=ci)
        self.rng = self.w.rng
        self.fault_left = 0
        self.hb_seq = 1000
        self.stats = {}

    # ---------------------------------------------------------------- helpers
    def count(self, k):
        self.stats[k] = self.stats.get(k, 0) + 1

    @property
    def faulted(self):
        return self.w.proxy.fault is not None

    def journal(self, env):
        return fence.journal_entries(env)

    def run_hook(self, event, inp, env):
        out, err = io.StringIO(), io.StringIO()
        rc = hooks.main(event, stdin=io.StringIO(json.dumps(inp)), stdout=out, env=env, stderr=err)
        txt = out.getvalue().strip()
        d = json.loads(txt) if txt else None
        dec = ((d or {}).get("hookSpecificOutput") or {}).get("permissionDecision")
        if rc == 2:
            dec = "deny"
        return dec or "allow", d

    def trace_since(self, n):
        return self.w.coord.check_trace[n:]

    def seq_of(self, trace):
        return trace[-1]["seq"] if trace else self.w.coord.log.last_seq

    def override_valid(self, env):
        return fence.read_override(env) is not None

    def own_loc(self, wk):
        return "w%d" % wk.i

    def live_lease(self, wk):
        l = self.w.coord.state.lease(wk.lease_id) if wk.lease_id else None
        return l is not None and l["state"] != "released" and l["holder"] == wk.agent

    # ---------------------------------------------------------------- targets
    def pick_rel(self, wk):
        r = self.rng.random()
        if r < 0.6:
            mine = [p for p in TREE if any(ocovers(g, p) for g in wk.globs)]
            if mine:
                return self.rng.choice(mine)
        if r < 0.75:
            return self.rng.choice(TREE)
        if r < 0.9:
            return "%s/new%d.py" % (self.rng.choice(DIRS), self.rng.randrange(3))
        p = self.rng.choice(TREE)
        return p[0].upper() + p[1:]  # case trick

    def pick_target(self, wk):
        w, rng = self.w, self.rng
        kind = rng.choices(["rel", "abs", "dotdot", "symlink", "sibling", "main", "home", "git", "outside",
                            "ignored", "linkout"], [20, 25, 4, 4, 5, 3, 2, 2, 2, 3, 2])[0]
        rel = self.pick_rel(wk)
        if kind == "rel":
            return rel
        if kind == "abs":
            return os.path.join(wk.wt, rel)
        if kind == "dotdot":
            return rng.choice([os.path.join(wk.wt, "a", "..", rel), "a/b/../../" + rel])
        if kind == "symlink":
            return "docs/link/" + rng.choice(["x.py", "y.py", "b/x.py", "new.py", "bc/w.py"])
        if kind == "sibling":
            other = rng.choice(w.workers)
            return os.path.join(other.wt, rel)
        if kind == "main":
            return os.path.join(w.repo, rel)
        if kind == "home":
            return os.path.join(w.home, rng.choice(["fence-override.json", "state/x.json"]))
        if kind == "git":
            return rng.choice([os.path.join(wk.wt, ".git"), os.path.join(w.repo, ".git", "config"),
                               os.path.join(w.repo, ".git", "hooks", "pre-commit")])
        if kind == "outside":
            return os.path.join(w.outside, "x%d.txt" % rng.randrange(3))
        if kind == "ignored":
            return rng.choice(["build/out.txt", "a/debug.log", os.path.join(wk.wt, "build", "o.txt")])
        other = rng.choice(w.workers)
        link = os.path.join(w.outside, "to-%s" % other.agent)
        if not os.path.lexists(link):
            os.symlink(other.wt, link)
        return os.path.join(link, rel)

    def write_file(self, path, cwd):
        p = path if os.path.isabs(path) else os.path.join(cwd, path)
        if self.w.ci_forced:
            p = self.w.fold_case_path(os.path.abspath(p))
        try:
            os.makedirs(os.path.dirname(os.path.realpath(p)), exist_ok=True)
            with open(p, "a") as f:
                f.write("edit %d\n" % self.rng.randrange(10 ** 6))
        except OSError:
            pass
        self.w.dirty = True

    # ---------------------------------------------------------------- worker writes
    def ok_write(self, wk, loc, rel, seq):
        """The oracle verdict on one worker write, and whether it needs a server decision."""
        if loc == "outside":
            return True, False
        if loc in ("git", "home") or loc != self.own_loc(wk):
            return False, True
        if self.w.ignored(rel):
            return True, False
        return self.w.ostate(seq).ok(wk.agent, wk.task, wk.epoch, rel, self.w.fold()), True

    def op_edit(self, wk):
        w = self.w
        target = self.pick_target(wk)
        env = w.worker_env(wk)
        faulted = self.faulted
        n0, j0, ovr = len(w.coord.check_trace), len(self.journal(env)), self.override_valid(env)
        loc, rel = w.truth(target, wk.wt)
        inp = {"session_id": "s-%s" % wk.agent, "cwd": wk.wt, "hook_event_name": "PreToolUse",
               "tool_name": self.rng.choice(["Edit", "Write", "MultiEdit"]), "tool_input": {"file_path": target}}
        dec, out = self.run_hook("PreToolUse", inp, env)
        trace = self.trace_since(n0)
        ok, needs = self.ok_write(wk, loc, rel, self.seq_of(trace))
        accepted = dec == "allow"
        logged = len(self.journal(env)) > j0
        what = "edit %s -> %s:%s by %s (%s) dec=%s fault=%s" % (target, loc, rel, wk.agent, wk.task, dec, w.proxy.fault)
        self.stats["edit_" + ("allow" if accepted else "deny")] = self.stats.get("edit_" + ("allow" if accepted else
                                                                                          "deny"), 0) + 1
        if accepted and not ok and not (ovr and logged):
            w.fail("safety: " + what)
        if accepted and faulted and needs and not (ovr and logged):
            w.fail("fail-closed: " + what)
        if not accepted and not faulted and ok:
            w.fail("liveness: " + what + " out=%s" % json.dumps(out)[:300])
        if accepted and loc not in ("git", "home"):
            self.write_file(target, wk.wt)
            if self.rng.random() < 0.3 and not faulted:
                pi = dict(inp, hook_event_name="PostToolUse", tool_response={"success": True})
                self.run_hook("PostToolUse", pi, env)
        timed_out = "time budget" in json.dumps(out or {})
        if not faulted and trace and not timed_out and self.journal(env):
            w.fail("override log: journal not sent after a reachable call")

    def op_shell(self, wk):
        rel = self.pick_rel(wk) if self.rng.random() < 0.8 else "build/x.txt"
        self.write_file(rel, wk.wt)

    def op_commit(self, wk):
        w, rng = self.w, self.rng
        tracked = sorted(tree_of(wk.wt, "HEAD"))
        if rng.random() < 0.25 and tracked:
            src = rng.choice(tracked)
            dst = "%s/mv%d.py" % (rng.choice(["a", "docs", "ab", "src"]), rng.randrange(5))
            if src not in (".gitignore", ".claude/wavecoord.json") and not os.path.exists(os.path.join(wk.wt, dst)):
                git(wk.wt, "mv", src, dst, check=False)
        elif rng.random() < 0.15 and tracked:
            src = rng.choice(tracked)
            if src not in (".gitignore", ".claude/wavecoord.json"):
                git(wk.wt, "rm", "-q", src, check=False)
        git(wk.wt, "add", "-A")
        from fenceworld import index_of
        staged = changed(tree_of(wk.wt, "HEAD"), index_of(wk.wt))
        if not staged:
            return
        env = w.commit_env(wk)
        faulted = self.faulted
        n0, j0, ovr = len(w.coord.check_trace), len(self.journal(env)), self.override_valid(env)
        rc = __import__("wavecoord.guard", fromlist=["check"]).check("staged", cwd=wk.wt, env=env, err=io.StringIO())
        trace = self.trace_since(n0)
        seq = self.seq_of(trace)
        st = w.ostate(seq)
        oks = [st.ok(wk.agent, wk.task, wk.epoch, p, w.fold()) for p in staged]
        accepted = rc == 0
        self.count("commit_" + ("ok" if accepted else "refused"))
        logged = len(self.journal(env)) > j0
        what = "commit %s by %s (%s) rc=%s fault=%s" % (staged[:6], wk.agent, wk.task, rc, w.proxy.fault)
        if accepted and not all(oks) and not (ovr and logged):
            w.fail("safety: " + what + " bad=%s" % [p for p, o in zip(staged, oks) if not o])
        if accepted and faulted and not (ovr and logged):
            w.fail("fail-closed: " + what)
        if not accepted and not faulted and all(oks):
            w.fail("liveness: " + what)
        if accepted:
            git(wk.wt, "commit", "-q", "--no-verify", "-m", "c")
            wk.committed = True
        else:
            git(wk.wt, "reset", "-q", "--hard", "HEAD")
            git(wk.wt, "clean", "-fdq")
        w.dirty = True

    def net_paths(self, cwd, head):
        mb = git(cwd, "merge-base", "origin/main", head).strip()
        return changed(tree_of(cwd, mb), tree_of(cwd, head))

    def op_push(self, wk):
        w = self.w
        env = w.worker_env(wk)
        faulted = self.faulted
        jbefore = bool(self.journal(env))
        n0 = len(w.coord.check_trace)
        paths = self.net_paths(wk.wt, "HEAD")
        res = fence.push_check(wk.wt, "origin/main", wk.agent, wk.task, wk.epoch, env)
        trace = self.trace_since(n0)
        self.count("push_" + ("ok" if res["ok"] else "refused"))
        st = w.ostate(self.seq_of(trace))
        oks = [st.ok(wk.agent, wk.task, wk.epoch, p, w.fold()) for p in paths]
        what = "push %s by %s (%s) res=%s fault=%s" % (paths[:6], wk.agent, wk.task,
                                                        {k: res[k] for k in ("ok", "kind")}, w.proxy.fault)
        if res["ok"] and not all(oks):
            w.fail("safety: " + what + " bad=%s" % [p for p, o in zip(paths, oks) if not o])
        if res["ok"] and faulted and (paths or jbefore):
            w.fail("fail-closed: " + what)
        if res["ok"] and self.journal(env):
            w.fail("override log: E3 passed while the journal is not empty")
        if not res["ok"] and not faulted and not jbefore and all(oks):
            w.fail("liveness: " + what)
        if res["ok"] and paths:
            git(wk.wt, "push", "-q", "-f", "origin", "HEAD:refs/heads/" + wk.branch)
            wk.pushed = git(wk.wt, "rev-parse", "HEAD").strip()

    def op_merge(self, wk):
        w = self.w
        if not wk.pushed:
            return
        sha = wk.pushed
        git(w.repo, "fetch", "-q", "origin")
        paths = self.net_paths(w.repo, sha)
        try:
            w.coord.report(lead_ctx(), wk.task, wk.attempt, {"report": synthetic_report(wk.task),
                                                              "changed_paths": paths, "lease_epoch": wk.epoch},
                           idem="sim-report:%s:%s" % (wk.task, sha))
            att = w.coord.state.get("attempts", "%s/%d" % (wk.task, wk.attempt))
            fv = (att or {}).get("fence")
            if fv:
                st = w.ostate(fv["seq"])
                want = "pass" if all(st.ok(wk.agent, wk.task, wk.epoch, p, False) for p in paths) else "fail"
                if fv["verdict"] != want:
                    w.fail("E4 verdict %s, oracle %s for %s" % (fv["verdict"], want, paths[:6]))
        except ApiError:
            pass
        faulted = self.faulted
        n0 = len(w.coord.check_trace)
        env = w.base_env("gate")
        code = gatecheck.run(None, wk.task, wk.attempt, "origin/main..%s" % sha, None, "%s@%d" % (wk.task, wk.epoch),
                             w.repo, env=env, out=io.StringIO(), err=io.StringIO(), as_json=True)
        trace = self.trace_since(n0)
        st = w.ostate(self.seq_of(trace))
        oks = [st.ok(wk.agent, wk.task, wk.epoch, p, w.fold()) for p in paths]
        self.count("gate_%d" % code)
        what = "merge %s of %s (%s) code=%s fault=%s" % (paths[:6], wk.agent, wk.task, code, w.proxy.fault)
        if code == 0 and not all(oks):
            w.fail("integration: " + what + " bad=%s" % [p for p, o in zip(paths, oks) if not o])
        if code == 0 and faulted and paths:
            w.fail("fail-closed: " + what)
        if code != 0 and not faulted and all(oks):
            w.fail("liveness: " + what)
        if code == 0:
            p = git(w.repo, "merge", "-q", "--no-ff", "-m", "merge %s" % wk.task, sha, check=False)
            if os.path.exists(os.path.join(w.repo, ".git", "MERGE_HEAD")):
                git(w.repo, "merge", "--abort", check=False)
            else:
                git(w.repo, "push", "-q", "origin", "main")
                git(w.repo, "fetch", "-q", "origin")
                self.stats["merged"] = self.stats.get("merged", 0) + 1
            del p
            try:
                w.coord.release(lead_ctx(), wk.lease_id)
            except ApiError:
                pass
            self.redispatch(wk)

    def redispatch(self, wk):
        try:
            self.w.dispatch(wk)
        except ApiError:
            self.w.clock.advance(20)
            self.w.dispatch(wk)
        wk.env_offline = False
        self.check_new_grant(wk.lease_id)

    # ---------------------------------------------------------------- lead and agent operations
    def check_new_grant(self, lease_id):
        """In enforce mode a grant without force overlaps no grant of another lease (D22)."""
        st = self.w.coord.state
        mine = [r for r in st.live_reservations() if r["lease_id"] == lease_id and r.get("origin") != "hot"]
        if any(f == lease_id for f in self.w.forced):
            return
        others = [r for r in st.live_reservations() if r["lease_id"] != lease_id and r.get("origin") != "hot"]
        for a in mine:
            for b in others:
                if "exclusive" in (a["mode"], b["mode"]) and \
                        any(ocovers(a["glob"], p) and ocovers(b["glob"], p) for p in UNIVERSE):
                    self.w.fail("grant overlap: %s (lease %s) and %s (lease %s) were both granted in enforce mode"
                                % (a["glob"], lease_id, b["glob"], b["lease_id"]))
                    return

    def op_steal(self, wk):
        w = self.w
        w.thieves += 1
        # a steal to another agent, or a new grant to the same agent: the old session keeps its old epoch
        holder = wk.agent if self.rng.random() < 0.3 else "thief%d" % w.thieves
        body = {"task": wk.task, "attempt": wk.attempt + 1, "holder": holder, "host": "local",
                "steal": True, "reservations": w.random_globs()}
        try:
            w.coord.claim(lead_ctx(), body)
        except ApiError as e:
            if e.code == "conflict":
                body["force"] = True
                try:
                    w.coord.claim(lead_ctx(), body)
                except ApiError:
                    return
            else:
                return
        w.dirty = True

    def op_release(self, wk):
        try:
            self.w.coord.release(lead_ctx(), wk.lease_id)
        except ApiError:
            pass

    def op_lead_reserve(self, wk):
        if not self.live_lease(wk):
            return
        it = self.w.random_globs()[:1]
        try:
            self.w.coord.reserve(lead_ctx(), {"lease_id": wk.lease_id, "reservations": it})
        except ApiError:
            return
        self.check_new_grant_items(wk.lease_id, it)

    def check_new_grant_items(self, lease_id, items):
        st = self.w.coord.state
        others = [r for r in st.live_reservations() if r["lease_id"] != lease_id and r.get("origin") != "hot"]
        for a in items:
            for b in others:
                if "exclusive" in (a["mode"], b["mode"]) and \
                        any(ocovers(a["glob"], p) and ocovers(b["glob"], p) for p in UNIVERSE):
                    self.w.fail("grant overlap: lead reservation %s on lease %s overlaps %s of lease %s"
                                % (a["glob"], lease_id, b["glob"], b["lease_id"]))
                    return

    def op_overlap(self, wk):
        """The lead grants an exclusive that overlaps another lease's exclusive: 409 in enforce mode."""
        st = self.w.coord.state
        if not self.live_lease(wk):
            return
        ex = [r for r in st.live_reservations() if r["lease_id"] != wk.lease_id and r["mode"] == "exclusive"
              and r.get("origin") in ("claim", "lead")]
        if not ex:
            return
        r = self.rng.choice(ex)
        try:
            self.w.coord.reserve(lead_ctx(), {"lease_id": wk.lease_id,
                                              "reservations": [{"glob": r["glob"], "mode": "exclusive"}]})
        except ApiError as e:
            if e.code != "conflict":
                self.w.fail("overlap grant: expected 409 conflict, got %s" % e.code)
            return
        self.w.fail("overlap grant: %s on lease %s was granted over lease %s in enforce mode"
                    % (r["glob"], wk.lease_id, r["lease_id"]))

    def agent_req(self, body, agent):
        c = http.client.HTTPConnection("127.0.0.1", self.w.server.port, timeout=10)
        data = json.dumps(body).encode()
        c.putrequest("POST", "/api/reservations", skip_host=True)
        for k, v in (("Host", "127.0.0.1:%d" % self.w.server.port), ("Authorization", "Bearer agent-secret"),
                     ("X-Wavecoord-Agent", agent), ("Content-Type", "application/json"),
                     ("Content-Length", str(len(data)))):
            c.putheader(k, v)
        c.endheaders(data)
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def op_widen(self, wk):
        if not self.live_lease(wk):
            return
        choice = self.rng.randrange(3)
        if choice == 0:
            s = self.agent_req({"lease_id": wk.lease_id, "glob": "**", "mode": "exclusive"}, wk.agent)
            if s != 403:
                self.w.fail("widen: an agent token added an exclusive ** reservation (HTTP %s)" % s)
        elif choice == 1:
            s = self.agent_req({"lease_id": wk.lease_id, "glob": "**", "mode": "shared", "origin": "lead"}, wk.agent)
            if s != 403:
                self.w.fail("widen: an agent token added a lead-origin reservation (HTTP %s)" % s)
        else:
            s = self.agent_req({"lease_id": wk.lease_id, "glob": self.rng.choice(TREE), "mode": "shared",
                                "origin": "hot"}, wk.agent)
            if s != 200:
                self.w.fail("widen: a hot-file mark was refused (HTTP %s)" % s)

    def op_hb(self, wk):
        self.hb_seq += 1
        try:
            self.w.coord.hb(Ctx("agent", "host:local", wk.agent),
                            {"agent": wk.agent, "kind": "worker", "host": "local", "source": "launcher",
                             "seq": self.hb_seq, "task": wk.task, "attempt": wk.attempt})
        except ApiError:
            pass

    def op_expire(self):
        """Time passes past the lease TTL in steps below GAP_S (a larger step is a sleep gap)."""
        from wavecoord import const
        left = const.get("LEASE_TTL_S") + 1
        while left > 0:
            step = min(left, const.get("GAP_S") * 0.8)
            self.w.clock.advance(step)
            self.w.coord.reap()
            left -= step

    def op_rollback(self):
        cs = self.w.coord.log.commits()
        cand = [c for c in cs[len(cs) // 2:-1] if c[1] < self.w.coord.log.last_seq]
        if not cand:
            return
        sha = self.rng.choice(cand)[0]
        try:
            self.w.coord.revert(lead_ctx(), {"at_sha": sha, "reason": "sim"})
        except ApiError:
            pass

    def op_lead_edit(self, lead):
        w = self.w
        rel = self.rng.choice(TREE)
        if lead:
            env = w.base_env("lead")
            env["WAVECOORD_ROLE"] = "lead"
            sid = w.lead_sid
        else:
            env = w.base_env("agent")
            sid = w.other_sid
        faulted = self.faulted
        n0 = len(w.coord.check_trace)
        inp = {"session_id": sid, "cwd": w.repo, "hook_event_name": "PreToolUse", "tool_name": "Edit",
               "tool_input": {"file_path": os.path.join(w.repo, rel)}}
        dec, out = self.run_hook("PreToolUse", inp, env)
        trace = self.trace_since(n0)
        who = "lead" if lead else "other"
        if faulted:
            if dec != "allow":
                w.fail("%s: a server fault (%s) gave %s for %s" % (who, w.proxy.fault, dec, rel))
            return
        foreign = w.ostate(self.seq_of(trace)).foreign(rel, w.fold())
        want = "ask" if foreign else "allow"
        if dec != want:
            w.fail("%s edit %s: got %s, want %s (out=%s)" % (who, rel, dec, want, json.dumps(out)[:200]))

    # ---------------------------------------------------------------- faults and the override
    def op_fault(self):
        self.w.proxy.fault = self.rng.choice(FAULTS)
        self.fault_left = self.rng.randint(1, 3)
        if self.w.proxy.fault in OUTAGES and self.rng.random() < 0.5:
            fence.write_override(self.w.base_env("agent"), 3600, "sim outage")

    def op_agent_trick(self, wk):
        if self.rng.random() < 0.5:
            wk.env_offline = not wk.env_offline
            return
        p = os.path.join(wk.wt, ".claude", "wavecoord.json")
        if wk.cfg_trick:
            git(wk.wt, "checkout", "-q", "--", ".claude/wavecoord.json", check=False)
        else:
            with open(p, "w") as f:
                json.dump({"project": "sim", "fence": "off", "fence_offline": True}, f)
        wk.cfg_trick = not wk.cfg_trick

    def tick_fault(self):
        if self.fault_left > 0:
            self.fault_left -= 1
            if self.fault_left == 0:
                self.w.proxy.fault = None
                if self.rng.random() < 0.6:
                    fence.remove_override(self.w.base_env("agent"))

    # ---------------------------------------------------------------- the run
    def step(self):
        rng, w = self.rng, self.w
        w.dirty = True  # git operations recreate directories: rebuild the inode index
        wk = rng.choice(w.workers)
        if not self.live_lease(wk) and rng.random() < 0.5:
            self.redispatch(wk)
            return "redispatch"
        if self.faulted:
            # during a fault, exercise the points that must fail closed
            op = rng.choices(["edit", "commit", "push", "merge", "lead", "other"], [10, 4, 3, 3, 1, 1])[0]
            if op == "merge" and not wk.pushed:
                cands = [x for x in w.workers if x.pushed]
                wk = rng.choice(cands) if cands else wk
            getattr(self, {"edit": "op_edit", "commit": "op_commit", "push": "op_push", "merge": "op_merge"}.get(
                op, "op_lead_edit"))(*((wk,) if op in ("edit", "commit", "push", "merge") else (op == "lead",)))
            return op
        if wk.pushed and rng.random() < 0.4:
            self.op_merge(wk)
            return "merge"
        if wk.committed and rng.random() < 0.4:
            wk.committed = False
            self.op_push(wk)
            return "push"
        op = rng.choices(
            ["edit", "shell", "commit", "push", "merge", "steal", "release", "reserve", "overlap", "widen", "hb",
             "expire", "rollback", "restart", "clock_back", "clock_fwd", "fault", "trick", "lead", "other"],
            [30, 4, 14, 9, 8, 4, 2, 3, 2, 3, 2, 2, 1, 1, 1, 1, 6, 5, 3, 3])[0]
        if op == "edit":
            self.op_edit(wk)
        elif op == "shell":
            self.op_shell(wk)
        elif op == "commit":
            self.op_commit(wk)
        elif op == "push":
            self.op_push(wk)
        elif op == "merge":
            self.op_merge(wk)
        elif op == "steal":
            self.op_steal(wk)
        elif op == "release":
            self.op_release(wk)
        elif op == "reserve":
            self.op_lead_reserve(wk)
        elif op == "overlap":
            self.op_overlap(wk)
        elif op == "widen":
            self.op_widen(wk)
        elif op == "hb":
            self.op_hb(wk)
        elif op == "expire":
            self.op_expire()
        elif op == "rollback":
            self.op_rollback()
        elif op == "restart":
            if not self.faulted:
                w.restart_server()
        elif op == "clock_back":
            w.clock.advance(-10)
        elif op == "clock_fwd":
            w.clock.advance(15)
        elif op == "fault":
            if not self.faulted:
                self.op_fault()
        elif op == "trick":
            self.op_agent_trick(wk)
        elif op == "lead":
            self.op_lead_edit(True)
        elif op == "other":
            self.op_lead_edit(False)
        return op

    def run(self, ops):
        w = self.w
        try:
            for i in range(ops):
                op = self.step()
                w.log.append(op)
                for e in fence.journal_entries(w.base_env("agent")):
                    w.journaled.add(e.get("n"))
                if op != "fault":
                    self.tick_fault()
                if len(w.failures) > 5:
                    break
            self.finish()
        finally:
            w.close()
        return w.failures

    def finish(self):
        """Clear faults; one reachable hook run sends the override journal (assertion 5)."""
        w = self.w
        w.proxy.fault = None
        for e in fence.journal_entries(w.base_env("agent")):
            w.journaled.add(e.get("n"))
        if not w.journaled:
            return
        wk = w.workers[0]
        inp = {"session_id": "s-%s" % wk.agent, "cwd": wk.wt, "hook_event_name": "PreToolUse", "tool_name": "Edit",
               "tool_input": {"file_path": os.path.join(w.outside, "flush.txt")}}
        env = w.worker_env(wk)
        hooks.handle("UserPromptSubmit", json.dumps(dict(inp, hook_event_name="UserPromptSubmit", prompt="x")), env)
        left = fence.journal_entries(env)
        if left:
            w.fail("override log: %d journal entries left after a reachable call" % len(left))
        sent = set()
        for ev in w.coord.log.events:  # the log, not the state: a rollback drops later notices from the state
            n = ev["payload"].get("notice") if ev["kind"] == "notify.post" else None
            if n and n.get("kind") == "fence_override":
                sent.update(e.get("n") for e in (n.get("detail") or {}).get("entries") or [])
        missing = w.journaled - sent
        if missing:
            w.fail("override log: %d journaled writes never reached the server" % len(missing))


def run(seed, ops=None, ci=None, n_workers=None):
    ops = ops if ops is not None else random.Random(seed).randint(40, int(os.environ.get("WAVECOORD_FENCE_OPS",
                                                                                        "80")))
    s = Sim(seed, ci=ci, n_workers=n_workers)
    return s.run(ops), s.stats
