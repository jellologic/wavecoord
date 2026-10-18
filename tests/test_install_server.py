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

    def cli(self, *args, **extra_env):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("WAVECOORD_", "XDG_"))}
        env.update(HOME=self.home, PYTHONPATH=_paths.SRC, PYTHONDONTWRITEBYTECODE="1")
        env.update(extra_env)
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

    def test_env_carry_and_flags(self):
        # the server's launcher needs the shell's WAVECOORD_SSH (an ssh wrapper with the host key)
        p = self.cli("--dry-run", "--json", "--platform", "darwin", "--env", "FOO=bar baz", "--env",
                     "WAVECOORD_POLL_S=5", WAVECOORD_SSH="/opt/x/ssh-wrapper", WAVECOORD_HOME="/srv/wc",
                     WAVECOORD_POLL_S="1", WAVECOORD_TOKEN="not-carried-secret", WAVECOORD_TOKEN_FILE="/t/tok",
                     WAVECOORD_TASK="t1")
        self.assertEqual(p.returncode, 0, p.stderr)
        env = plistlib.loads(json.loads(p.stdout)["unit"].encode())["EnvironmentVariables"]
        self.assertEqual(env["WAVECOORD_SSH"], "/opt/x/ssh-wrapper")
        self.assertEqual(env["WAVECOORD_HOME"], "/srv/wc")
        self.assertEqual(env["WAVECOORD_TOKEN_FILE"], "/t/tok")
        self.assertEqual(env["WAVECOORD_POLL_S"], "5")  # --env wins over a carried value
        self.assertEqual(env["FOO"], "bar baz")
        self.assertEqual(env["HOME"], self.home)
        for k in ("WAVECOORD_TOKEN", "WAVECOORD_TASK"):
            self.assertNotIn(k, env)
        self.assertNotIn("not-carried-secret", p.stdout)
        p = self.cli("--dry-run", "--platform", "linux", "--no-carry-env", WAVECOORD_SSH="/opt/x/ssh-wrapper")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("WAVECOORD_SSH", p.stdout)
        p = self.cli("--dry-run", "--platform", "linux", "--env", "WAVECOORD_SSH=/w/ssh")
        self.assertIn('Environment="WAVECOORD_SSH=/w/ssh"', p.stdout)
        for bad in ("NOEQUALS", "1X=y", "HOME=/x"):
            p = self.cli("--dry-run", "--platform", "linux", "--env", bad)
            self.assertEqual(p.returncode, 1, bad)
            self.assertIn("--env", p.stderr)

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


FAKE_LAUNCHCTL = """#!/usr/bin/env python3
# A fake launchctl: the job stays loaded for $FAKE_LC_LINGER `print` calls after a bootout (the old
# process still exits); a bootstrap while it is loaded fails with exit 5, as launchd does.
import json, os, sys
st_path = os.environ["FAKE_LC_STATE"]
st = json.load(open(st_path)) if os.path.exists(st_path) else {"loaded": True, "linger": 0, "eio": 0}
args = sys.argv[1:]
with open(os.environ["FAKE_LC_LOG"], "a") as f:
    f.write(" ".join(args) + "\\n")
rc = 0
if args[0] == "bootout":
    if st["loaded"]:
        st["linger"] = int(os.environ.get("FAKE_LC_LINGER", "0"))
        st["loaded"] = st["linger"] > 0
    else:
        sys.stderr.write("Boot-out failed: 3: No such process\\n"); rc = 3
elif args[0] == "print":
    if st["linger"] > 0:
        st["linger"] -= 1
        st["loaded"] = st["linger"] > 0
        rc = 0
    else:
        rc = 0 if st["loaded"] else 113
elif args[0] == "bootstrap":
    if st["loaded"] or st.get("eio", 0) < int(os.environ.get("FAKE_LC_EIO", "0")):
        st["eio"] = st.get("eio", 0) + 1
        sys.stderr.write("Bootstrap failed: 5: Input/output error\\n"); rc = 5
    else:
        st["loaded"] = True
json.dump(st, open(st_path, "w"))
sys.exit(rc)
"""


class LaunchdRestartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wc-lc-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "launchctl"), "w") as f:
            f.write(FAKE_LAUNCHCTL)
        os.chmod(os.path.join(bindir, "launchctl"), 0o755)
        self.log = os.path.join(self.tmp, "lc.log")
        self.state = os.path.join(self.tmp, "lc.json")
        self.env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get("PATH", ""),
                        FAKE_LC_LOG=self.log, FAKE_LC_STATE=self.state)
        self.p = {"platform": "darwin", "zipapp": os.path.join(self.tmp, "wc.pyz"), "log": os.path.join(self.tmp, "l", "s.log"),
                  "unit_path": os.path.join(self.tmp, "u.plist"), "unit": "<plist/>",
                  "start": [["launchctl", "bootout", "gui/501/dev.wavecoord.demo"],
                            ["launchctl", "bootstrap", "gui/501", os.path.join(self.tmp, "u.plist")]]}

    def run_install(self, **env):
        e = dict(self.env, **env)
        run = lambda argv: subprocess.run(argv, env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          universal_newlines=True)
        return installsrv.install(self.p, start=True, run=run, sleep=lambda s: None, wait_s=5)

    def calls(self):
        with open(self.log) as f:
            return [l.split()[0] for l in f if l.strip()]

    def test_waits_for_the_old_job_before_bootstrap(self):
        self.run_install(FAKE_LC_LINGER="3")
        c = self.calls()
        self.assertEqual(c[0], "bootout")
        self.assertEqual(c[-1], "bootstrap")
        self.assertEqual(c.count("bootstrap"), 1)  # no EIO: it waited until `print` said gone
        self.assertGreaterEqual(c.count("print"), 3)

    def test_not_loaded_is_fine(self):
        with open(self.state, "w") as f:
            json.dump({"loaded": False, "linger": 0, "eio": 0}, f)
        self.run_install()
        self.assertEqual(self.calls(), ["bootout", "print", "bootstrap"])

    def test_retries_once_on_eio(self):
        self.run_install(FAKE_LC_EIO="1")
        c = self.calls()
        self.assertEqual(c.count("bootstrap"), 2)

    def test_second_eio_fails_loudly(self):
        with self.assertRaises(RuntimeError) as cm:
            self.run_install(FAKE_LC_EIO="2")
        self.assertIn("exit 5", str(cm.exception))
        self.assertEqual(self.calls().count("bootstrap"), 2)


if __name__ == "__main__":
    unittest.main()
