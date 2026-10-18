"""Core behavior and the rollback invariants (docs/rollback.md)."""

import os
import random
import unittest

import _coord
from wavecoord import const
from wavecoord.core import ApiError, Ctx, lead_ctx
from wavecoord.log import read_branch
from wavecoord.store import State, Store, fold

LEAD = lead_ctx()


def agent_ctx(host, agent):
    return Ctx("agent", "host:" + host, agent)


def hb(c, ctx, agent, host, seq, **kw):
    body = {"agent": agent, "kind": "worker", "host": host, "source": "launcher", "seq": seq}
    body.update(kw)
    return c.hb(ctx, body)


def advance(c, clock, seconds, step=10):
    """Move the clock like a running server: small steps, a reaper tick at each."""
    while seconds > 0:
        d = min(step, seconds)
        clock.advance(d)
        seconds -= d
        c.reap()


def claim(c, ctx, task, holder, host, globs=(), attempt=1, **kw):
    body = {"task": task, "attempt": attempt, "holder": holder, "host": host,
            "reservations": [{"glob": g, "mode": m} for g, m in globs]}
    body.update(kw)
    return c.claim(ctx, body)


class CoordCase(unittest.TestCase):
    def setUp(self):
        self.tr = _coord.TempRepo()
        self.clock = _coord.Clock()
        self.c = _coord.make_coord(self.tr, self.clock, push=False).open()

    def tearDown(self):
        self.c.close()
        self.tr.cleanup()

    def reopen(self, **kw):
        self.c.close()
        self.c = _coord.make_coord(self.tr, self.clock, push=False, **kw).open()
        return self.c


