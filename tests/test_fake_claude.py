import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import uuid

import _paths
from wavecoord import jsonschema_lite as js

FAKE = os.path.join(_paths.TESTS, "fake-claude", "claude")
SID = "5f0c1e9a-3b7d-4c2e-9a61-0d8e2f4b7c10"


def run(args, stdin="do the task", env=None, cwd=None):
    e = dict(os.environ)
    for k in list(e):
        if k.startswith("FAKE_CLAUDE_"):
            del e[k]
    e.update(env or {})
    return subprocess.run([sys.executable, FAKE] + args, input=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, env=e, cwd=cwd)


def report_schema():
    with open(os.path.join(_paths.SCHEMAS, "report.schema.json"), encoding="utf-8") as f:
        return json.load(f)


class FakeClaudeTest(unittest.TestCase):
    def test_is_executable(self):
        self.assertTrue(os.access(FAKE, os.X_OK))

    def test_stream_json(self):
        p = run(["-p", "--output-format", "stream-json", "--verbose", "--session-id", SID, "--json-schema", "{}"])
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = [json.loads(l) for l in p.stdout.splitlines()]
        self.assertEqual([l["type"] for l in lines], ["system", "assistant", "result"])
        self.assertEqual(lines[0]["subtype"], "init")
        self.assertTrue(all(l["session_id"] == SID for l in lines))
        res = lines[-1]
        self.assertFalse(res["is_error"])
        self.assertIn("do the task", res["result"])
        self.assertEqual(js.validate(res["structured_output"], report_schema()), [])

    def test_json_output(self):
        p = run(["-p", "--output-format", "json", "--session-id", SID])
        res = json.loads(p.stdout)
        self.assertEqual(res["type"], "result")
        self.assertEqual(res["session_id"], SID)
        self.assertEqual(js.validate(res["structured_output"], report_schema()), [])

    def test_text_output_and_new_session(self):
        p = run(["-p"])
        self.assertEqual(p.returncode, 0)
        self.assertIn("do the task", p.stdout)
        p = run(["-p", "--output-format", "json"])
        uuid.UUID(json.loads(p.stdout)["session_id"])

    def test_bad_session_id(self):
        p = run(["-p", "--session-id", "not-a-uuid"])
        self.assertEqual(p.returncode, 1)
        self.assertIn("UUID", p.stderr)

    def test_status_and_exit_overrides(self):
        p = run(["-p", "--output-format", "json"], env={"FAKE_CLAUDE_STATUS": "blocked", "FAKE_CLAUDE_EXIT": "3"})
        self.assertEqual(p.returncode, 3)
        res = json.loads(p.stdout)
        self.assertTrue(res["is_error"])
        self.assertEqual(res["structured_output"]["status"], "blocked")

    def test_report_file_override(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"status": "needs_decision"}, f)
        try:
            p = run(["-p", "--output-format", "json"], env={"FAKE_CLAUDE_REPORT": f.name})
            self.assertEqual(json.loads(p.stdout)["structured_output"], {"status": "needs_decision"})
        finally:
            os.unlink(f.name)

    def test_commit(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            p = run(["-p", "--output-format", "stream-json", "--session-id", SID], cwd=d,
                    env={"FAKE_CLAUDE_COMMIT": "1"})
            self.assertEqual(p.returncode, 0, p.stderr)
            rep = json.loads(p.stdout.splitlines()[-1])["structured_output"]
            self.assertEqual(js.validate(rep, report_schema()), [])
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d, stdout=subprocess.PIPE,
                                  universal_newlines=True, check=True).stdout.strip()
            self.assertEqual(rep["commits"], [head])
            self.assertEqual(rep["files"], ["fake-claude-%s.txt" % SID[:8]])
            self.assertTrue(os.path.isfile(os.path.join(d, rep["files"][0])))

    def test_modes(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            modes = os.path.join(d, ".modes.json")
            runs = os.path.join(d, ".runs.jsonl")
            with open(modes, "w") as f:
                json.dump({"t1": {"commit": {"a/x.txt": "x\n", "b.txt": "b\n"},
                                  "report": {"proposals": [{"kind": "split", "summary": "split t1"}]}},
                           "t1@alt": {"rate_limit": True},
                           "t1:resolve": {"status": "blocked"}}, f)
            env = {"FAKE_CLAUDE_MODES": modes, "FAKE_CLAUDE_RUNS": runs, "WAVECOORD_TASK": "t1",
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
            p = run(["-p", "--output-format", "stream-json", "--session-id", SID], cwd=d, env=env)
            self.assertEqual(p.returncode, 0, p.stderr)
            rep = json.loads(p.stdout.splitlines()[-1])["structured_output"]
            self.assertEqual(js.validate(rep, report_schema()), [])
            self.assertEqual((rep["files"], len(rep["commits"])), (["a/x.txt", "b.txt"], 1))
            self.assertEqual(rep["proposals"][0]["summary"], "split t1")
            # the provider of the env file picks the rate-limited mode
            p = run(["-p", "--output-format", "stream-json"], cwd=d, env=dict(env, FAKE_PROVIDER="alt"))
            self.assertEqual(p.returncode, 1)
            lines = [json.loads(l) for l in p.stdout.splitlines()]
            self.assertEqual(lines[-2]["type"], "rate_limit_event")
            self.assertTrue(lines[-1]["is_error"])
            # a resolver brief picks the :resolve mode
            p = run(["-p", "--output-format", "json"], stdin="# Resolve merge conflicts: task t1, attempt 1\n",
                    cwd=d, env=env)
            self.assertEqual(json.loads(p.stdout)["structured_output"]["status"], "blocked")
            with open(runs) as f:
                self.assertEqual([json.loads(l)["mode"] for l in f], ["t1", "t1@alt", "t1:resolve"])


class FakeTranscriptsTest(unittest.TestCase):
    def test_resume_keeps_the_id_and_unknown_ids_fail(self):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            env = {"FAKE_CLAUDE_TRANSCRIPTS": "1", "CLAUDE_CONFIG_DIR": os.path.join(d, "cfg")}
            p = run(["-p", "--output-format", "json", "--session-id", SID], cwd=d, env=env)
            self.assertEqual(p.returncode, 0, p.stderr)
            proj = os.path.join(d, "cfg", "projects")
            self.assertEqual(os.listdir(proj), [re.sub(r"[^A-Za-z0-9]", "-", d)])
            p = run(["-p", "--output-format", "json", "--resume", SID], cwd=d, env=env)
            self.assertEqual(json.loads(p.stdout)["session_id"], SID)
            with open(os.path.join(proj, os.listdir(proj)[0], SID + ".jsonl")) as f:
                self.assertEqual(len(f.readlines()), 2)
            p = run(["-p", "--output-format", "json", "--resume", SID, "--session-id", str(uuid.uuid4())],
                    cwd=d, env=env)
            self.assertNotEqual(p.returncode, 0)
            other = str(uuid.uuid4())
            p = run(["-p", "--output-format", "json", "--resume", other], cwd=d, env=env)
            self.assertEqual(p.returncode, 1)
            self.assertIn("No conversation found with session ID: %s" % other, p.stderr)
            res = json.loads(p.stdout)
            self.assertEqual((res["subtype"], res["num_turns"]), ("error_during_execution", 0))
            self.assertNotEqual(res["session_id"], other)


if __name__ == "__main__":
    unittest.main()
