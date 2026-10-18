"""Test environment for the launcher: a temp repo with a bare remote, a fake coordinator,
a fake pr_cmd, and tests/fake-claude + tests/fake-ssh on PATH. No network, no model."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

import _paths

from wavecoord.launcher.client import Launcher
from wavecoord.launcher.config import Config

FAKE_CLAUDE_DIR = os.path.join(_paths.TESTS, "fake-claude")
FAKE_SSH_DIR = os.path.join(_paths.TESTS, "fake-ssh")
NOW = "2026-10-18T12:00:00Z"


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class FakeCoord(object):
    """Records every request; answers like the contract (health, claim, hb, the rest 200)."""

    def __init__(self):
        self.requests = []
        self.lost = []
        self.epoch = 1792324800000
        self.claim_status = 200
        fc = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                fc.requests.append({"method": "GET", "path": self.path, "headers": dict(self.headers), "body": None})
                if self.path == "/api/health":
                    return self._reply(200, {"now": NOW, "ok": True, "api": 1, "schema": 1, "version": "fake",
                                             "started": NOW})
                return self._reply(404, {"now": NOW, "error": {"code": "not_found", "message": self.path}})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode() or "null")
                fc.requests.append({"method": "POST", "path": self.path, "headers": dict(self.headers), "body": body})
                if self.path == "/api/leases":
                    if fc.claim_status != 200:
                        return self._reply(fc.claim_status, {"now": NOW, "error": {"code": "held", "message": "held"}})
                    lease = {"id": 7, "task": body["task"], "attempt": body["attempt"], "holder": body["holder"],
                             "epoch": fc.epoch, "state": "active", "acquired": NOW, "renewed": NOW,
                             "expires_at": NOW, "released_at": None, "released_by": None, "reason": None}
                    return self._reply(200, {"now": NOW, "lease": lease, "reservations": [], "conflicts": []})
                if self.path == "/api/hb":
                    return self._reply(200, {"now": NOW, "agent": {}, "lease": None, "lost": fc.lost, "inbox": [],
                                             "more": False})
                return self._reply(200, {"now": NOW, "ok": True})

        self.server = _Server(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def posts(self, prefix):
        return [r for r in self.requests if r["method"] == "POST" and r["path"].startswith(prefix)]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


PR_SCRIPT = """#!/usr/bin/env python3
import json, os, sys
out = os.environ["FAKE_PR_OUT"]
args = sys.argv[1:]
body = ""
if "--body-file" in args:
    with open(args[args.index("--body-file") + 1]) as f:
        body = f.read()
with open(out, "a") as f:
    f.write(json.dumps({"args": args, "body": body, "cwd": os.getcwd()}) + "\\n")
if os.environ.get("FAKE_PR_FAIL"):
    sys.stderr.write("pr failed\\n")
    sys.exit(1)
