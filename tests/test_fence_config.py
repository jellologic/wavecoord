"""Fence and lead-config fixes of the second review (fix/fence-config): diff flags, the pre-push and
pre-merge-commit guards, the Stop hook's paths, the refinery's changed files, the override journal batches,
the schema patterns and the repo config lookup. The seeded defects D31 to D37 are in test_fence_defects.py."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

import _paths  # noqa: F401
from fenceworld import git
from wavecoord import client, fence, gatecheck, guard, hooks, schemaload


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wc-fcfg-")
        self.repo = os.path.join(self.tmp, "r")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q")
        self.write("a.txt")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "a")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text="x\n", cwd=None):
        p = os.path.join(cwd or self.repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a") as f:
            f.write(text)


class LeadConfigKindTest(unittest.TestCase):
    def test_kinds(self):
        """Items 53, 54, 92: any case, any depth; only the exact root files get a key compare."""
        k = gatecheck.lead_config_kind
        self.assertEqual(k(".claude/wavecoord.json"), "config")
        self.assertEqual(k(".claude/settings.json"), "settings")
        self.assertEqual(k(".claude/settings.local.json"), "settings")
        for p in (".claude/Settings.json", ".CLAUDE/wavecoord.json", "pkg/.claude/settings.json",
                  "pkg/.claude/wavecoord.json", ".claude/agents/reviewer.md", ".claude/commands/x.md",
                  ".claude/skills/s/SKILL.md", ".mcp.json", "pkg/.MCP.json", "CLAUDE.local.md",
                  ".claude-plugin/plugin.json", "plugin/.claude-plugin/marketplace.json", ".claude"):
            self.assertEqual(k(p), "file", p)
        for p in ("hooks/hooks.json", "plugin/Hooks/hooks.json", "x/hooks/HOOKS.json"):
            self.assertEqual(k(p), "hooks", p)
        for p in ("src/a.py", "docs/claude.md", "CLAUDE.md", "hooks/run.sh", "x.claude/y", "mcp.json"):
            self.assertIsNone(k(p), p)


class DiffFlagsTest(Repo):
    def test_changed_files_lists_both_names_of_a_rename_and_fails_closed(self):
        """Item 63: a moved protected file is a protected path (--no-renames); a git error is not []."""
        from wavecoord.core import ApiError
        from wavecoord.lead.collect import CollectMixin
        self.write("prot/secret.txt", "a long enough secret line to be found as a rename\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "p")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.repo, "checkout", "-q", "-b", "b")
        os.makedirs(os.path.join(self.repo, "solo"))
        git(self.repo, "mv", "prot/secret.txt", "solo/secret.txt")
        git(self.repo, "commit", "-q", "-m", "mv")
        git(self.repo, "update-ref", "refs/remotes/origin/b", "HEAD")
        lead = CollectMixin()
        lead.repo = self.repo
        lead.default_branch = lambda: "main"
        self.assertEqual(lead.changed_files("b"), ["prot/secret.txt", "solo/secret.txt"])
        with self.assertRaises(ApiError):
            lead.changed_files("gone")
        self.assertEqual(lead.changed_files("gone", strict=False), [])


class GuardTest(Repo):
    def test_push_with_an_unknown_remote_sha_lists_the_new_commits(self):
        """Item 60: a remote head this repo does not have (a forced push over a moved remote branch) falls back
        to the commits no remote-tracking ref has; a merge commit lists what it brings in."""
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.repo, "checkout", "-q", "-b", "other")
        self.write("foreign/x.txt")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "foreign")
        git(self.repo, "update-ref", "refs/remotes/origin/other", "HEAD")
        git(self.repo, "checkout", "-q", "-b", "w", "origin/main")
        self.write("outside.txt")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "o")
        git(self.repo, "merge", "-q", "--no-ff", "-m", "m", "origin/other")
        local = git(self.repo, "rev-parse", "HEAD").strip()
        stdin = io.StringIO("refs/heads/w %s refs/heads/w %s\n" % (local, "1" * 40))
        self.assertEqual(guard.push_paths(self.repo, "-", stdin), ["foreign/x.txt", "outside.txt"])
        # a local sha that git does not know: no paths can be computed, so None (the guard refuses in enforce)
        stdin = io.StringIO("refs/heads/w %s refs/heads/w %s\n" % ("2" * 40, "1" * 40))
        self.assertIsNone(guard.push_paths(self.repo, "-", stdin))

    def test_install_writes_a_pre_merge_commit_guard(self):
        """Item 61: a merge that makes a commit runs the staged check (pre-merge-commit)."""
        mark = os.path.join(self.tmp, "ran")
        fake = os.path.join(self.tmp, "fakeguard")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\necho \"$@\" >> %s\nexit 1\n" % mark)
        os.chmod(fake, 0o755)
        guard.install(self.repo, cmd=[fake], out=io.StringIO(), env={"WAVECOORD_AGENT_ID": "w1"})
        hd = os.path.join(self.repo, ".git", "wavecoord-hooks")
        self.assertTrue(os.access(os.path.join(hd, "pre-merge-commit"), os.X_OK))
        git(self.repo, "checkout", "-q", "-b", "other")
        self.write("foreign/x.txt")
        git(self.repo, "add", "-A")
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "commit", "-q",
                        "--no-verify", "-m", "f"], cwd=self.repo, check=True)
        git(self.repo, "checkout", "-q", "-")
        self.write("mine.txt")
        git(self.repo, "add", "-A")
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "commit", "-q",
                        "--no-verify", "-m", "m"], cwd=self.repo, check=True)
        p = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "merge", "--no-ff",
                            "-m", "m", "other"], cwd=self.repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(p.returncode, 0)
        with open(mark) as f:
            self.assertIn("guard check --staged", f.read())

    def test_e3_sees_a_cherry_picked_foreign_commit(self):
        """Item 61: git runs no commit hook for a cherry-pick; the net diff of E3 and E5 has its paths."""
        git(self.repo, "checkout", "-q", "-b", "other")
        self.write("foreign/x.txt")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "f")
        git(self.repo, "checkout", "-q", "-b", "w", "HEAD~1")
        git(self.repo, "cherry-pick", "other")
        self.assertEqual(fence.diff_paths(self.repo, "other~1...HEAD"), ["foreign/x.txt"])


class StopHookPathsTest(Repo):
    def test_non_ascii_and_unglobbable_paths(self):
        """Item 62: -z (no C-quoted name), and a path that no glob can hold is outside_lease without a server
        call, so the other paths are still checked."""
        for rel in ("café.md", "outside.txt", "a b.txt"):
            self.write(rel)
        git(self.repo, "add", "-A")
        h = types.SimpleNamespace(cwd=self.repo)
        paths = hooks.Hook.changed_paths(h)
        self.assertEqual(paths, sorted(["a b.txt", "café.md", "outside.txt"]))
        sent = []

        class C(object):
            def post(self, path, body):
                sent.append(body)
                return 200, {"violations": [{"kind": "outside_lease", "path": p} for p in body["paths"]]}
        h = types.SimpleNamespace(id=types.SimpleNamespace(agent="w1", task="t"), state={}, c=C())
        out = hooks.Hook.check(h, paths)
        self.assertEqual(sent[0]["paths"], ["café.md", "outside.txt"])
        self.assertEqual(sorted(v["path"] for v in out), ["a b.txt", "café.md", "outside.txt"])


class JournalBatchTest(unittest.TestCase):
    def test_batches_stay_under_the_body_limit(self):
        """Item 56: a batch is cut by size, so a journal of long entries never gets a 413."""
        rec = {"ts": 1.0, "n": 1, "agent": "w1", "task": "t", "epoch": 1, "path": "p/" + "d" * 200, "point": "commit",
               "nonce": "ab" * 8, "created": 1.0, "until": 2.0, "reason": "r" * 500}
        entries = [dict(rec, n=i) for i in range(1000)]
        batch, body = fence.journal_batch(entries, "w1")
        self.assertGreater(len(batch), 1)
        self.assertLessEqual(len(json.dumps(body, separators=(",", ":")).encode()), fence.journal_body_max())
        self.assertEqual(batch, entries[:len(batch)])
        batch, _ = fence.journal_batch(entries[:1], "w1", limit=10)
        self.assertEqual(len(batch), 1)  # one entry always goes


class SchemaPatternTest(unittest.TestCase):
    def test_dollar_is_the_end_of_the_string(self):
        """Item 99: in JSON Schema `$` matches only at the end; Python's also before a final newline."""
        self.assertEqual(schemaload.errors("t1", "api", "#/definitions/task_id"), [])
        self.assertTrue(schemaload.errors("t1\n", "api", "#/definitions/task_id"))
        self.assertTrue(schemaload.errors("local\n", "api", "#/definitions/host"))
        from wavecoord.jsonschema_lite import ecma_anchors
        self.assertEqual(ecma_anchors(r"^a[$]\$(/|$)$"), r"^a[$]\$(/|\Z)\Z")
        self.assertEqual(ecma_anchors(r"[^]$]$"), r"[^]$]\Z")


