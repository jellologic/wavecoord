import json
import os
import subprocess
import sys
import tempfile
import unittest

import _paths
from wavecoord import __version__, cli

ZIPAPP = os.path.join(_paths.ROOT, "plugin", "bin", "wavecoord")
BUILD = os.path.join(_paths.ROOT, "scripts", "build-zipapp.py")


def clean_env(**extra):
    e = {k: v for k, v in os.environ.items()
         if not k.startswith(("WAVECOORD_", "CLAUDE_PLUGIN_OPTION_"))}
    e.update(extra)
    return e


def run(args, stdin="", env=None, cwd=None):
    return subprocess.run([sys.executable, ZIPAPP] + args, input=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True,
                          env=env or clean_env(), cwd=cwd)


class ZipappTest(unittest.TestCase):
    def test_up_to_date(self):
        p = subprocess.run([sys.executable, BUILD, "--check"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_shebang_and_mode(self):
        with open(ZIPAPP, "rb") as f:
            self.assertEqual(f.readline(), b"#!/usr/bin/env python3\n")
        self.assertTrue(os.access(ZIPAPP, os.X_OK))

    def test_deterministic(self):
        with tempfile.TemporaryDirectory() as d:
            outs = []
            for i in range(2):
                out = os.path.join(d, "w%d" % i)
                subprocess.run([sys.executable, BUILD, "--out", out], check=True, stdout=subprocess.PIPE)
                with open(out, "rb") as f:
                    outs.append(f.read())
            self.assertEqual(outs[0], outs[1])


class CliTest(unittest.TestCase):
    def test_version(self):
        p = run(["version"])
        self.assertEqual((p.returncode, p.stdout), (0, "wavecoord %s\n" % __version__))

    def test_status_not_configured(self):
        with tempfile.TemporaryDirectory() as d:
            p = run(["status"], cwd=d)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "not configured\n")

    def test_status_unreachable(self):
        with tempfile.TemporaryDirectory() as d:
            p = run(["status", "--timeout", "1"], cwd=d, env=clean_env(WAVECOORD_URL="http://127.0.0.1:9"))
        self.assertEqual(p.returncode, 1)
        self.assertTrue(p.stdout.startswith("unreachable: no coordinator answers at http://127.0.0.1:9"), p.stdout)

    def test_url_resolution_order(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".claude", "sub"))
            with open(os.path.join(d, ".claude", "wavecoord.json"), "w") as f:
                json.dump({"project": "demo", "coord_url": "http://127.0.0.1:3/"}, f)
            sub = os.path.join(d, ".claude", "sub")
            self.assertEqual(cli.resolve_coord_url({}, sub), "http://127.0.0.1:3")
            self.assertEqual(cli.resolve_coord_url({"CLAUDE_PLUGIN_OPTION_COORD_URL": "http://127.0.0.1:2"}, sub), "http://127.0.0.1:2")
            self.assertEqual(cli.resolve_coord_url({"WAVECOORD_URL": "http://127.0.0.1:1",
                                                    "CLAUDE_PLUGIN_OPTION_COORD_URL": "http://127.0.0.1:2"}, sub), "http://127.0.0.1:1")

    def test_hook_is_silent_noop(self):
        for stdin in ('{"hook_event_name": "SessionStart", "session_id": "x", "source": "startup"}', "", "not json"):
            for ev in ("SessionStart", "PreToolUse", "SomeFutureEvent"):
                p = run(["hook", ev], stdin=stdin)
                self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""), (ev, stdin))

    def test_stubs_exit_2(self):
        for name in cli.STUBS:  # every command is implemented now
            p = run([name, "--any", "arg"])
            self.assertEqual(p.returncode, 2, name)
            self.assertIn("not implemented in P0", p.stderr)


class PluginFilesTest(unittest.TestCase):
    def test_hooks_json_points_at_bin(self):
        with open(os.path.join(_paths.ROOT, "plugin", "hooks", "hooks.json")) as f:
            hooks = json.load(f)["hooks"]
        for ev, groups in hooks.items():
            for g in groups:
                for h in g["hooks"]:
                    self.assertEqual(h["command"], "${CLAUDE_PLUGIN_ROOT}/bin/wavecoord")
                    self.assertEqual(h["args"][:2], ["hook", ev])

    def test_manifest_version_matches_package(self):
        with open(os.path.join(_paths.ROOT, "plugin", ".claude-plugin", "plugin.json")) as f:
            self.assertEqual(json.load(f)["version"], __version__)


if __name__ == "__main__":
    unittest.main()
