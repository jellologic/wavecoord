"""The ssh path of the launcher, with tests/fake-ssh/ssh (it runs the command locally)."""

import json
import os
import unittest

import _paths  # noqa: F401
from launcher_env import LauncherTestCase

from wavecoord import zipapp
from wavecoord.launcher.common import EXIT_NEED_UPLOAD, Unreachable
from wavecoord.launcher.transport import Host


class SshTest(LauncherTestCase):
    def setUp(self):
        super(SshTest, self).setUp()
        self.remote_home = os.path.join(self.tmp, "remote-home")
        os.environ["FAKE_SSH_HOME"] = self.remote_home
        os.environ["FAKE_SSH_LOG"] = os.path.join(self.tmp, "ssh.jsonl")

    def ssh_calls(self):
        try:
            with open(os.environ["FAKE_SSH_LOG"]) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def host_cfg(self):
        return {"name": "build-1", "ssh": "builder@build-1.example.invalid", "slots": 2, "repo_path": self.repo}

    def test_argv_uses_control_master_and_exit_86_marker(self):
        h = Host(self.host_cfg())
        argv = h.argv("probe")
        self.assertEqual(argv[0], "ssh")
        joined = " ".join(argv)
        for opt in ("BatchMode=yes", "ControlMaster=auto", "ControlPersist=", "ControlPath=", "ServerAliveInterval="):
            self.assertIn(opt, joined)
        self.assertEqual(argv[-2], "builder@build-1.example.invalid")
        self.assertIn("wavecoord-%s" % zipapp.self_hash(), argv[-1])
        self.assertIn("exit %d" % EXIT_NEED_UPLOAD, argv[-1])
        self.assertIn("python3 -I", argv[-1])

    def test_probe_uploads_on_86_then_reuses(self):
        h = Host(self.host_cfg())
        out = h.call("probe", {"project": "demo"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["wavecoord"], zipapp.self_hash())
        uploaded = os.path.join(self.remote_home, "bin", "wavecoord-" + zipapp.self_hash())
        self.assertTrue(os.access(uploaded, os.X_OK))
        with open(uploaded, "rb") as f:
            self.assertEqual(f.read(), zipapp.self_bytes())
        calls = self.ssh_calls()
        self.assertEqual(len(calls), 3)  # probe -> 86, upload, probe again
        self.assertIn("cat >", calls[1]["cmd"])
        h.call("probe", {"project": "demo"})
        self.assertEqual(len(self.ssh_calls()), 4)  # no second upload

    def test_unreachable(self):
        os.environ["FAKE_SSH_DOWN"] = "1"
        with self.assertRaises(Unreachable):
            Host(self.host_cfg()).call("probe", {})
        out = Host(self.host_cfg()).safe_call("probe", {})
        self.assertFalse(out["ok"])
        self.assertTrue(out["unreachable"])

    def test_launch_over_ssh_end_to_end(self):
        os.environ["FAKE_CLAUDE_COMMIT"] = "1"
        dump = os.path.join(self.tmp, "dump.json")
        os.environ["FAKE_CLAUDE_DUMP"] = dump
        # an agent token for the host, copied over ssh into a mode-600 file
        hosts_dir = os.path.join(os.environ["WAVECOORD_HOME"], "hosts")
        os.makedirs(hosts_dir)
        with open(os.path.join(hosts_dir, "build-1.token"), "w") as f:
            f.write("test-token-not-a-secret\n")
        L = self.launcher(hosts=[self.host_cfg()])
        rec = L.launch(self.opts())
        self.assertEqual(rec["host"], "build-1")
        r = self.wait_state(L, "t1-1", ("reported", "failed", "timed_out"))
        self.assertEqual(r["state"], "reported", r)
        self.assertTrue(r["pushed"])
        self.assertTrue(r["state_dir"].startswith(self.remote_home))
        # the pinned plugin copy is on the "remote" and is the worker's --plugin-dir
        with open(dump) as f:
            argv = json.load(f)["argv"]
        plugin = argv[argv.index("--plugin-dir") + 1]
        self.assertTrue(plugin.startswith(os.path.join(self.remote_home, "plugin-")))
        self.assertTrue(os.path.isfile(os.path.join(plugin, "bin", "wavecoord")))
        tok = os.path.join(self.remote_home, "token")
        self.assertEqual(oct(os.stat(tok).st_mode & 0o777), "0o600")
        cmds = [c["cmd"] for c in self.ssh_calls()]
        self.assertTrue(any("plugin-install" in c for c in cmds))
        self.assertTrue(any("token-install" in c for c in cmds))
        self.assertFalse(any("test-token-not-a-secret" in " ".join(c["argv"]) for c in self.ssh_calls()))
        self.assertEqual(len(self.coord.posts("/api/attempts/t1/1/report")), 1)
        self.assertEqual(len(self.prs()), 1)


if __name__ == "__main__":
    unittest.main()
