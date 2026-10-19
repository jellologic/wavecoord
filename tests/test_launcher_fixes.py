"""Launcher fixes from the second review: launch cleanup, stop, uploads, the finish fetch and push,
heartbeats, worktree paths, the PR body, ssh, steering and the stuck detectors. Fakes only."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import _paths  # noqa: F401
from launcher_env import LauncherTestCase, sh

from wavecoord import gatecheck, zipapp
from wavecoord.launcher import common, hostside, stream, transport, worktrees, wrapper
from wavecoord.launcher.steer import action_body
from wavecoord.launcher.transport import SSH_OPTS, Host


def lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    except OSError:
        return []


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


FINAL = ("reported", "failed", "timed_out", "stopped")


class FixCase(LauncherTestCase):
    def setUp(self):
        super().setUp()
        self.modes = os.path.join(self.tmp, "modes.json")
        self.stdin_log = os.path.join(self.tmp, "stdin.jsonl")
        os.environ.update(FAKE_CLAUDE_MODES=self.modes, FAKE_CLAUDE_STDIN_LOG=self.stdin_log,
                          WAVECOORD_INBOX_POLL_S="0.3")
        self._saved_extra = {k: os.environ.get(k) for k in ("WAVECOORD_STEER_WAIT_S", "WAVECOORD_PREPARE_TIMEOUT_S")}

    def tearDown(self):
        for k, v in self._saved_extra.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def mode(self, task="t1", **m):
        with open(self.modes, "w") as f:
            json.dump({task: m}, f)

    def adir(self, aid="t1-1", project="demo"):
        return os.path.join(os.environ["WAVECOORD_HOME"], "attempts", project, aid)

    def run_attempt(self, L=None, **opts):
        L = L or self.launcher()
        L.launch(self.opts(**opts))
        return self.wait_state(L, "t1-1", FINAL, timeout=90)


# ---------------------------------------------------------------- 33, 39: launch

class LaunchCleanupTest(FixCase):
    def test_failed_launch_frees_its_number(self):
        # item 33: the first launch fails in prepare_worktree (origin is gone); the next one must not
        # fail forever with "attempt t1-1 already exists"
        away = self.origin + ".away"
        os.rename(self.origin, away)
        L = self.launcher()
        with self.assertRaises(common.LauncherError):
            L.launch(self.opts())
        os.rename(away, self.origin)
        self.mode(commit={"a.txt": "a\n"})
        rec = L.launch(self.opts())
        self.assertEqual(rec["id"], "t1-1")
        r = self.wait_state(L, "t1-1", FINAL)
        self.assertEqual(r["state"], "reported", r)
        failed = os.path.join(os.environ["WAVECOORD_HOME"], "attempts", "demo", ".failed")
        self.assertEqual(len(os.listdir(failed)), 1)  # kept for diagnosis, out of the list
        self.assertNotIn(".failed", [a["id"] for a in L.ls()["local"]])

    def test_existing_attempts_counts_local_branches_and_host_dirs(self):
        sh(["git", "branch", "wave/t1-7"], self.repo)
        L = self.launcher()
        self.assertIn(7, L.existing_attempts("t1"))
        self.assertIn(9, L.existing_attempts("t1", {"local": {"attempt_ids": ["t1-9", "t10-3", "x-1"]}}))
        self.assertNotIn(3, L.existing_attempts("t1", {"local": {"attempt_ids": ["t10-3"]}}))

    def test_side_by_side_repos_do_not_share_worktrees(self):
        # item 39: two projects whose repos share a parent directory launch the same task id
        from wavecoord.launcher.client import Launcher
        from wavecoord.launcher.config import Config
        other_origin = os.path.join(self.tmp, "other.git")
        sh(["git", "clone", "-q", "--bare", self.origin, other_origin], self.tmp)
        other = os.path.join(self.tmp, "other")
        sh(["git", "clone", "-q", other_origin, other], self.tmp)
        self.mode(commit={"a.txt": "a\n"})
        A = self.launcher()
        raw = {"project": "other", "branch_prefix": "wave", "hosts": [{"name": "local", "slots": 2, "repo_path": other}]}
        B = Launcher(Config(raw, other), coord_url=self.coord.url)
        ra = A.launch(self.opts())
        rb = B.launch(self.opts())
        self.assertNotEqual(ra["worktree"], rb["worktree"])
        self.assertTrue(ra["worktree"].endswith(os.path.join("wt", "demo", "t1-1")), ra["worktree"])
        self.assertTrue(rb["worktree"].endswith(os.path.join("wt", "other", "t1-1")), rb["worktree"])
        self.assertIn(self.wait_state(A, "t1-1", FINAL)["state"], ("reported", "failed"))
        self.wait_state(B, "t1-1", FINAL)


# ---------------------------------------------------------------- 34, 37: stop and heartbeats

class StopAndHeartbeatTest(FixCase):
    def test_stop_during_prepare_kills_it_and_reports(self):
        # items 34 and 37: a long prepare_cmd gets heartbeats; a stop ends it at once, and the wrapper
        # (not r_stop) records `stopped` and posts the report and the finish
        hosts = [{"name": "local", "slots": 1, "repo_path": self.repo, "prepare_cmd": "sleep 60"}]
        L = self.launcher(hosts=hosts)
        L.launch(self.opts())
        time.sleep(2.5)
        hb = [p for p in self.coord.posts("/api/hb")]
        self.assertGreaterEqual(len(hb), 3, "heartbeats during prepare_cmd")
        t = time.time()
        out = L.stop("t1-1")
        self.assertEqual(out["state"], "stopped", out)
        self.assertLess(time.time() - t, 30)
        self.assertTrue(self.coord.posts("/api/attempts/t1/1/report"))
        self.assertEqual(self.coord.posts("/api/agents/")[-1]["body"]["state"], "stopped")

    def test_held_run_lock_keeps_the_slot_and_the_worktree(self):
        # item 34: a status that says `stopped` while the wrapper still holds its run lock
        d = self.adir()
        os.makedirs(d)
        wt = os.path.join(self.tmp, "wt-x")
        sh(["git", "worktree", "add", "-q", "-b", "wave/t1-1", wt], self.repo)
        common.write_json(os.path.join(d, "task.json"), {"project": "demo", "host": "local", "task": "t1",
                                                          "attempt": 1, "worktree": wt, "repo": self.repo,
                                                          "branch": "wave/t1-1"})
        common.write_json(os.path.join(d, "status.json"), {"state": "running", "host": "local"})
        with common.Flock(os.path.join(d, hostside.RUN_LOCK)):
            out = hostside.r_stop({"project": "demo", "wait_s": 0.5}, ["t1-1"])
            self.assertTrue(out.get("stopping"), out)
            self.assertEqual(common.read_json(os.path.join(d, "status.json"))["state"], "running")
            common.update_status(d, state="stopped")  # whatever the status says
            self.assertEqual(hostside.count_active("local"), 1)
            gc = hostside.r_gc({"project": "demo", "host": {"name": "local"}}, [])
            self.assertEqual(gc["removed"], [])
            self.assertTrue(os.path.isdir(wt))
        self.assertEqual(hostside.count_active("local"), 0)

    def test_finish_failure_still_posts_report_and_finish(self):
        # item 34: finish() raises (here: the worktree is gone); the fallback posts a failed report
        self.mode(sleep=2)
        L = self.launcher()
        rec = L.launch(self.opts())
        time.sleep(0.8)
        shutil.rmtree(rec["worktree"])
        r = self.wait_state(L, "t1-1", FINAL)
        self.assertEqual(r["state"], "failed", r)
        self.assertIn("finish failed", r["note"])
        rep = self.coord.posts("/api/attempts/t1/1/report")
        self.assertEqual(rep[-1]["body"]["state"], "failed")
        self.assertEqual(self.coord.posts("/api/agents/")[-1]["body"]["state"], "failed")


# ---------------------------------------------------------------- 35, 38, 41, 42: transport

class TransportTest(FixCase):
    def setUp(self):
        super().setUp()
        self.remote_home = os.path.join(self.tmp, "remote-home")
        os.environ["FAKE_SSH_HOME"] = self.remote_home
        for k in ("FAKE_SSH_SHELL", "FAKE_SSH_CUT"):
            self.addCleanup(os.environ.pop, k, None)

    def host(self):
        return Host({"name": "build-1", "ssh": "builder@build-1.example.invalid", "slots": 2, "repo_path": self.repo})

    def build(self):
        return os.path.join(self.remote_home, "bin", "wavecoord-" + zipapp.self_hash())

    def test_cut_upload_never_takes_the_build_name(self):
        # item 35
        os.environ["FAKE_SSH_CUT"] = str(len(zipapp.self_bytes()) // 2)
        with self.assertRaises(common.LauncherError):
            self.host().call("probe", {"project": "demo"})
        self.assertFalse(os.path.exists(self.build()))
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.build())) if ".tmp." in n], [])
        os.environ.pop("FAKE_SSH_CUT")
        self.assertTrue(self.host().call("probe", {"project": "demo"})["ok"])

    def test_commands_touch_their_build(self):
        # item 38: the 30-day cleanup goes by mtime; a build in use is touched by every command
        h = self.host()
        h.call("probe", {"project": "demo"})
        old = time.time() - 40 * 86400
        os.utime(self.build(), (old, old))
        h.call("probe", {"project": "demo"})
        self.assertGreater(os.path.getmtime(self.build()), time.time() - 600)

    @unittest.skipUnless(os.path.exists("/bin/csh"), "no /bin/csh")
    def test_csh_login_shell(self):
        # item 42: the scripts run under `sh -c`, whatever the login shell is
        os.environ["FAKE_SSH_SHELL"] = "/bin/csh"
        out = self.host().call("probe", {"project": "demo"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(os.path.exists(self.build()))

    def test_scripts_go_through_sh_c(self):
        h = self.host()
        self.assertTrue(h.argv("probe")[-1].startswith("sh -c '"))
        self.assertTrue(transport.sh_c("echo 'x'").startswith("sh -c "))

    def test_tunnel_overrides_come_first(self):
        # item 41: ssh keeps the first value of an option
        calls = []
        L = self.launcher(hosts=[{"name": "build-1", "ssh": "builder@build-1.example.invalid", "slots": 1,
                                  "repo_path": self.repo}])
        L.coord_url = "http://127.0.0.1:18741"
        orig = subprocess.run
        subprocess.run = lambda argv, **kw: calls.append(argv)
        try:
            L.ensure_tunnel(L.hosts["build-1"], {"coord_reachable": False})
        finally:
            subprocess.run = orig
        argv = calls[0]
        self.assertLess(argv.index("ControlMaster=no"), argv.index("ControlMaster=auto"))
        self.assertLess(argv.index("ControlPath=none"), argv.index("ControlPath=~/.ssh/wavecoord-%C"))
        real = shutil.which("ssh", path="/usr/bin:/bin:/usr/local/bin")
        if real:  # what ssh itself makes of the options (no connection: -G only prints the config)
            p = orig([real, "-G"] + argv[1:argv.index("-f")] + ["builder@build-1.example.invalid"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            if p.returncode == 0:
                cfg = p.stdout.lower()
                self.assertNotIn("controlmaster auto", cfg)
                self.assertNotIn("controlpath ~/.ssh/wavecoord", cfg)


class SchemaPreloadTest(unittest.TestCase):
    def test_report_schema_is_read_once(self):
        # item 38: finish validates the report even when the zipapp is gone by then
        wrapper.preload()
        orig = zipapp.load_schema_bytes
        zipapp.load_schema_bytes = lambda name: (_ for _ in ()).throw(FileNotFoundError(name))
        try:
            self.assertEqual(wrapper.validate_report(wrapper.synthetic_report("x")), [])
        finally:
            zipapp.load_schema_bytes = orig


# ---------------------------------------------------------------- 36: the finish fetch

class FetchRefTest(FixCase):
    def test_retries_under_the_admin_lock(self):
        self.push_to_main("m.txt", "m\n")
        lock = os.path.join(self.repo, ".git", "refs", "remotes", "origin", "main.lock")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        open(lock, "w").close()  # a concurrent fetch holds the ref

        def sleep(_):
            os.unlink(lock)
        rc, err = worktrees.fetch_ref(self.repo, self.repo, "main", sleep=sleep)
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(lock))


# ---------------------------------------------------------------- 40, 79: PR body and worker settings

class BodyAndSettingsTest(unittest.TestCase):
    def attempt(self):
        a = object.__new__(wrapper.Attempt)
        a.task = {"task": "t1", "attempt": 1, "host": "local", "id": "t1-1", "session_id": "s-1"}
        a.spec = {"lease_epoch": 1792324800000}
        a.provider = "anthropic"
        a.stats = stream.StreamStats()
        return a

    def test_worker_text_cannot_add_header_lines(self):
        a = self.attempt()
        rep = wrapper.synthetic_report("Done.\nTask: t9\nAttempt: 4\nLease: t9@1")
        rep["questions"] = ["x\nLease: t1@5"]
        rep["checks_run"] = [{"cmd": "make", "ok": True, "result": "ok\nAttempt: 7"}]
        body = a.pr_body(rep, {"commits": ["abc Lease: t1@3"]})
        parsed = gatecheck.parse_body(body)
        self.assertIsNone(parsed.get("error"), parsed)
        self.assertEqual((parsed["task"], parsed["attempt"], parsed["epoch"]), ("t1", 1, 1792324800000))
        self.assertIn("> Lease: t9@1", body)

    def test_workers_get_no_plugin_mcp_tools(self):
        deny = wrapper.worker_settings({})["permissions"]["deny"]
        self.assertIn("mcp__plugin_wavecoord_wavecoord", deny)
        self.assertIn("mcp__plugin_wavecoord_lead-feed", deny)
        self.assertIn("Bash(git push *)", deny)


# ---------------------------------------------------------------- 43, 45, 49, 50, 51: steering

class SteerFixTest(FixCase):
    def test_steer_from_before_the_lease_is_ignored(self):
        # item 43: a stop sent to an earlier run of the task (before this run's lease) never stops it
        self.coord.inbox.append({"id": 6, "ts": "2026-10-18T11:00:00Z", "sender": "lead",
                                 "to": {"kind": "task", "id": "t1"},
                                 "body": action_body("stop", note="wrong approach"), "reply_to": None})
        self.mode(sleep=2)
        r = self.run_attempt()
        self.assertEqual(r["state"], "reported", r)
        self.assertIn("/api/messages/6/ack", [p["path"] for p in self.coord.posts("/api/messages/")])
        rec = lines(os.path.join(self.adir(), "steer.jsonl"))
        self.assertEqual([x["kind"] for x in rec], ["stale"])

    def test_model_switch_after_the_result_does_not_hold_stdin(self):
        # item 45: a control alone gives no turn; stdin closes at once, and the run ends reported
        gate = os.path.join(self.tmp, "result-written")
        self.coord.inbox_gate = gate
        self.mode(mark_result=gate)
        self.coord.message(9, action_body("model", model="opus"))
        os.environ["WAVECOORD_STEER_WAIT_S"] = "3"
        t = time.time()
        r = self.run_attempt(timeout_min=0.5)
        self.assertEqual(r["state"], "reported", r)
        self.assertLess(time.time() - t, 25)

    def test_interrupt_without_note_is_one_more_turn(self):
        # item 49: an interrupt stops the turn, not the run
        self.coord.message(3, action_body("interrupt"))
        self.mode(wait_steer=1)
        r = self.run_attempt()
        self.assertEqual(r["state"], "reported", r)
        sent = lines(self.stdin_log)
        self.assertEqual([l["type"] for l in sent], ["control_request", "user"])
        self.assertIn("(no note) The lead interrupted your last step", sent[1]["message"]["content"])

    def test_a_failing_steered_turn_keeps_the_report(self):
        # item 50: the hint's extra turn ends with an error; the worker's report stands
        gate = os.path.join(self.tmp, "result-written")
        self.coord.inbox_gate = gate
        self.mode(mark_result=gate, extra_turn_error=True, commit={"a.txt": "a\n"})
        self.coord.message(8, "One more thing.")
        r = self.run_attempt()
        self.assertEqual(r["state"], "reported", r)
        self.assertEqual(r["late_turn"]["subtype"], "error_during_execution")
        self.assertTrue(r.get("pr_url"), r)

    def test_failed_control_is_reported(self):
        # item 51: an error control_response of a lead action goes to the lead
        self.coord.message(4, action_body("model", model="no-such-model"))
        self.mode(sleep=3, control_error=["set_model"])
        r = self.run_attempt()
        self.assertEqual(r["state"], "reported", r)
        notes = [n["body"] for n in self.coord.posts("/api/notify") if n["body"]["detail"].get("event") == "steer_failed"]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("set_model", notes[0]["text"])
        self.assertIn("fake error", notes[0]["text"])
        rec = lines(os.path.join(self.adir(), "steer.jsonl"))
        self.assertIn("control_failed", [x["kind"] for x in rec])


# ---------------------------------------------------------------- 66, 100: the branch is already pushed

class AlreadyPushedTest(FixCase):
    def test_worker_push_still_gets_the_push_checks(self):
        # item 100: E3 runs and the report has the E4 fields, although the worker pushed itself
        self.mode(commit={"a.txt": "a\n"}, push=True)
        r = self.run_attempt()
        self.assertEqual(r["state"], "reported", r)
        self.assertEqual(r["already_pushed"], "worker")
        self.assertEqual(r["changed_paths"], ["a.txt"])
        body = self.coord.posts("/api/attempts/t1/1/report")[-1]["body"]
        self.assertEqual(body["changed_paths"], ["a.txt"])

    def test_resolver_that_merges_nothing_is_not_clean(self):
        # item 66: a --continue run that adds no commit still gets the merge-tree check
        self.mode(commit={"x.txt": "mine\n"})
        L = self.launcher()
        L.launch(self.opts())
        r = self.wait_state(L, "t1-1", FINAL)
        self.assertEqual(r["state"], "reported", r)
        self.push_to_main("x.txt", "theirs\n")
        self.mode()  # the resolver does nothing
        L.launch(self.opts(**{"continue": True}))
        time.sleep(1)
        r = self.wait_state(L, "t1-1", FINAL)
        self.assertEqual(r["already_pushed"], "earlier_run", r)
        self.assertEqual(r["conflicts"], ["x.txt"])


# ---------------------------------------------------------------- 52: the stuck detectors

class StuckTest(unittest.TestCase):
    def feed(self, st, *objs):
        out = []
        for o in objs:
            out += st.feed(json.dumps(o))
        return out + st.flush()

    def msg(self, mid, *calls):
        return {"type": "assistant", "parent_tool_use_id": None, "message": {"id": mid, "content": [
            {"type": "tool_use", "id": "%s-%d" % (mid, i), "name": n, "input": inp} for i, (n, inp) in enumerate(calls)]}}

    def result(self, tid, error):
        return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid,
                                                          "is_error": error}]}}

    def test_parallel_greps_and_chunked_reads_are_not_a_loop(self):
        st = stream.StreamStats()
        grep = [("Grep", {"pattern": "def main", "path": p}) for p in ("a", "b", "c")]
        reads = [("Read", {"file_path": "/w/big.py", "offset": o, "limit": 500}) for o in (1, 501, 1001)]
        ev = self.feed(st, self.msg("m1", *grep), self.msg("m2", *reads[:1]), self.msg("m3", *reads[1:2]),
                       self.msg("m4", *reads[2:]))
        self.assertEqual(ev, [])

    def test_one_message_per_line_is_one_step(self):
        # Claude Code streams one line per content block of a message
        st = stream.StreamStats()
        call = ("Bash", {"command": "ls"})
        ev = self.feed(st, self.msg("m1", call), self.msg("m1", call), self.msg("m1", call))
        self.assertEqual(ev, [])
        ev = self.feed(st, self.msg("m2", call, call, call), self.msg("m3", call, call, call))
        self.assertEqual([e[1]["why"] for e in ev], ["loop"])

    def test_red_green_cycle_is_not_failing(self):
        st = stream.StreamStats()
        test = ("Bash", {"command": "python3 -m unittest"})
        edit = ("Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"})
        seq = []
        for i in range(4):
            seq += [self.msg("t%d" % i, test), self.result("t%d-0" % i, True), self.msg("e%d" % i, edit)]
        self.assertEqual([e for e in self.feed(st, *seq) if e[1]["why"] == "failing"], [])
        st = stream.StreamStats()
        seq = [self.msg("a", test), self.result("a-0", True), self.msg("b", test), self.result("b-0", False),
               self.msg("c", test), self.result("c-0", True), self.msg("d", test), self.result("d-0", True)]
        self.assertEqual([e for e in self.feed(st, *seq) if e[1]["why"] == "failing"], [])


if __name__ == "__main__":
    unittest.main()
