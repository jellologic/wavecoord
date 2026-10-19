"""The watchdog: idle live lease -> nudge -> stale (watchdog.py, docs/feedback.md)."""

import os
import time
import unittest
from unittest import mock

import _coord
from wavecoord.core import Ctx, lead_ctx
from wavecoord.watchdog import Watchdog

AGENT = Ctx("agent", "host:build-1", agent="w1")


class WatchdogCase(unittest.TestCase):
    def setUp(self):
        self.tr = _coord.TempRepo()
        self.clock = _coord.Clock()
        self.coord = _coord.make_coord(self.tr, self.clock, push=False).open()
        self.heads = {}
        self.seq = 0

    def tearDown(self):
        self.coord.close()
        self.tr.cleanup()

    def wd(self, **kw):
        kw.setdefault("interval_s", 1)
        kw.setdefault("nudge_s", 60)
        kw.setdefault("stale_s", 30)
        return Watchdog(self.coord, head=lambda b: self.heads.get(b), **kw)

    def claim(self, holder="w1", task="api-auth", ctx=None, host="build-1", **extra):
        ctx = ctx or lead_ctx()  # the lead grants a lease; an agent token only renews it
        body = {"task": task, "attempt": 1, "holder": holder, "host": host}
        body.update(extra)
        return self.coord.claim(ctx, body)["lease"]["id"]

    def hb(self, source="launcher", tool=None, agent="w1"):
        self.seq += 1
        body = {"agent": agent, "kind": "worker", "host": "build-1", "source": source, "seq": self.seq,
                "task": "api-auth", "attempt": 1, "branch": "wc/api-auth-1"}
        if tool:
            body["last_tool"] = tool
        return self.coord.hb(Ctx("agent", "host:build-1", agent=agent), body)

    def lease(self, lid):
        return self.coord.state.lease(lid)


class NudgeThenStaleTest(WatchdogCase):
    def test_nudge_then_stale_then_revive(self):
        lid = self.claim()
        self.hb(tool="Bash: make")
        wd = self.wd()
        self.assertEqual(wd.check(), {"nudged": [], "stale": []})
        for _ in range(4):  # launcher beats with the same tool are not activity
            self.clock.advance(20)
            self.hb(tool="Bash: make")
        res = wd.check()
        self.assertEqual(res["nudged"], [lid])
        inbox = self.coord.inbox(AGENT, "w1")["messages"]
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["sender"], "watchdog")
        self.assertIn("no tool activity and no new commit on task api-auth", inbox[0]["body"])
        self.assertEqual(wd.check()["nudged"], [])  # once
        self.clock.advance(20)
        self.assertEqual(wd.check(), {"nudged": [], "stale": []})
        self.clock.advance(15)
        self.assertEqual(wd.check()["stale"], [lid])
        l = self.lease(lid)
        self.assertEqual((l["state"], l["stale_by"]), ("stale", "watchdog"))
        ev = [e for e in self.coord.log.events if e["kind"] == "lease.stale"][-1]
        self.assertEqual(ev["payload"]["by"], "watchdog")
        self.assertIn("no activity", ev["payload"]["reason"])
        # A launcher heartbeat alone does not revive it; real activity does.
        self.hb(tool="Bash: make")
        self.assertEqual(self.lease(lid)["state"], "stale")
        self.hb(source="hook")
        self.assertEqual(self.lease(lid)["state"], "active")

    def test_activity_resets(self):
        lid = self.claim()
        wd = self.wd()
        wd.check()
        self.clock.advance(50)
        self.hb(tool="Edit: a.py")
        self.clock.advance(50)
        self.assertEqual(wd.check()["nudged"], [])  # 50 s since the last tool
        self.clock.advance(20)
        self.assertEqual(wd.check()["nudged"], [lid])
        self.hb(source="hook")  # the worker reacts to the nudge
        self.clock.advance(40)
        self.assertEqual(wd.check(), {"nudged": [], "stale": []})
        self.assertEqual(self.lease(lid)["state"], "active")

    def test_new_commit_counts_as_activity(self):
        lid = self.claim()
        self.hb()
        self.heads["wc/api-auth-1"] = ("a1", None)
        wd = self.wd()
        wd.check()
        self.clock.advance(70)
        self.heads["wc/api-auth-1"] = ("b2", "b2")
        self.assertEqual(wd.check()["nudged"], [])
        self.clock.advance(70)
        self.assertEqual(wd.check()["nudged"], [lid])

    def test_user_set_and_interactive_leases_are_never_touched(self):
        lead = Ctx("lead", "lead")
        l1 = self.claim(ctx=lead, user_set=True)
        self.coord.hb(lead, {"agent": "me", "kind": "other", "host": "local", "source": "hook", "seq": 1})
        l2 = self.claim(holder="me", task="docs", ctx=lead, host="local")
        wd = self.wd()
        for _ in range(5):
            self.clock.advance(100)
            self.assertEqual(wd.check(), {"nudged": [], "stale": []})
        self.assertEqual(self.lease(l1)["state"], "active")
        self.assertTrue(self.lease(l1)["user_set"])
        self.assertEqual(self.lease(l2)["state"], "active")

    def test_only_the_lead_sets_user_set(self):
        from wavecoord.core import ApiError
        with self.assertRaises(ApiError) as cm:
            self.claim(user_set=True, ctx=AGENT)
        self.assertEqual(cm.exception.code, "forbidden")

    def test_restart_starts_the_count_again(self):
        self.claim()
        self.clock.advance(1000)
        wd = self.wd()
        self.assertEqual(wd.check()["nudged"], [])
        self.clock.advance(61)
        self.assertEqual(len(wd.check()["nudged"]), 1)


class ConfigTest(WatchdogCase):
    def test_env_overrides(self):
        env = {"WAVECOORD_WATCHDOG_S": "2", "WAVECOORD_WATCHDOG_NUDGE_S": "5", "WAVECOORD_WATCHDOG_STALE_S": "3"}
        with mock.patch.dict(os.environ, env):
            wd = Watchdog(self.coord)
        self.assertEqual((wd.interval_s, wd.nudge_s, wd.stale_s), (2, 5, 3))
        with mock.patch.dict(os.environ, {"WAVECOORD_WATCHDOG_S": "0"}):
            self.assertIsNone(Watchdog.from_config(self.coord))
        self.assertIsNone(Watchdog.from_config(self.coord, {"enabled": False}))
        wd = Watchdog.from_config(self.coord, {"interval_s": 7, "nudge_s": 8, "stale_s": 9})
        self.assertEqual((wd.interval_s, wd.nudge_s, wd.stale_s), (7, 8, 9))


class ThreadTest(unittest.TestCase):
    def test_background_thread_with_short_settings(self):
        tr = _coord.TempRepo()
        coord = _coord.make_coord(tr, push=False).open()  # the real clock
        try:
            lid = coord.claim(lead_ctx(), {"task": "api-auth", "attempt": 1, "holder": "w1", "host": "build-1"})["lease"]["id"]
            wd = Watchdog.from_config(coord, {"interval_s": 0.1, "nudge_s": 0.5, "stale_s": 0.5}).start()
            try:
                deadline = time.monotonic() + 10
                while coord.state.lease(lid)["state"] != "stale" and time.monotonic() < deadline:
                    time.sleep(0.1)
            finally:
                wd.stop()
            self.assertEqual(coord.state.lease(lid)["state"], "stale")
            kinds = [e["kind"] for e in coord.log.events]
            self.assertLess(kinds.index("message.new"), kinds.index("lease.stale"))
        finally:
            coord.close()
            tr.cleanup()


if __name__ == "__main__":
    unittest.main()