class LeaseTest(CoordCase):
    def test_claim_held_busy_steal(self):
        a1, a2 = agent_ctx("h1", "w1"), agent_ctx("h2", "w2")
        r = claim(self.c, a1, "api-auth", "w1", "h1", [("src/auth/**", "exclusive")])
        lease = r["lease"]
        self.assertEqual((lease["state"], lease["epoch"]), ("active", int(_coord.T0 * 1000)))
        with self.assertRaises(ApiError) as e:
            claim(self.c, a2, "api-auth", "w2", "h2")
        self.assertEqual((e.exception.code, e.exception.extra["lease"]["id"]), ("held", lease["id"]))
        with self.assertRaises(ApiError) as e:
            claim(self.c, a1, "other", "w1", "h1")
        self.assertEqual(e.exception.code, "agent_busy")
        with self.assertRaises(ApiError) as e:
            claim(self.c, a2, "api-auth", "w2", "h2", steal=True)
        self.assertEqual(e.exception.code, "forbidden")
        # The lead steals for w2; the epoch goes up even with the same clock.
        r2 = claim(self.c, LEAD, "api-auth", "w2", "h2", steal=True)
        self.assertEqual(r2["lease"]["epoch"], lease["epoch"] + 1)
        h = hb(self.c, a1, "w1", "h1", 1)
        self.assertEqual([(x["lease_id"], x["reason"], x["by"]) for x in h["lost"]], [(lease["id"], "steal", "lead")])
        self.assertIsNone(h["lease"])
        # The same claim again is idempotent: same lease, no new event.
        n = self.c.log.last_seq
        self.assertEqual(claim(self.c, LEAD, "api-auth", "w2", "h2")["lease"]["id"], r2["lease"]["id"])
        self.assertEqual(self.c.log.last_seq, n)

    def test_host_binding(self):
        hb(self.c, agent_ctx("h1", "w1"), "w1", "h1", 1)
        with self.assertRaises(ApiError) as e:
            hb(self.c, agent_ctx("h2", "w1"), "w1", "h2", 1)
        self.assertEqual(e.exception.code, "forbidden")
        with self.assertRaises(ApiError):
            hb(self.c, agent_ctx("h1", "lead"), "lead", "h1", 1)
        with self.assertRaises(ApiError):  # body host must be the token's host
            hb(self.c, agent_ctx("h1", "w9"), "w9", "h2", 1)

    def test_stale_revive_park_and_drift(self):
        a = agent_ctx("h1", "w1")
        lid = claim(self.c, a, "t1", "w1", "h1")["lease"]["id"]
        advance(self.c, self.clock, const.get("LEASE_TTL_S") - 5)
        hb(self.c, a, "w1", "h1", 1)  # renews in memory, logs nothing new for the lease
        advance(self.c, self.clock, const.get("LEASE_TTL_S") - 5)
        self.assertEqual(self.c.state.lease(lid)["state"], "active")
        self.clock.advance(10)
        self.assertEqual(self.c.reap(), [lid])
        self.assertEqual(self.c.state.lease(lid)["state"], "stale")
        with self.assertRaises(ApiError) as e:  # stale is not free
            claim(self.c, agent_ctx("h2", "w2"), "t1", "w2", "h2")
        self.assertEqual(e.exception.code, "held")
        r = hb(self.c, a, "w1", "h1", 2)
        self.assertEqual(r["lease"]["state"], "active")
        kinds = [e["kind"] for e in self.c.log.events]
        self.assertEqual(kinds.count("lease.stale"), 1)
        self.assertEqual(kinds.count("lease.revive"), 1)
        f = self.c.finish(a, "w1", {"state": "done", "pushed": True, "source": "launcher", "seq": 3})
        self.assertEqual((f["lease"]["state"], f["lease"]["expires_at"], f["agent"]["state"]), ("parked", None, "gone"))
        advance(self.c, self.clock, const.get("PARKED_DRIFT_S") + 1, step=50)
        self.assertEqual(self.c.state.lease(lid)["state"], "parked")  # parked never expires
        self.assertEqual([l["id"] for l in self.c.live(LEAD)["drift"]], [lid])

    def test_failed_park_resume_and_takeover(self):
        """A failed run with pushed commits parks its lease; a --continue run (resume) of the same
        attempt takes it over with its paths; a newer attempt takes over a failed parked lease;
        a parked lease of a done run needs the lead (steal)."""
        a1, a2, a3 = agent_ctx("h1", "w1"), agent_ctx("h1", "w2"), agent_ctx("h1", "w3")
        l1 = claim(self.c, a1, "t1", "w1", "h1", [("src/**", "exclusive"), ("docs/ci.md", "shared")])["lease"]
        f = self.c.finish(a1, "w1", {"state": "failed", "pushed": True, "source": "launcher", "seq": 1})
        self.assertEqual((f["lease"]["state"], f["lease"]["reason"]), ("parked", "finish:failed"))
        self.assertEqual(len([r for r in self.c.state.live_reservations() if r["lease_id"] == l1["id"]]), 2)
        with self.assertRaises(ApiError) as e:  # resume without the flag: held
            claim(self.c, a2, "t1", "w2", "h1")
        self.assertEqual(e.exception.code, "held")
        r = claim(self.c, a2, "t1", "w2", "h1", resume=True)
        l2 = r["lease"]
        self.assertEqual((l2["attempt"], l2["state"]), (1, "active"))
        self.assertGreater(l2["epoch"], l1["epoch"])
        self.assertEqual(sorted((x["glob"], x["mode"]) for x in r["reservations"]),
                         [("docs/ci.md", "shared"), ("src/**", "exclusive")])
        self.assertEqual(self.c.state.lease(l1["id"])["state"], "released")
        self.assertEqual(self.c.check(a2, {"agent": "w2", "paths": ["src/a.py", "docs/ci.md"], "task": "t1",
                                           "epoch": l2["epoch"], "source": "guard"})["violations"], [])
        # failed again with commits: a newer attempt takes the parked lease over without a steal
        self.c.finish(a2, "w2", {"state": "failed", "pushed": True, "source": "launcher", "seq": 1})
        l3 = claim(self.c, a3, "t1", "w3", "h1", [("src/**", "exclusive")], attempt=2)["lease"]
        self.assertEqual((l3["attempt"], self.c.state.lease(l2["id"])["state"]), (2, "released"))
        # a done run's parked lease is review: no takeover by a newer attempt; resume of the same attempt is fine
        self.c.finish(a3, "w3", {"state": "done", "pushed": True, "source": "launcher", "seq": 1})
        self.assertIsNone(self.c.state.lease(l3["id"])["reason"])
        a4 = agent_ctx("h1", "w4")
        with self.assertRaises(ApiError) as e:
            claim(self.c, a4, "t1", "w4", "h1", attempt=3)
        self.assertEqual(e.exception.code, "held")
        with self.assertRaises(ApiError) as e:  # resume names the attempt of the parked lease
            claim(self.c, a4, "t1", "w4", "h1", attempt=3, resume=True)
        self.assertEqual(e.exception.code, "held")
        l4 = claim(self.c, a4, "t1", "w4", "h1", attempt=2, resume=True)
        self.assertEqual([x["glob"] for x in l4["reservations"]], ["src/**"])

    def test_failed_without_push_releases_and_abandon_releases(self):
        a1 = agent_ctx("h1", "w1")
        claim(self.c, a1, "t1", "w1", "h1", [("src/**", "exclusive")])
        f = self.c.finish(a1, "w1", {"state": "failed", "pushed": False, "source": "launcher", "seq": 1})
        self.assertEqual(f["lease"]["state"], "released")
        a2 = agent_ctx("h1", "w2")
        l2 = claim(self.c, a2, "t2", "w2", "h1", [("lib/**", "exclusive")])["lease"]
        self.c.finish(a2, "w2", {"state": "failed", "pushed": True, "source": "launcher", "seq": 1})
        self.assertEqual(self.c.state.lease(l2["id"])["state"], "parked")
        self.c.attempt_end(LEAD, "t2", 1, {"state": "abandoned"})
        self.assertEqual((self.c.state.lease(l2["id"])["state"], self.c.state.lease(l2["id"])["reason"]),
                         ("released", "abandoned"))
        self.assertEqual(self.reopen().state.lease(l2["id"])["state"], "released")  # rebuilt from the log

    def test_heartbeat_dedup_not_logged(self):
        a = agent_ctx("h1", "w1")
        hb(self.c, a, "w1", "h1", 1)
        n = self.c.log.last_seq
        for s in range(2, 6):
            hb(self.c, a, "w1", "h1", s, last_tool="Bash")
        self.assertEqual(self.c.log.last_seq, n)
        self.assertTrue(hb(self.c, a, "w1", "h1", 3)["duplicate"])

    def test_sleep_gap_shifts_expiry(self):
        a = agent_ctx("h1", "w1")
        lid = claim(self.c, a, "t1", "w1", "h1")["lease"]["id"]
        before = self.c.render_lease(self.c.state.lease(lid))["expires_at"]
        self.clock.advance(const.get("LEASE_TTL_S") * 3)  # the laptop slept
        self.assertEqual(self.c.reap(), [])
        self.assertEqual(self.c.state.lease(lid)["state"], "active")
        after = self.c.render_lease(self.c.state.lease(lid))["expires_at"]
        self.assertEqual(const.parse_ts(after) - const.parse_ts(before), const.get("LEASE_TTL_S") * 3)
        self.assertIn("server.gap", [e["kind"] for e in self.c.log.events])

    def test_clock_back_blocks_grants(self):
        self.clock.advance(-60)
        with self.assertRaises(ApiError) as e:
            claim(self.c, LEAD, "t1", "w1", "h1")
        self.assertEqual((e.exception.code, e.exception.status), ("clock", 503))
        self.clock.advance(60)
        self.assertEqual(claim(self.c, LEAD, "t1", "w1", "h1")["lease"]["state"], "active")
        states = [e["payload"]["state"] for e in self.c.log.events if e["kind"] == "server.clock"]
        self.assertEqual(states, ["back", "ok"])

    def test_reservation_conflicts_and_check(self):
        claim(self.c, LEAD, "a", "w1", "h1", [("src/**", "exclusive"), ("docs/api.md", "shared")])
        r = claim(self.c, LEAD, "b", "w2", "h2", [("src/auth/x.py", "shared"), ("docs/**", "shared")])
        self.assertEqual([(x["glob"], x["other_glob"]) for x in r["conflicts"]], [("src/auth/x.py", "src/**")])
        ck = self.c.check(LEAD, {"agent": "w2", "paths": ["src/auth/x.py", "docs/api.md"], "source": "hook"})
        self.assertEqual([(v["kind"], v["holder"]) for v in ck["violations"]], [("foreign_exclusive", "w1")])
        ck = self.c.check(LEAD, {"agent": "w1", "paths": ["src/auth/x.py", "lib/y.py"], "source": "hook"})
        self.assertEqual([(v["path"], v["kind"]) for v in ck["violations"]], [("lib/y.py", "outside_lease")])
        self.assertEqual(len(self.c.conflicts(LEAD)["conflicts"]), 1)
        self.c.release(LEAD, 1)
        self.assertEqual(self.c.check(LEAD, {"agent": "w2", "paths": ["src/auth/x.py"], "source": "guard"})
                         ["violations"], [])

    def test_ui_releases_only_stale(self):
        ui = Ctx("ui", "ui")
        lid = claim(self.c, LEAD, "t1", "w1", "h1")["lease"]["id"]
        with self.assertRaises(ApiError):
            self.c.release(ui, lid)
        advance(self.c, self.clock, const.get("LEASE_TTL_S") + 1)
        self.assertEqual(self.c.release(ui, lid)["lease"]["state"], "released")


