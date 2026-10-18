"""The launcher against the real coordinator (P1 server) with an agent token for host `local`."""

import hashlib
import json
import os
import unittest
import urllib.request

import _coord
from launcher_env import LauncherTestCase

from wavecoord.server import Server, Tokens

AGENT_TOKEN = "local-agent-test-token"
LEAD_TOKEN = "lead-test-token"


def tokens():
    rows = {AGENT_TOKEN: ("agent", "host:local"), LEAD_TOKEN: ("lead", "lead")}
    return {"version": 1, "tokens": {hashlib.sha256(t.encode()).hexdigest():
                                     {"principal": p, "role": r, "created": "2026-10-18T12:00:00Z"}
                                     for t, (r, p) in rows.items()}}


class LauncherWithServer(LauncherTestCase):
    def setUp(self):
        super(LauncherWithServer, self).setUp()
        self.tr = _coord.TempRepo()
        self.addCleanup(self.tr.cleanup)
        self.core = _coord.make_coord(self.tr, None, push=False).open()
        self.addCleanup(self.core.close)
        self.srv = Server(self.core, port=0, tokens=Tokens(data=tokens())).start()
        self.addCleanup(self.srv.stop)
        self.url = "http://127.0.0.1:%d" % self.srv.port
        os.environ["CLAUDE_PLUGIN_OPTION_TOKEN"] = AGENT_TOKEN

    def get(self, path):
        req = urllib.request.Request(self.url + path, headers={"Authorization": "Bearer " + LEAD_TOKEN})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def test_claim_heartbeat_report_finish(self):
        os.environ["FAKE_CLAUDE_COMMIT"] = "1"
        L = self.launcher()
        L.coord_url = self.url
        rec = L.launch(self.opts())
        self.assertTrue(rec["lease_epoch"])
        r = self.wait_state(L, "t1-1", ("reported", "failed", "timed_out"))
        self.assertEqual(r["state"], "reported", r)
        self.assertEqual((r["report_post"], r["finish_post"]), ("sent", "sent"), r)
        a = self.get("/api/attempts/t1/1")["attempt"]
        self.assertEqual(a["state"], "reported")
        self.assertEqual(a["report"]["status"], "done")
        self.assertEqual((a["provider"], a["cost_basis"]), ("anthropic", "anthropic"))
        self.assertTrue(a["pushed"])
        lease = self.get("/api/tasks/t1/lease")
        self.assertEqual(lease["lease"]["state"], "parked", lease)
        self.assertEqual(lease["lease"]["epoch"], rec["lease_epoch"])
        # the PR body carries the epoch the gate compares with
        self.assertIn("Lease: t1@%d" % rec["lease_epoch"], self.prs()[0]["body"])


if __name__ == "__main__":
    unittest.main()
