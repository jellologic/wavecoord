"""Launcher end to end on a local host (ssh null) with the fake claude, a bare remote,
a fake pr_cmd and a fake coordinator."""

import json
import os
import subprocess
import sys
import unittest

import _paths  # noqa: F401
from launcher_env import LauncherTestCase, sh

from wavecoord import jsonschema_lite
from wavecoord.launcher.common import LauncherError
from wavecoord.launcher.wrapper import report_schema


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class LocalEndToEnd(LauncherTestCase):
    def test_success_push_pr_report(self):
        os.environ["FAKE_CLAUDE_COMMIT"] = "1"
        dump = os.path.join(self.tmp, "dump.json")
        os.environ["FAKE_CLAUDE_DUMP"] = dump
        L = self.launcher()
        rec = L.launch(self.opts(model="sonnet", effort="high", max_turns=5, max_budget_usd=1.5,
                                 fallback_model="haiku"))
        self.assertEqual((rec["id"], rec["branch"], rec["host"]), ("t1-1", "wave/t1-1", "local"))
        self.assertEqual(rec["lease_epoch"], self.coord.epoch)
        r = self.wait_state(L, "t1-1", ("reported", "failed", "timed_out"))
        self.assertEqual(r["state"], "reported", r)

        # worktree at <repo>/../wt/<attempt>, on the attempt branch
        wt = os.path.join(os.path.dirname(self.repo), "wt", "t1-1")
        self.assertEqual(r["worktree"], wt)
        self.assertEqual(sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], wt), "wave/t1-1")

        # pushed to the bare remote, non-force; report commits are the pushed truth
        self.assertIn("wave/t1-1", self.remote_branches())
        head = sh(["git", "rev-parse", "refs/heads/wave/t1-1"], self.origin)
        self.assertTrue(r["pushed"])
        self.assertEqual(r["commits"], [head])
        self.assertEqual(r["conflicts"], [])

        # claude was run with the documented flags, prompt on stdin, the right env
        d = load(dump)
        argv = d["argv"]
        for flag in ("-p", "--verbose", "--include-hook-events"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertEqual(argv[argv.index("--session-id") + 1], rec["session_id"])
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), report_schema())
        for flag, val in (("--model", "sonnet"), ("--effort", "high"), ("--max-turns", "5"),
                          ("--max-budget-usd", "1.5"), ("--fallback-model", "haiku")):
            self.assertEqual(argv[argv.index(flag) + 1], val)
        plugin = argv[argv.index("--plugin-dir") + 1]
        self.assertTrue(os.path.isfile(os.path.join(plugin, ".claude-plugin", "plugin.json")))
        self.assertTrue(os.path.basename(plugin).startswith("plugin-"))
        settings = load(argv[argv.index("--settings") + 1])
        self.assertIn("Bash(git push *)", settings["permissions"]["deny"])
        self.assertIn("wave/t1-1", argv[argv.index("--append-system-prompt") + 1])
        self.assertEqual(d["stdin"], "Add a file.\nMore detail.")
        self.assertEqual(os.path.realpath(d["cwd"]), os.path.realpath(wt))
        env = d["env"]
        self.assertEqual(env["WAVECOORD_URL"], self.coord.url)
        self.assertEqual(env["WAVECOORD_AGENT_ID"], rec["agent"])
        self.assertEqual((env["WAVECOORD_TASK"], env["WAVECOORD_ATTEMPT"]), ("t1", "1"))
        self.assertEqual((env["WAVECOORD_LEASE_ID"], env["WAVECOORD_LEASE_EPOCH"]), ("7", str(self.coord.epoch)))
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
            self.assertNotIn(k, env)

        # PR through pr_cmd with the lease lines and the summary
        prs = self.prs()
        self.assertEqual(len(prs), 1)
        args = prs[0]["args"]
        self.assertEqual(args[args.index("--head") + 1], "wave/t1-1")
        self.assertEqual(args[args.index("--base") + 1], "main")
        body = prs[0]["body"]
        for line in ("Task: t1", "Attempt: 1", "Lease: t1@%d" % self.coord.epoch, "fake claude handled: Add a file."):
            self.assertIn(line, body)
        self.assertEqual(r["pr_url"], "https://example.invalid/pull/1")

        # coordinator: claim, heartbeat, a schema-valid report with idem, finish
        claim = self.coord.posts("/api/leases")[0]["body"]
        self.assertEqual((claim["task"], claim["attempt"], claim["holder"], claim["host"]),
                         ("t1", 1, rec["agent"], "local"))
        self.assertTrue(self.coord.posts("/api/hb"))
        hb = self.coord.posts("/api/hb")[0]
        self.assertEqual(hb["headers"].get("X-Wavecoord-Agent"), rec["agent"])
        self.assertEqual(hb["body"]["source"], "launcher")
        reps = self.coord.posts("/api/attempts/t1/1/report")
        self.assertEqual(len(reps), 1)
        body = reps[0]["body"]
        self.assertEqual(jsonschema_lite.validate(body["report"], report_schema()), [])
        self.assertEqual(body["report"]["status"], "done")
        self.assertEqual(reps[0]["headers"].get("Idempotency-Key"), "report:t1:1")
        self.assertEqual((body["provider"], body["cost_basis"]), ("anthropic", "anthropic"))
        self.assertTrue(body["model_usage"])
        self.assertEqual(body["state"], "reported")
        fin = self.coord.posts("/api/agents/%s/finish" % rec["agent"])
        self.assertEqual(fin[0]["body"]["state"], "done")
        self.assertTrue(fin[0]["body"]["pushed"])

        # gc removes the finished, pushed and clean worktree
        out = L.gc()
        self.assertEqual([x["id"] for x in out["local"]["removed"]], ["t1-1"])
        self.assertFalse(os.path.exists(wt))

        # the next attempt number is 2 (the branch exists on origin)
        self.assertEqual(L.existing_attempts("t1"), {1})

    def test_rebase_when_main_moved(self):
        os.environ["FAKE_CLAUDE_COMMIT"] = "1"
        os.environ["FAKE_CLAUDE_SLEEP"] = "1.5"
        L = self.launcher()
        L.launch(self.opts())
        self.push_to_main("other.txt", "main moved\n")
        r = self.wait_state(L, "t1-1", ("reported", "failed", "timed_out"))
        self.assertEqual(r["state"], "reported", r)
        self.assertIn("rebased", r)
        main = sh(["git", "rev-parse", "refs/heads/main"], self.origin)
        sh(["git", "merge-base", "--is-ancestor", main, "refs/heads/wave/t1-1"], self.origin)
        self.assertEqual(len(self.prs()), 1)

    def test_conflict_no_pr(self):
        os.environ["FAKE_CLAUDE_COMMIT"] = "1"
        os.environ["FAKE_CLAUDE_SLEEP"] = "1.5"
        L = self.launcher()
        rec = L.launch(self.opts())
        # the fake writes fake-claude-<session[:8]>.txt; main gets the same file first
        self.push_to_main("fake-claude-%s.txt" % rec["session_id"][:8], "conflicting\n")
        r = self.wait_state(L, "t1-1", ("reported", "failed", "timed_out"))
        self.assertEqual(r["state"], "reported", r)
        self.assertTrue(r["pushed"])
        self.assertEqual(r["conflicts"], ["fake-claude-%s.txt" % rec["session_id"][:8]])
        self.assertIn("conflicts", r["pr_error"])
        self.assertEqual(self.prs(), [])
        self.assertTrue([p for p in self.coord.posts("/api/notify") if p["body"]["detail"]["event"] == "conflict"])

    def test_push_rejected_is_failed(self):
        hook = os.path.join(self.origin, "hooks", "pre-receive")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\necho refused by test >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        os.environ["FAKE_CLAUDE_COMMIT"] = "1"
        L = self.launcher()
        L.launch(self.opts())
        r = self.wait_state(L, "t1-1", ("reported", "failed", "timed_out"))
        self.assertEqual(r["state"], "failed", r)
        self.assertFalse(r["pushed"])
        self.assertIn("push failed", r["note"])
        self.assertEqual(self.prs(), [])
        rep = self.coord.posts("/api/attempts/t1/1/report")[0]["body"]["report"]
        self.assertEqual(rep["status"], "failed")
        self.assertEqual(jsonschema_lite.validate(rep, report_schema()), [])
        # gc keeps a worktree with unpushed commits
        out = L.gc()
        self.assertEqual(out["local"]["removed"], [])
        self.assertIn("unpushed", out["local"]["kept"][0]["why"])

    def test_timeout(self):
        os.environ["FAKE_CLAUDE_SLEEP"] = "60"
        L = self.launcher()
        L.launch(self.opts(timeout_min=0.02))
        r = self.wait_state(L, "t1-1", ("reported", "failed", "timed_out", "stopped"), timeout=40)
        self.assertEqual(r["state"], "timed_out", r)
        self.assertEqual(r["reason"], "timeout")
        rep = self.coord.posts("/api/attempts/t1/1/report")[0]["body"]["report"]
        self.assertEqual(rep["status"], "failed")

    def test_invalid_report(self):
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w") as f:
            json.dump({"status": "great", "summary": ""}, f)
        os.environ["FAKE_CLAUDE_REPORT"] = bad
        L = self.launcher()
        L.launch(self.opts())
        r = self.wait_state(L, "t1-1", ("reported", "failed", "timed_out"))
        self.assertEqual(r["state"], "failed", r)
        self.assertTrue(r["report_errors"])
        rep = self.coord.posts("/api/attempts/t1/1/report")[0]["body"]["report"]
        self.assertEqual(rep["status"], "failed")
        self.assertIn("report invalid", rep["summary"])
        self.assertEqual(jsonschema_lite.validate(rep, report_schema()), [])

    def test_branch_exists(self):
        sh(["git", "push", "-q", "origin", "main:refs/heads/wave/t1-1"], self.seed)
        L = self.launcher()
        with self.assertRaises(LauncherError) as cm:
            L.launch(self.opts(attempt=1, no_claim=True))
        self.assertIn("already exists", str(cm.exception))
        self.assertEqual(L.existing_attempts("t1"), {1})

    def test_branch_checked_out_elsewhere(self):
        sh(["git", "branch", "wave/t1-1", "origin/main"], self.repo)
        sh(["git", "worktree", "add", "-q", os.path.join(self.tmp, "other"), "wave/t1-1"], self.repo)
        L = self.launcher()
        with self.assertRaises(LauncherError) as cm:
            L.launch(self.opts(attempt=1, no_claim=True, **{"continue": True}))
        self.assertIn("already checked out", str(cm.exception))

    def test_continue_resumes_session(self):
        os.environ["FAKE_CLAUDE_COMMIT"] = "1"
        L = self.launcher()
        rec = L.launch(self.opts())
        self.wait_state(L, "t1-1", ("reported",))
        dump = os.path.join(self.tmp, "dump2.json")
        os.environ["FAKE_CLAUDE_DUMP"] = dump
        rec2 = L.launch(self.opts(prompt="Fix the review comments.", **{"continue": True}))
        self.assertEqual(rec2["id"], "t1-1")
        r = self.wait_state(L, "t1-1", ("reported", "failed", "timed_out"))
        self.assertEqual(r["state"], "reported", r)
        argv = load(dump)["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], rec["session_id"])
        self.assertNotIn("--session-id", argv)
        self.assertEqual(len(r["commits"]), 2)
        self.assertEqual(len(self.prs()), 1)  # the PR from the first run is kept
        self.assertEqual(r["pr_url"], "https://example.invalid/pull/1")

    def test_prepare_cmd_failure(self):
        dump = os.path.join(self.tmp, "dump.json")
        os.environ["FAKE_CLAUDE_DUMP"] = dump
        L = self.launcher(hosts=[{"name": "local", "slots": 1, "repo_path": self.repo,
                                  "prepare_cmd": "echo preparing $WAVECOORD_TASK; exit 3"}])
        L.launch(self.opts())
        r = self.wait_state(L, "t1-1", ("reported", "failed", "timed_out"))
        self.assertEqual(r["state"], "failed")
        self.assertIn("prepare_cmd failed (exit 3)", r["note"])
        self.assertFalse(os.path.exists(dump))

    def test_api_retry_notify_and_spool(self):
        ev = os.path.join(self.tmp, "events.jsonl")
        with open(ev, "w") as f:
            f.write(json.dumps({"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 10,
                                "retry_delay_ms": 500, "error_status": 429, "error": "rate_limit"}) + "\n")
            f.write(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected",
                                                                                "rateLimitType": "five_hour"}}) + "\n")
        os.environ["FAKE_CLAUDE_EVENTS"] = ev
        L = self.launcher()
        L.launch(self.opts())
        r = self.wait_state(L, "t1-1", ("reported", "failed", "timed_out"))
        self.assertEqual(r["state"], "reported", r)
        self.assertEqual(r["api_retries"], 1)
        notes = self.coord.posts("/api/notify")
        self.assertEqual(sorted(p["body"]["detail"]["event"] for p in notes), ["api_retry", "rate_limit"])
        self.assertEqual([p["body"]["kind"] for p in notes], ["rate_limit", "rate_limit"])

    def test_unreachable_coord_spools_report(self):
        L = self.launcher(coord=False)
        L.coord_url = "http://127.0.0.1:9"  # nothing listens
        L.launch(self.opts(no_claim=True))
        r = self.wait_state(L, "t1-1", ("reported", "failed", "timed_out"))
        self.assertEqual(r["state"], "reported", r)
        self.assertEqual(r["report_post"], "spooled")
        spool = os.path.join(os.environ["WAVECOORD_HOME"], "spool")
        recs = [load(os.path.join(spool, n)) for n in os.listdir(spool) if n.endswith(".json")]
        paths = sorted(x["path"] for x in recs)
        self.assertIn("/api/attempts/t1/1/report", paths)
        rep = [x for x in recs if x["path"].endswith("/report")][0]
        self.assertEqual(rep["idem"], "report:t1:1")

    def test_lease_lost_stops_worker(self):
        os.environ["FAKE_CLAUDE_SLEEP"] = "60"
        self.coord.lost = [{"lease_id": 7, "task": "t1", "epoch": self.coord.epoch, "reason": "steal", "by": "lead"}]
        L = self.launcher()
        L.launch(self.opts())
        r = self.wait_state(L, "t1-1", ("reported", "failed", "timed_out", "stopped"), timeout=40)
        self.assertEqual(r["state"], "failed", r)
        self.assertIn("lease lost", r["note"])

    def test_stop(self):
        os.environ["FAKE_CLAUDE_SLEEP"] = "60"
        L = self.launcher()
        L.launch(self.opts())
        out = L.stop("t1-1")
        self.assertEqual(out["state"], "stopped")
        fin = self.coord.posts("/api/agents/")
        self.assertEqual(fin[-1]["body"]["state"], "stopped")

    def test_claim_refused(self):
        self.coord.claim_status = 409
        L = self.launcher()
        with self.assertRaises(LauncherError) as cm:
            L.launch(self.opts())
        self.assertIn("refused", str(cm.exception))

    def test_slots_full(self):
        os.environ["FAKE_CLAUDE_SLEEP"] = "60"
        L = self.launcher(hosts=[{"name": "local", "slots": 1, "repo_path": self.repo}])
        L.launch(self.opts(no_claim=True))
        with self.assertRaises(LauncherError) as cm:
            L.launch(self.opts(task="t2", no_claim=True))
        self.assertIn("full", str(cm.exception))
        rows = L.ls()["local"]
        self.assertEqual([(x["id"], x["state"]) for x in rows], [("t1-1", "running")])
        L.stop("t1-1")


