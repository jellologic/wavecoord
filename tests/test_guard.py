"""`wavecoord guard install|check` in temp git repos against a fake coordinator."""

import json
import os
import subprocess
import sys
import unittest

import _paths
from fakecoord import FakeCoord, api_fixture
from test_hooks import WORKER, Base, closed_port, git
from wavecoord import jsonschema_lite as js

with open(os.path.join(_paths.SCHEMAS, "api.schema.json"), encoding="utf-8") as _f:
    API = json.load(_f)


class GuardTest(Base):
    def wc(self, args, env, cwd):
        return subprocess.run([sys.executable, "-m", "wavecoord"] + args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, timeout=60)

    def commit(self, env, cwd=None, name="docs/api.md"):
        cwd = cwd or self.repo
        os.makedirs(os.path.dirname(os.path.join(cwd, name)), exist_ok=True)
        with open(os.path.join(cwd, name), "a") as f:
            f.write("x\n")
        git(cwd, "add", name)
        return subprocess.run(["git", "commit", "-q", "-m", "c"], cwd=cwd, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, timeout=60)

    def install(self, env, cwd=None):
        p = self.wc(["guard", "install"], env, cwd or self.repo)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def test_install_sets_worktree_hooks_path_and_chains(self):
        marker = os.path.join(self.tmp, "chained")
        pre = os.path.join(self.repo, ".git", "hooks", "pre-commit")
        with open(pre, "w") as f:
            f.write("#!/bin/sh\necho ran >> %s\n" % marker)
        os.chmod(pre, 0o755)
        with FakeCoord() as fc:
            fc.routes[("POST", r"/api/check")] = (200, api_fixture("check-response.json"))
            env = self.worker_env(fc.url)
            self.install(env)
            self.install(env)  # re-install keeps the original chain
            hp = git(self.repo, "config", "--worktree", "--get", "core.hooksPath")
            self.assertTrue(hp.endswith("wavecoord-hooks"), hp)
            p = self.commit(env)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("docs/api.md: foreign_exclusive (task api-docs, docs/**)", p.stderr)
            self.assertIn("advisory only", p.stderr)
            chk = fc.find("POST", "/api/check")
            self.assertEqual(len(chk), 1)
            self.assertEqual(chk[0]["body"]["paths"], ["docs/api.md"])
            self.assertEqual(chk[0]["body"]["source"], "guard")
            self.assertEqual(chk[0]["body"]["agent"], WORKER)
            self.assertEqual(js.validate(chk[0]["body"], API, "#/definitions/check_request"), [])
            with open(marker) as f:
                self.assertEqual(f.read(), "ran\n")
            # --enforce (reserved for P6) refuses on a violation.
            p = self.commit(dict(env, WAVECOORD_GUARD_FLAGS="--enforce"))
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("refused (--enforce)", p.stderr)

    def test_failing_chained_hook_stops_commit(self):
        pre = os.path.join(self.repo, ".git", "hooks", "pre-commit")
        with open(pre, "w") as f:
            f.write("#!/bin/sh\nexit 3\n")
        os.chmod(pre, 0o755)
        env = self.worker_env(None)
        self.install(env)
        self.assertNotEqual(self.commit(env).returncode, 0)

    def test_no_config_and_unreachable_are_advisory(self):
        env = self.worker_env(None)
        self.install(env)
        p = self.commit(env)
        self.assertEqual((p.returncode, p.stderr), (0, ""))
        p = self.commit(self.worker_env("http://127.0.0.1:%d" % closed_port()))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("unreachable", p.stderr)

    def test_missing_build_is_skipped(self):
        import io
        from wavecoord import guard
        guard.install(self.repo, cmd=[sys.executable, os.path.join(self.tmp, "gone", "wavecoord")], out=io.StringIO())
        p = self.commit(self.worker_env("http://127.0.0.1:%d" % closed_port()))
        self.assertEqual((p.returncode, p.stderr), (0, ""))

    def test_linked_worktree_and_pre_push(self):
        bare = os.path.join(self.tmp, "remote.git")
        git(self.tmp, "init", "-q", "--bare", bare)
        git(self.repo, "remote", "add", "origin", bare)
        git(self.repo, "push", "-q", "origin", "main")
        wt = os.path.join(self.tmp, "wt")
        git(self.repo, "worktree", "add", "-q", "-b", "wave/api-auth-1", wt)
        with FakeCoord() as fc:
            env = self.worker_env(fc.url)
            p = self.wc(["guard", "install", "--worktree", wt], env, self.tmp)
            self.assertEqual(p.returncode, 0, p.stderr)
            # Only the linked worktree has the guards.
            self.assertIsNone(_get(self.repo, "core.hooksPath"))
            self.assertTrue(_get(wt, "core.hooksPath").endswith("wavecoord-hooks"))
            self.assertEqual(self.commit(env, wt, "src/auth/a.py").returncode, 0)
            self.assertEqual(self.commit(env, self.repo, "other.txt").returncode, 0)
            self.assertEqual(len(fc.find("POST", "/api/check")), 1)
            p = subprocess.run(["git", "push", "-q", "origin", "wave/api-auth-1"], cwd=wt, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            chk = fc.find("POST", "/api/check")
        self.assertEqual(len(chk), 2)
        self.assertEqual(chk[1]["body"]["paths"], ["src/auth/a.py"])
        self.assertEqual(chk[1]["body"]["branch"], "wave/api-auth-1")

    def test_check_push_range(self):
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "e")
        self.commit(self.env(None), name="src/x.py")
        with FakeCoord() as fc:
            p = self.wc(["guard", "check", "--push", "HEAD~2..HEAD"], self.worker_env(fc.url), self.repo)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(fc.find("POST", "/api/check")[0]["body"]["paths"], ["src/x.py"])


def _get(cwd, key):
    p = subprocess.run(["git", "config", "--get", key], cwd=cwd, stdout=subprocess.PIPE, universal_newlines=True)
    return p.stdout.strip() if p.returncode == 0 else None


if __name__ == "__main__":
    unittest.main()