print("https://example.invalid/pull/1")
"""


def sh(args, cwd, **kw):
    return subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, **kw).stdout.strip()


class LauncherTestCase(unittest.TestCase):
    """setUp builds: <tmp>/origin.git (bare), <tmp>/repo (clone, repo_path), a fake coord."""

    ENV_KEYS = ("PATH", "HOME", "WAVECOORD_HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_AUTHOR_NAME",
                "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "WAVECOORD_STOP_GRACE_S",
                "WAVECOORD_HB_INTERVAL_S", "WAVECOORD_POLL_S", "FAKE_PR_OUT", "FAKE_PR_FAIL", "FAKE_SSH_LOG",
                "FAKE_SSH_HOME", "FAKE_SSH_DOWN", "WAVECOORD_URL", "CLAUDE_PLUGIN_OPTION_COORD_URL",
                "CLAUDE_PLUGIN_OPTION_TOKEN", "WAVECOORD_TOKEN_FILE", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                "WAVECOORD_NOTIFY_MIN_INTERVAL_S", "WAVECOORD_CLIENT_TIMEOUT_S") + tuple(
        "FAKE_CLAUDE_" + k for k in ("COMMIT", "STATUS", "REPORT", "EXIT", "SLEEP", "COST", "DUMP", "EVENTS"))

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV_KEYS}
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wc-launch-"))
        t = self.tmp
        self.home = os.path.join(t, "home")
        os.makedirs(self.home)
        gitcfg = os.path.join(t, "gitconfig")
        with open(gitcfg, "w") as f:
            f.write("[user]\n\tname = test\n\temail = test@example.invalid\n[init]\n\tdefaultBranch = main\n"
                    "[commit]\n\tgpgsign = false\n")
        bindir = os.path.join(t, "bin")
        os.makedirs(bindir)
        self.pr_script = os.path.join(bindir, "fake-pr")
        with open(self.pr_script, "w") as f:
            f.write(PR_SCRIPT)
        os.chmod(self.pr_script, 0o755)
        env = os.environ
        for k in self.ENV_KEYS:
            if k.startswith(("FAKE_", "CLAUDE", "WAVECOORD_URL", "WAVECOORD_TOKEN")):
                env.pop(k, None)
        env.update({
            "PATH": os.pathsep.join([FAKE_CLAUDE_DIR, FAKE_SSH_DIR, bindir, os.environ.get("PATH", "")]),
            "HOME": self.home, "WAVECOORD_HOME": os.path.join(self.home, ".wavecoord"),
            "GIT_CONFIG_GLOBAL": gitcfg, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "WAVECOORD_STOP_GRACE_S": "2", "WAVECOORD_HB_INTERVAL_S": "0.5", "WAVECOORD_POLL_S": "0.2",
            "WAVECOORD_NOTIFY_MIN_INTERVAL_S": "0", "WAVECOORD_CLIENT_TIMEOUT_S": "2",
            "FAKE_PR_OUT": os.path.join(t, "prs.jsonl"),
            "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",  # must be stripped for the worker
        })
        self.origin = os.path.join(t, "origin.git")
        sh(["git", "init", "-q", "--bare", "-b", "main", self.origin], t)
        seed = os.path.join(t, "seed")
        sh(["git", "init", "-q", "-b", "main", seed], t)
        with open(os.path.join(seed, "README.md"), "w") as f:
            f.write("seed\n")
        sh(["git", "add", "README.md"], seed)
        sh(["git", "commit", "-q", "-m", "seed"], seed)
        sh(["git", "remote", "add", "origin", self.origin], seed)
        sh(["git", "push", "-q", "origin", "main"], seed)
        self.seed = seed
        self.repo = os.path.join(t, "repo")
        sh(["git", "clone", "-q", self.origin, self.repo], t)
        self.coord = FakeCoord()
        self.addCleanup(self.coord.close)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config(self, hosts=None, **extra):
        raw = {"project": "demo", "branch_prefix": "wave",
               "hosts": hosts or [{"name": "local", "slots": 2, "repo_path": self.repo}],
               "pr_cmd": [self.pr_script, "--head", "{branch}", "--base", "{base}", "--title", "{title}",
                          "--body-file", "{body_file}"]}
        raw.update(extra)
        return Config(raw, self.repo)

    def launcher(self, coord=True, **cfg_extra):
        return Launcher(self.config(**cfg_extra), coord_url=self.coord.url if coord else "")

    def opts(self, task="t1", prompt="Add a file.\nMore detail.", **kw):
        o = {"task": task, "prompt": prompt, "timeout_min": 1}
        o.update(kw)
        return o

    def push_to_main(self, name, content):
        sh(["git", "pull", "-q", "origin", "main"], self.seed)
        with open(os.path.join(self.seed, name), "w") as f:
            f.write(content)
        sh(["git", "add", name], self.seed)
        sh(["git", "commit", "-q", "-m", "main: " + name], self.seed)
        sh(["git", "push", "-q", "origin", "main"], self.seed)

    def remote_branches(self):
        out = sh(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"], self.origin)
        return out.splitlines()

    def prs(self):
        try:
            with open(os.environ["FAKE_PR_OUT"]) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def wait_state(self, L, aid, states, timeout=60):
        end = time.time() + timeout
        r = None
        while time.time() < end:
            r = L.result(aid)
            if r.get("state") in states:
                return r
            time.sleep(0.2)
        self.fail("attempt %s did not reach %s: %s" % (aid, states, r))


def python():
    return sys.executable