class ClassifyTest(Repo):
    def test_dot_git_in_another_case_is_protected(self):
        """Item 57: `.GIT` is git internals on a folding disk; a worker never writes it."""
        t = fence.classify(os.path.join(self.repo, ".GIT", "config"), self.repo,
                           {"WAVECOORD_WORKTREE": self.repo, "WAVECOORD_TASK": "t"}, True)
        self.assertEqual(t.kind, "protected_local")

    def test_nested_worktree_of_the_same_repo_keeps_its_top(self):
        """Item 58: only a nested repo of another repo is checked from the own worktree."""
        sub = os.path.join(self.repo, ".claude", "worktrees", "x")
        git(self.repo, "worktree", "add", "-q", "-b", "x", sub)
        t = fence.classify(os.path.join(sub, "src", "a.py"), self.repo,
                           {"WAVECOORD_WORKTREE": self.repo, "WAVECOORD_TASK": "t"}, True)
        self.assertEqual((t.kind, t.rel), ("check", "src/a.py"))


class RepoConfigTest(Repo):
    def test_a_nested_config_is_not_the_repo_config(self):
        """Item 92: only the config at the repo top counts; a nested pkg/.claude/wavecoord.json is ignored."""
        self.write(".claude/wavecoord.json", json.dumps({"project": "root"}))
        self.write("pkg/web/.claude/wavecoord.json", json.dumps({"coord_url": "http://192.0.2.1:1"}))
        self.assertEqual(client.find_repo_config(os.path.join(self.repo, "pkg", "web")), {"project": "root"})
        os.remove(os.path.join(self.repo, ".claude", "wavecoord.json"))
        self.assertIsNone(client.find_repo_config(os.path.join(self.repo, "pkg", "web")))
        # outside a repo, the nearest file above
        d = os.path.join(self.tmp, "plain", "x")
        self.write(".claude/wavecoord.json", json.dumps({"project": "plain"}), cwd=os.path.join(self.tmp, "plain"))
        os.makedirs(d)
        self.assertEqual(client.find_repo_config(d), {"project": "plain"})


if __name__ == "__main__":
    unittest.main()