class CliEndToEnd(LauncherTestCase):
    ZIPAPP = os.path.join(_paths.ROOT, "plugin", "bin", "wavecoord")

    def wc(self, *args, **kw):
        env = dict(os.environ, WAVECOORD_URL=self.coord.url)
        p = subprocess.run([sys.executable, self.ZIPAPP] + list(args), cwd=self.repo, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120, **kw)
        return p

    def test_cli_launch_wait_and_attempt_commands(self):
        os.makedirs(os.path.join(self.repo, ".claude"))
        with open(os.path.join(self.repo, ".claude", "wavecoord.json"), "w") as f:
            json.dump({"project": "demo", "hosts": [{"name": "local", "slots": 2, "repo_path": "."}],
                       "pr_cmd": [self.pr_script, "--head", "{branch}", "--body-file", "{body_file}"]}, f)
        prompt = os.path.join(self.tmp, "prompt.md")
        with open(prompt, "w") as f:
            f.write("Do the CLI task.\n")
        os.environ["FAKE_CLAUDE_COMMIT"] = "1"
        p = self.wc("launch", "--task", "cli", "--prompt-file", prompt, "--wait", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        res = json.loads(p.stdout)
        self.assertEqual((res["id"], res["state"], res["report_status"]), ("cli-1", "reported", "done"))

        p = self.wc("attempts", "ls", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual([(r["id"], r["state"]) for r in json.loads(p.stdout)], [("cli-1", "reported")])
        p = self.wc("attempts", "result", "cli-1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("cli-1 on local: reported", p.stdout)
        p = self.wc("attempts", "logs", "cli-1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("[result success", p.stdout)
        self.assertIn("finished: reported", p.stdout)
        p = self.wc("hosts", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["local"]["free"], 2)
        p = self.wc("gc", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("would remove", p.stdout)
        p = self.wc("attempts", "stop", "cli-1")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_cli_launch_errors(self):
        p = self.wc("launch", "--task", "x")
        self.assertEqual(p.returncode, 1)
        self.assertIn("--prompt-file", p.stderr)
        p = self.wc("attempts", "result", "nope-1")
        self.assertEqual(p.returncode, 1)
        self.assertIn("no attempt", p.stderr)


if __name__ == "__main__":
    unittest.main()