class MessageTest(CoordCase):
    def test_inbox_ack_and_limits(self):
        a = agent_ctx("h1", "w1")
        claim(self.c, a, "api-auth", "w1", "h1")
        m = self.c.send(LEAD, {"to": {"kind": "task", "id": "api-auth"}, "body": "hello"})["message"]
        self.c.send(LEAD, {"to": {"kind": "agent", "id": "w2"}, "body": "not yours"})
        self.c.send(LEAD, {"to": {"kind": "all"}, "body": "everyone"})
        self.assertEqual([x["body"] for x in self.c.inbox(a, "w1")["messages"]], ["hello", "everyone"])
        self.c.ack(a, m["id"])
        self.c.ack(a, m["id"])  # a repeat logs nothing
        self.assertEqual([e["kind"] for e in self.c.log.events].count("message.ack"), 1)
        self.assertEqual([x["body"] for x in self.c.inbox(a, "w1")["messages"]], ["everyone"])
        with self.assertRaises(ApiError) as e:
            self.c.send(LEAD, {"to": {"kind": "all"}, "body": "x" * (const.get("MSG_MAX_BYTES") + 1)})
        self.assertEqual(e.exception.code, "too_large")
        for i in range(const.get("INBOX_MAX") + 2):
            self.c.send(LEAD, {"to": {"kind": "agent", "id": "w1"}, "body": "m%d" % i})
        r = self.c.inbox(a, "w1")
        self.assertEqual((len(r["messages"]), r["more"]), (const.get("INBOX_MAX"), True))

    def test_idempotency_key(self):
        b = {"to": {"kind": "lead"}, "body": "once"}
        m1 = self.c.send(LEAD, b, idem="k1")["message"]
        n = self.c.log.last_seq
        m2 = self.c.send(LEAD, b, idem="k1")["message"]
        self.assertEqual((m1, self.c.log.last_seq), (m2, n))
        with self.assertRaises(ApiError) as e:
            self.c.notify(LEAD, {"kind": "other", "agent": "lead", "text": "x"}, idem="k1")
        self.assertEqual(e.exception.code, "conflict")

    def test_revert_one_event(self):
        m = self.c.send(LEAD, {"to": {"kind": "lead"}, "body": "wrong task"})["message"]
        seq = self.c.log.last_seq
        self.c.revert(LEAD, {"seq": seq, "reason": "oops"})
        self.assertIsNone(self.c.state.get("messages", m["id"]))
        with self.assertRaises(ApiError):
            self.c.revert(LEAD, {"seq": seq})  # already reverted
        lseq = claim(self.c, LEAD, "t1", "w1", "h1") and self.c.log.last_seq
        with self.assertRaises(ApiError) as e:
            self.c.revert(LEAD, {"seq": lseq})
        self.assertEqual(e.exception.code, "conflict")
        # ids never come back
        self.assertEqual(self.c.send(LEAD, {"to": {"kind": "lead"}, "body": "next"})["message"]["id"], m["id"] + 1)
        self.assertEqual(self.c.rebuild(LEAD)["unchanged"], True)


