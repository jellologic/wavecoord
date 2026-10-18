"""The core CLI end to end, through the zipapp: tokens, serve, status, events, log, revert, rebuild."""

import json
import os
import signal
import stat
import subprocess
import sys
import unittest

import _coord
from test_cli import ZIPAPP, clean_env


class CoreCliTest(unittest.TestCase):
    def setUp(self):
        self.tr = _coord.TempRepo()
        self.tokens = os.path.join(self.tr.root, "tokens.json")
        self.env = clean_env(HOME=self.tr.root, WAVECOORD_TOKENS_FILE=self.tokens,
                             WAVECOORD_COMMIT_WINDOW_MS="20")
        self.proc = None

    def tearDown(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        if self.proc:
            self.proc.stdout.close()
            self.proc.stderr.close()
        self.tr.cleanup()

    def run_cli(self, *args, env=None, ok=True):
        p = subprocess.run([sys.executable, ZIPAPP] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, env=env or self.env, cwd=self.tr.repo, timeout=60)
        if ok:
            self.assertEqual(p.returncode, 0, (args, p.stdout, p.stderr))
        return p

    def test_tokens_provision(self):
        tok = self.run_cli("tokens", "provision", "--role", "agent", "--host", "build-1").stdout.strip()
        self.assertGreater(len(tok), 30)
        with open(self.tokens) as f:
            data = json.load(f)
        self.assertEqual(stat.S_IMODE(os.stat(self.tokens).st_mode), 0o600)
        self.assertEqual(list(data["tokens"].values())[0]["principal"], "host:build-1")
        with open(self.tokens) as f:
            self.assertNotIn(tok, f.read())
        from wavecoord import schemaload
        self.assertEqual(schemaload.errors(data, "api", "#/definitions/tokens_file"), [])
        p = self.run_cli("tokens", "provision", "--role", "agent", ok=False)
        self.assertNotEqual(p.returncode, 0)

    def test_serve_end_to_end(self):
        p = self.run_cli("serve", "--port", "0", ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("tokens provision", p.stderr)
        tok = self.run_cli("tokens", "provision", "--role", "lead").stdout.strip()
        home = os.path.join(self.tr.root, "home")
        self.proc = subprocess.Popen([sys.executable, ZIPAPP, "serve", "--repo", self.tr.repo, "--home", home,
                                      "--port", "0"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     universal_newlines=True, env=self.env)
        line = self.proc.stdout.readline()
        self.assertTrue(line.startswith("wavecoord serve: http://127.0.0.1:"), line)
        url = line.split()[2]
        env = dict(self.env, WAVECOORD_URL=url, WAVECOORD_TOKEN=tok)
        self.assertTrue(self.run_cli("status", env=env).stdout.startswith("ok: " + url))
        # A second server on the same home refuses to start (single writer).
        p = self.run_cli("serve", "--repo", self.tr.repo, "--home", home, "--port", "0", ok=False)
        self.assertIn("another server", p.stderr)
        from wavecoord.corecli import api
        for i in range(2):
            s, b = api(url, "POST", "/api/messages", {"to": {"kind": "lead"}, "body": "m%d" % i}, token=tok)
            self.assertEqual(s, 200, b)
        out = self.run_cli("events", "--since", "0", env=env).stdout.splitlines()
        self.assertEqual([json.loads(l)["kind"] for l in out], ["server.start", "message.new", "message.new"])
        sha = subprocess.run(["git", "rev-parse", "coord/log"], cwd=self.tr.repo, stdout=subprocess.PIPE,
                             universal_newlines=True).stdout.strip()
        r = json.loads(self.run_cli("revert", "3", "--reason", "test", env=env).stdout)
        self.assertEqual(r["event"]["payload"]["target_seq"], 3)
        self.assertTrue(json.loads(self.run_cli("rebuild", env=env).stdout)["unchanged"])
        api(url, "POST", "/api/messages", {"to": {"kind": "lead"}, "body": "later"}, token=tok)
        r = json.loads(self.run_cli("rebuild", "--at", sha, env=env).stdout)
        self.assertEqual(r["event"]["payload"]["to_seq"], 3)
        log = self.run_cli("log", "--verify", env=env)
        self.assertIn("chain verified", log.stderr)
        self.assertEqual([l.split()[2] for l in log.stdout.splitlines()][-1], "revert")
        self.proc.send_signal(signal.SIGTERM)
        self.proc.wait(20)
        self.assertEqual(self.proc.returncode, 0)
        p = self.run_cli("status", env=env, ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertTrue(p.stdout.startswith("unreachable"))
        out = self.run_cli("rebuild", "--repo", self.tr.repo, "--home", home, env=env).stdout
        self.assertIn("offline", out)
        # Everything acked reached the remote by the time the server stopped... or later: the push is async.
        n = len(subprocess.run(["git", "ls-tree", "-r", "--name-only", "coord/log", "events"], cwd=self.tr.repo,
                               stdout=subprocess.PIPE, universal_newlines=True).stdout.split())
        self.assertEqual(n, len(log.stdout.splitlines()))


if __name__ == "__main__":
    unittest.main()
