"""The launcher against the real coordinator (P1 server) with an agent token for host `local`."""

import hashlib
import io
import json
import os
import subprocess
import time
import unittest
import urllib.request

import _coord
from launcher_env import LauncherTestCase

from wavecoord import hooks
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
        # the launcher claims with the lead token; the wrapper and the worker use the host's agent token
        os.environ["CLAUDE_PLUGIN_OPTION_TOKEN"] = LEAD_TOKEN
        home = os.environ["WAVECOORD_HOME"]
        os.makedirs(os.path.join(home, "hosts"), exist_ok=True)
        with open(os.path.join(home, "hosts", "local.token"), "w") as f:
            f.write(AGENT_TOKEN + "\n")

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

    def _worker_env(self, dump):
        """The env of the worker run that `dump` recorded, as its Bash tool would pass it to git."""
        with open(dump) as f:
            wenv = json.load(f)["env"]
        env = dict(os.environ)
        for k in list(env):
            if k.startswith(("WAVECOORD_", "CLAUDE")):
                env.pop(k)
        env.update({k: v for k, v in wenv.items() if k.startswith("WAVECOORD_")})
        env["WAVECOORD_HOME"] = os.environ["WAVECOORD_HOME"]
        return env

    def test_continue_after_failed_run_keeps_the_lease_paths(self):
        """Reproducer (worker report on attempt M1.7a-2): a run fails after it pushed commits, a
        --continue run takes over. The guard and the hooks must see the paths of the lease as the
        worker's own, and a violation line must never print None."""
        home = os.environ["WAVECOORD_HOME"]
        os.makedirs(os.path.join(home, "hosts"), exist_ok=True)
        with open(os.path.join(home, "hosts", "local.token"), "w") as f:
            f.write(AGENT_TOKEN + "\n")
        os.environ["FAKE_CLAUDE_COMMIT"] = "1"
        os.environ["FAKE_CLAUDE_COMMIT_FILE"] = "src/first.txt"
        os.environ["FAKE_CLAUDE_EXIT"] = "1"  # like error_max_turns: a failed run with commits
        L = self.launcher()
        L.coord_url = self.url
        res = [{"glob": "src/**", "mode": "exclusive"}, {"glob": "docs/ci.md", "mode": "shared"}]
        rec1 = L.launch(self.opts(reservations=res))
        r = self.wait_state(L, "t1-1", ("reported", "failed", "timed_out"))
        self.assertEqual(r["state"], "failed", r)
        self.assertTrue(r["pushed"], r)
        lease1 = self.get("/api/tasks/t1/lease")["lease"]
        # parked, not released: the reservations stay with the attempt
        self.assertEqual((lease1["id"], lease1["state"], lease1["reason"]), (rec1["lease_id"], "parked", "finish:failed"))
        self.assertEqual(sorted(x["glob"] for x in self.get("/api/live")["reservations"]
                                if x["lease_id"] == lease1["id"]), ["docs/ci.md", "src/**"])

        # --continue as the CLI runs it: no --spec, so no reservations in the request
        os.environ.pop("FAKE_CLAUDE_EXIT")
        os.environ["FAKE_CLAUDE_COMMIT_FILE"] = "src/second.txt"
        dump = os.path.join(self.tmp, "dump2.json")
        os.environ["FAKE_CLAUDE_DUMP"] = dump
        rec2 = L.launch(self.opts(prompt="Go on.", **{"continue": True}))
        self.assertEqual(rec2["id"], "t1-1")
        self.assertNotEqual(rec2["agent"], rec1["agent"])
        self.assertGreater(rec2["lease_epoch"], rec1["lease_epoch"])
        r = self.wait_state(L, "t1-1", ("reported", "failed", "timed_out"))
        self.assertEqual(r["state"], "reported", r)
        live = self.get("/api/live")
        lease2 = next(l for l in live["leases"] if l["task"] == "t1" and l["state"] != "released")
        self.assertEqual(lease2["id"], rec2["lease_id"])
        self.assertEqual(sorted((x["glob"], x["mode"]) for x in live["reservations"] if x["lease_id"] == lease2["id"]),
                         [("docs/ci.md", "shared"), ("src/**", "exclusive")])

        # the guard identity of the continued worktree names the new run
        wt = r["worktree"]
        gd = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=wt, stdout=subprocess.PIPE,
                            universal_newlines=True, check=True).stdout.strip()
        with open(os.path.join(gd, "wavecoord-hooks", "identity.json")) as f:
            ident = json.load(f)
        self.assertEqual((ident["agent"], ident["epoch"], ident["lease_id"]),
                         (rec2["agent"], rec2["lease_epoch"], rec2["lease_id"]))

        # the server resumes the lease for the new holder; check as a third run would (a live lease)
        os.environ["FAKE_CLAUDE_COMMIT_FILE"] = "src/third.txt"
        os.environ["FAKE_CLAUDE_EXIT"] = "1"
        dump3 = os.path.join(self.tmp, "dump3.json")
        os.environ["FAKE_CLAUDE_DUMP"] = dump3
        os.environ["FAKE_CLAUDE_SLEEP"] = "30"
        rec3 = L.launch(self.opts(prompt="More.", **{"continue": True}))
        try:
            for _ in range(100):
                if os.path.exists(dump3):
                    break
                time.sleep(0.1)
            env = self._worker_env(dump3)
            self.assertEqual(env["WAVECOORD_AGENT_ID"], rec3["agent"])
            # a commit by the worker's Bash: the guard sees in-lease paths as in the lease
            for rel in ("src/in-lease.txt", "docs/ci.md"):
                os.makedirs(os.path.dirname(os.path.join(wt, rel)), exist_ok=True)
                with open(os.path.join(wt, rel), "a") as f:
                    f.write("x\n")
                subprocess.run(["git", "add", rel], cwd=wt, check=True)
            p = subprocess.run(["git", "commit", "-q", "-m", "in lease"], cwd=wt, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertNotIn("outside", p.stderr)
            self.assertNotIn("None", p.stderr)
            # the PreToolUse hook (E1) allows an edit of an in-lease path
            for rel in ("src/other.txt", "docs/ci.md"):
                inp = {"session_id": "s-" + rec3["agent"], "cwd": wt, "hook_event_name": "PreToolUse",
                       "tool_name": "Edit", "tool_input": {"file_path": os.path.join(wt, rel)}}
                out, err = io.StringIO(), io.StringIO()
                hooks.main("PreToolUse", stdin=io.StringIO(json.dumps(inp)), stdout=out, env=env, stderr=err)
                d = json.loads(out.getvalue()) if out.getvalue().strip() else {}
                self.assertNotEqual((d.get("hookSpecificOutput") or {}).get("permissionDecision"), "deny",
                                    (rel, out.getvalue(), err.getvalue()))
            # outside the lease: a violation that names the lease, never None
            with open(os.path.join(wt, "README.md"), "a") as f:
                f.write("x\n")
            subprocess.run(["git", "add", "README.md"], cwd=wt, check=True)
            p = subprocess.run(["git", "commit", "-q", "-m", "outside"], cwd=wt, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
            self.assertIn("README.md: outside_lease (no reservation of your lease #%d of task t1 covers it)"
                          % rec3["lease_id"], p.stderr)
            self.assertNotIn("None", p.stderr)
        finally:
            L.stop("t1-1")
            self.wait_state(L, "t1-1", ("reported", "failed", "timed_out", "stopped"))

    def test_new_attempt_takes_over_a_failed_parked_lease(self):
        os.environ["FAKE_CLAUDE_COMMIT"] = "1"
        os.environ["FAKE_CLAUDE_EXIT"] = "1"
        L = self.launcher()
        L.coord_url = self.url
        L.launch(self.opts(reservations=[{"glob": "src/**", "mode": "exclusive"}]))
        self.wait_state(L, "t1-1", ("reported", "failed", "timed_out"))
        self.assertEqual(self.get("/api/tasks/t1/lease")["lease"]["state"], "parked")
        os.environ.pop("FAKE_CLAUDE_EXIT")
        rec = L.launch(self.opts(reservations=[{"glob": "src/**", "mode": "exclusive"}]))
        self.assertEqual(rec["id"], "t1-2")
        self.assertTrue(rec["lease_epoch"])
        r = self.wait_state(L, "t1-2", ("reported", "failed", "timed_out"))
        self.assertEqual(r["state"], "reported", r)

    def test_continue_after_a_released_lease_claims_the_same_paths(self):
        """No pushed commits: the lease is released. The --continue run claims the paths of the earlier run."""
        os.environ["FAKE_CLAUDE_EXIT"] = "1"
        L = self.launcher()
        L.coord_url = self.url
        rec1 = L.launch(self.opts(reservations=[{"glob": "src/**", "mode": "exclusive"}]))
        self.wait_state(L, "t1-1", ("reported", "failed", "timed_out"))
        self.assertIsNone(self.get("/api/tasks/t1/lease")["lease"])
        os.environ.pop("FAKE_CLAUDE_EXIT")
        rec2 = L.launch(self.opts(prompt="Go on.", **{"continue": True}))
        self.assertNotEqual(rec2["lease_id"], rec1["lease_id"])
        live = self.get("/api/live")
        self.assertEqual([x["glob"] for x in live["reservations"] if x["lease_id"] == rec2["lease_id"]], ["src/**"])
        self.wait_state(L, "t1-1", ("reported", "failed", "timed_out"))


if __name__ == "__main__":
    unittest.main()