VALID_REPORT = {"status": "done", "summary": "ok", "commits": [], "files": [], "checks_run": [],
                "acceptance_met": True, "questions": [], "proposals": [], "plan_feedback": "", "risks_hit": []}


class ReportProposalTest(CoordCase):
    """report.proposals[] are filed as proposal.new events (0.1.1 fix)."""

    def report(self, idem=None, proposals=None):
        claim(self.c, LEAD, "t1", "w1", "h1")
        props = proposals if proposals is not None else [
            {"kind": "split", "summary": "split the parser"},
            {"kind": "new_task", "summary": "add docs", "task": "t9", "detail": "a page"}]
        return self.c.report(LEAD, "t1", 1, {"report": dict(VALID_REPORT, proposals=props)}, idem=idem)

    def test_report_files_its_proposals(self):
        self.assertEqual(self.c.proposals(LEAD)["proposals"], [])
        self.report()
        ps = sorted(self.c.proposals(LEAD)["proposals"], key=lambda p: p["id"])
        self.assertEqual([(p["kind"], p["task"], p["attempt"], p["source"], p["state"]) for p in ps],
                         [("split", "t1", 1, "t1-1", "open"), ("new_task", "t9", None, "t1-1", "open")])
        self.assertEqual(ps[1]["detail"], "a page")
        self.assertEqual(ps[1]["id"], ps[0]["id"] + 1)
        evs = [e for e in self.c.log.events if e["kind"] == "proposal.new"]
        self.assertEqual([e["idem"] for e in evs], ["report:t1:1#p0", "report:t1:1#p1"])
        self.assertEqual(self.c.attempt(LEAD, "t1", 1)["attempt"]["state"], "reported")

    def test_idempotent_and_rebuilt(self):
        self.report()
        n = self.c.log.last_seq
        self.report()  # the same report again (a spool flush, a retry)
        self.assertEqual(self.c.log.last_seq, n)
        self.assertEqual(len(self.c.proposals(LEAD)["proposals"]), 2)
        # a manual proposal after them takes the next id
        p = self.c.propose(LEAD, {"summary": "later"})["proposal"]
        self.assertEqual(p["id"], 3)
        self.reopen()
        self.assertEqual(len(self.c.proposals(LEAD)["proposals"]), 3)

    def test_no_proposals_no_events(self):
        self.report(proposals=[])
        self.assertEqual([e for e in self.c.log.events if e["kind"] == "proposal.new"], [])


