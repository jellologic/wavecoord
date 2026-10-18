"""`wavecoord install-server`: the pinned zipapp copy and the launchd / systemd --user unit."""

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import _paths

from wavecoord import installsrv, zipapp


class InstallServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wc-inst-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.repo = os.path.join(self.tmp, "my repo")
        os.makedirs(os.path.join(self.repo, ".claude"))
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        with open(os.path.join(self.repo, ".claude", "wavecoord.json"), "w") as f:
            json.dump({"project": "demo"}, f)

    def cli(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("WAVECOORD_", "XDG_"))}
        env.update(HOME=self.home, PYTHONPATH=_paths.SRC, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-m", "wavecoord", "install-server", "--repo", self.repo] + list(args),
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    def files(self):
        out = []
        for d, _, fs in os.walk(self.home):
            out += [os.path.relpath(os.path.join(d, f), self.home) for f in fs]
        return sorted(f for f in out if not f.startswith("Library/Caches/"))  # the macOS python's own cache

    def test_dry_run_launchd(self):
        p = self.cli("--dry-run", "--json", "--platform", "darwin", "--port", "18800")
        self.assertEqual(p.returncode, 0, p.stderr)
        plan = json.loads(p.stdout)
        h = zipapp.self_hash()
        self.assertEqual(plan["zipapp"], os.path.join(self.home, ".wavecoord", "bin", "wavecoord-" + h))
        self.assertEqual(plan["unit_path"], os.path.join(self.home, "Library", "LaunchAgents",
                                                         "io.github.wavecoord.demo.plist"))
        unit = plistlib.loads(plan["unit"].encode())
        self.assertEqual(unit["Label"], "io.github.wavecoord.demo")
        self.assertEqual(unit["ProgramArguments"][1:], ["-I", plan["zipapp"], "serve", "--repo", self.repo,
                                                        "--port", "18800"])
        self.assertEqual(unit["WorkingDirectory"], self.repo)
        self.assertEqual(unit["EnvironmentVariables"]["HOME"], self.home)
        self.assertIn("PATH", unit["EnvironmentVariables"])
        self.assertTrue(unit["RunAtLoad"])
        self.assertEqual(unit["StandardOutPath"], os.path.join(self.home, ".wavecoord", "demo", "serve.log"))
        self.assertEqual(plan["start"][-1][:2], ["launchctl", "bootstrap"])
        self.assertIn("no tokens file", p.stderr)
        self.assertEqual(self.files(), [])  # a dry run writes nothing

    def test_dry_run_systemd(self):
        p = self.cli("--dry-run", "--platform", "linux", "--no-push")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("would write %s" % os.path.join(self.home, ".config", "systemd", "user",
                                                      "wavecoord-demo.service"), p.stdout)
        self.assertIn('ExecStart="%s" "-I" "%s"' % (sys.executable, os.path.join(
            self.home, ".wavecoord", "bin", "wavecoord-" + zipapp.self_hash())), p.stdout)
        self.assertIn('"--repo" "%s" "--no-push"' % self.repo, p.stdout)
        self.assertIn("would run: systemctl --user enable wavecoord-demo.service", p.stdout)
        self.assertEqual(self.files(), [])

    def test_install_writes_the_pin_and_the_unit(self):
        old = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old) if old else os.environ.pop("HOME", None))
        plan = installsrv.plan(self.repo, platform="linux")
        ran = []

        class R(object):
            returncode, stdout, stderr = 0, "", ""

        installsrv.install(plan, start=True, run=lambda argv: ran.append(argv) or R())
        with open(plan["zipapp"], "rb") as f:
            self.assertEqual(f.read(), zipapp.self_bytes())
        self.assertTrue(os.access(plan["zipapp"], os.X_OK))
        with open(plan["unit_path"]) as f:
            self.assertEqual(f.read(), plan["unit"])
        self.assertEqual(ran, plan["start"])
        self.assertEqual(ran[-1], ["systemctl", "--user", "restart", "wavecoord-demo.service"])


if __name__ == "__main__":
    unittest.main()
