"""The text output of the lead CLI: tables, refinery lines, the hint delay and the gate-check verdict."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import _paths
from wavecoord import gatecheck, leadcli
from wavecoord.launcher import cli as lcli


class TableTest(unittest.TestCase):
    def test_columns_fit_the_content(self):
        rows = lcli._table([("ATTEMPT", "HOST", "NOTE"), ("notes.search.words.index-1", "local", ""),
                            ("a-1", "remote-host", "https://example.invalid/pull/1")])
        self.assertEqual(rows, ["ATTEMPT                     HOST         NOTE",
                                "notes.search.words.index-1  local",
                                "a-1                         remote-host  https://example.invalid/pull/1"])


class RefineryLineTest(unittest.TestCase):
    def test_merged_has_no_trailing_space(self):
        self.assertEqual(leadcli._merge_line({"task": "a", "attempt": 1, "pr": 3, "state": "merged"}), "a/1 #3: merged")

    def test_blocked_shows_a_short_detail(self):
        r = {"task": "t", "attempt": 1, "pr": 2, "state": "blocked", "reason": "report_not_done",
             "detail": "report status is blocked, not done; retry it (`wavecoord launch t --continue`), ask the user,"
                       " or close the PR: the refinery merges only a done report"}
        self.assertEqual(leadcli._merge_line(r), "t/1 #2: blocked report_not_done (report status is blocked, not done)")
        r = dict(r, reason="check_failed", detail="\nFAILED test_x\nmore output\n")
        self.assertEqual(leadcli._merge_line(r), "t/1 #2: blocked check_failed (FAILED test_x)")
        r = dict(r, reason="conflict", detail=None, files=["a.py", "b.py"])
        self.assertEqual(leadcli._merge_line(r), "t/1 #2: blocked conflict (files: a.py, b.py)")
        r = dict(r, reason="branch_missing", files=None)
        self.assertEqual(leadcli._merge_line(r), "t/1 #2: blocked branch_missing")
        long = "x" * 300
        self.assertEqual(len(leadcli.short_detail({"detail": long})), leadcli.DETAIL_MAX)
        self.assertTrue(leadcli.short_detail({"detail": long}).endswith("..."))


class HintDelayTest(unittest.TestCase):
    def test_within(self):
        for s, want in ((0.25, "within a few seconds"), (0, "within a few seconds"), (2, "within a few seconds"),
                        (4.5, "within about 4.5s"), (5, "within about 5s"), (30.0, "within about 30s"),
                        (300, "within about 5m"), ("x", "soon")):
            self.assertEqual(leadcli.within(s), want, s)

    def test_hint_cli_with_a_sub_second_poll(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = json.dumps({"message": {"id": 7}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            env = dict(os.environ, PYTHONPATH=_paths.SRC, WAVECOORD_URL="http://127.0.0.1:%d" % srv.server_port,
                       CLAUDE_PLUGIN_OPTION_TOKEN="t" * 32, WAVECOORD_INBOX_POLL_S="0.5")
            p = subprocess.run([sys.executable, "-m", "wavecoord", "hint", "demo", "use the cache"],
                               cwd=tempfile.gettempdir(), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, timeout=60)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "hint #7 to task demo: the running worker reads it within a few seconds\n")


class GateVerdictTest(unittest.TestCase):
    def test_summary(self):
        res = {"verdict": "fail", "paths": 2, "detail": "1 violation(s) in 2 path(s)",
               "violations": [{"kind": "outside_lease", "path": "README.md", "task": "t", "lease_id": 1}]}
        self.assertEqual(gatecheck.human_verdict(res), "fail: 1 path outside the lease: README.md")
        res["violations"] = [{"kind": "outside_lease", "path": "f%d" % i} for i in range(7)] + [
            {"kind": "config_change", "path": ".claude/wavecoord.json", "key": ".claude/wavecoord.json: fence"}]
        self.assertEqual(gatecheck.human_verdict(res),
                         "fail: 7 paths outside the lease: f0, f1, f2, f3, f4 and 2 more; 1 lead config change:"
                         " .claude/wavecoord.json: fence; " + gatecheck.CONFIG_ADVICE)
        self.assertEqual(gatecheck.human_verdict({"verdict": "pass", "paths": 1, "seq": 9, "violations": []}),
                         "pass: 1 path checked against the lease at seq 9")
        self.assertEqual(gatecheck.human_verdict({"verdict": "error", "detail": "git diff x failed"}),
                         "error: git diff x failed")

    def test_stdout_and_stderr(self):
        # an error before any check: the verdict goes to stdout, and to stderr too (another file)
        out, err = io.StringIO(), io.StringIO()
        tmp = tempfile.mkdtemp(prefix="wc-gate-")
        try:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            code = gatecheck.run(None, "t", 1, "nope..nope", repo=tmp, out=out, err=err, env={})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(code, gatecheck.UNAVAILABLE_EXIT)
        self.assertTrue(out.getvalue().startswith("error: "), out.getvalue())
        self.assertEqual(err.getvalue(), "wavecoord gate-check: " + out.getvalue())
        # --json: only the JSON on stdout, nothing else
        out, err = io.StringIO(), io.StringIO()
        tmp = tempfile.mkdtemp(prefix="wc-gate-")
        try:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            gatecheck.run(None, "t", 1, "nope..nope", repo=tmp, out=out, err=err, env={}, as_json=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(json.loads(out.getvalue())["verdict"], "error")
        self.assertEqual(err.getvalue(), "")

    def test_same_file_writes_once(self):
        with tempfile.TemporaryFile("w+") as f:
            self.assertTrue(gatecheck._same_file(f, f))
            self.assertFalse(gatecheck._same_file(f, io.StringIO()))


if __name__ == "__main__":
    unittest.main()