class RandomOps(object):
    """Random operations against a Coord, for the invariant tests."""

    TASKS = ["t%d" % i for i in range(5)]
    AGENTS = [("w%d" % i, "h%d" % (i % 2)) for i in range(4)]
    GLOBS = ["src/**", "src/a/**", "docs/*.md", "docs/api.md", "tests/x.py", "lib/**"]

    def __init__(self, coord, clock, seed):
        self.c, self.clock, self.r = coord, clock, random.Random(seed)
        self.hbseq = 0

    def step(self):
        r, c = self.r, self.c
        agent, host = r.choice(self.AGENTS)
        a = agent_ctx(host, agent)
        op = r.choice(["hb", "claim", "claim", "release", "finish", "send", "ack", "notify", "propose",
                       "decide", "request", "reserve", "unreserve", "revert", "steal", "time", "report"])
        try:
            if op == "hb":
                self.hbseq += 1
                hb(c, a, agent, host, self.hbseq)
            elif op in ("claim", "steal"):
                globs = [(g, r.choice(["exclusive", "shared"])) for g in r.sample(self.GLOBS, r.randint(0, 2))]
                ctx = LEAD if op == "steal" or r.random() < 0.3 else a
                claim(c, ctx, r.choice(self.TASKS), agent, host, globs, attempt=r.randint(1, 3), steal=op == "steal")
            elif op == "release":
                ls = [l for l in c.state.values("leases") if l["state"] != "released"]
                if ls:
                    c.release(LEAD, r.choice(ls)["id"], {"reason": "test"})
            elif op == "finish":
                self.hbseq += 1
                c.finish(LEAD, agent, {"state": r.choice(["done", "failed"]), "pushed": r.random() < 0.5,
                                       "source": "launcher", "seq": self.hbseq})
            elif op == "send":
                to = r.choice([{"kind": "all"}, {"kind": "lead"}, {"kind": "agent", "id": agent},
                               {"kind": "task", "id": r.choice(self.TASKS)}])
                c.send(LEAD, {"to": to, "body": "m%d" % r.randint(0, 999)}, idem=r.choice([None, "m%d" % r.randint(0, 5)]))
            elif op == "ack":
                ms = c.state.values("messages")
                if ms:
                    c.ack(LEAD, r.choice(ms)["id"], {"agent": agent})
            elif op == "notify":
                c.notify(LEAD, {"kind": "ci", "agent": "lead", "text": "n"})
            elif op == "propose":
                c.propose(LEAD, {"summary": "split it", "kind": "split", "task": r.choice(self.TASKS)})
            elif op == "decide":
                ps = c.state.values("proposals")
                if ps:
                    c.decide(LEAD, r.choice(ps)["id"], {"decision": r.choice(["accepted", "rejected"])})
            elif op == "request":
                c.request(LEAD, {"task": r.choice(self.TASKS)})
            elif op == "reserve":
                ls = [l for l in c.state.values("leases") if l["state"] != "released"]
                if ls:
                    c.reserve(LEAD, {"lease_id": r.choice(ls)["id"],
                                     "reservations": [{"glob": r.choice(self.GLOBS), "mode": "exclusive"}]})
            elif op == "unreserve":
                rs = c.state.values("reservations")
                if rs:
                    c.unreserve(LEAD, r.choice(rs)["id"])
            elif op == "revert":
                cands = [e["seq"] for e in c.log.events if e["kind"] in ("message.new", "notify.post", "proposal.new")]
                if cands:
                    c.revert(LEAD, {"seq": r.choice(cands)})
            elif op == "time":
                gap = r.choice([1, 30, const.get("LEASE_TTL_S") + 1, const.get("GAP_S") + 5])
                if gap > const.get("GAP_S") and r.random() < 0.5:
                    self.clock.advance(gap)  # a sleep gap
                    c.reap()
                else:
                    advance(c, self.clock, gap, step=30)
            elif op == "report":
                atts = c.state.values("attempts")
                if atts:
                    at = r.choice(atts)
                    c.report(LEAD, at["task"], at["n"], {"report": dict(VALID_REPORT, status=r.choice(["done", "failed"]))})
        except ApiError:
            pass  # refused operations are part of the test


class InvariantTest(unittest.TestCase):
    """rebuild --at <sha> == fold of that prefix; replay == live cache."""

    def setUp(self):
        self.tr = _coord.TempRepo()
        self.clock = _coord.Clock()

    def tearDown(self):
        self.tr.cleanup()

    def run_ops(self, seed, n, snapshot_every):
        c = _coord.make_coord(self.tr, self.clock, push=False, snapshot_every=snapshot_every).open()
        ops = RandomOps(c, self.clock, seed)
        for _ in range(n):
            ops.step()
        return c

    def test_prefix_rebuild_equals_fold(self):
        for seed in (1, 2):
            self.tr.cleanup()
            self.tr = _coord.TempRepo()
            c = self.run_ops(seed, 90, snapshot_every=7)
            try:
                events = read_branch(self.tr.repo)
                self.assertEqual(len(events), c.log.last_seq)
                self.assertTrue(c.log.snapshots)
                commits = c.log.commits()
                self.assertGreater(len(commits), 20)
                for i, (sha, last) in enumerate(commits):
                    want = fold(events[:last]).dump()  # pure fold, no snapshots
                    self.assertEqual(c.state_at(last).dump(), want, "seed %d sha %s seq %d" % (seed, sha, last))
                    if i % 4 == 0 or i == len(commits) - 1:  # through git, on a sample (git calls are slow)
                        self.assertEqual(c.log.last_seq_at(sha), last)
                        self.assertEqual(c.state_at_sha(sha).dump(), want)
                # Live state == fold of the whole log == the sqlite cache.
                live = c.state.dump()
                self.assertEqual(fold(events).dump(), live)
                self.assertEqual(c.store.load().dump(), live)
                self.assertEqual(fold(events).dump(), fold(events).dump())  # deterministic
                self.assertEqual(State.from_dump(live).dump(), live)
            finally:
                c.close()

    def test_delete_sqlite_and_replay_equals_live(self):
        c = self.run_ops(7, 100, snapshot_every=11)
        live = c.state.dump()
        c.close()
        os.remove(os.path.join(self.tr.home, "cache.sqlite"))
        from wavecoord.corecli import offline_rebuild
        offline_rebuild(self.tr.repo, self.tr.home)
        st = Store(os.path.join(self.tr.home, "cache.sqlite"))
        self.assertEqual(st.load().dump(), live)
        st.close()
        # A reopen with a cache that is behind the log rebuilds it too.
        c2 = _coord.make_coord(self.tr, self.clock, push=False).open()
        try:
            self.assertEqual(c2.state_at(len(c2.log.events) - 1).dump(), live)
            self.assertEqual(c2.store.load().dump(), c2.state.dump())
        finally:
            c2.close()

    def test_rollback_at_sha(self):
        c = self.run_ops(11, 60, snapshot_every=5)
        try:
            # Put a lease after the rollback point, so the fencing exception shows.
            sha = c.log.head()
            to_seq = c.log.last_seq_at(sha)
            prefix = c.state_at(to_seq)
            ops = RandomOps(c, self.clock, 12)
            for _ in range(30):
                ops.step()
            for l in c.state.values("leases"):
                if l["state"] != "released":
                    c.release(LEAD, l["id"])
            new = claim(c, LEAD, "fresh", "w-new", "h9")["lease"]
            epochs_before = dict(c.state.d["epochs"])
            c.revert(LEAD, {"at_sha": sha, "reason": "test"})
            st = c.state
            for coll in ("messages", "acks", "proposals", "requests", "notices", "conflicts"):
                self.assertEqual(st.d[coll], prefix.d[coll], coll)
            for t, e in epochs_before.items():
                self.assertGreaterEqual(st.d["epochs"][t], e)
            nl = st.lease(new["id"])
            self.assertEqual((nl["state"], nl["reason"]), ("released", "rollback"))
            self.assertIn(new["id"], [x["lease_id"] for x in st.lost("w-new")])
            for lid, l in prefix.d["leases"].items():
                if l["state"] != "released" and st.d["leases"][lid]["state"] != "released":
                    self.assertEqual(st.d["leases"][lid], l)
            # The rollback itself replays deterministically.
            self.assertEqual(fold(read_branch(self.tr.repo)).dump(), st.dump())
            self.assertEqual(c.rebuild(LEAD)["unchanged"], True)
            # A new grant after the rollback still gets a higher epoch.
            g = claim(c, LEAD, "fresh", "w-new", "h9")["lease"]
            self.assertGreater(g["epoch"], new["epoch"])
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
