"""`wavecoord spec`: the S1 checks, the reads, fmt, and the git-ref source."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import _paths
from wavecoord.specs import check as checkmod
from wavecoord.specs import doc, query, source
from wavecoord.specs.tree import Tree, classify

FIX = os.path.join(_paths.FIXTURES, "specs")
ROT = "auth/sessions/token-rotation/_node.md"
IMPL = "auth/sessions/token-rotation/tasks/rotate-impl.md"


def files():
    return source.read_tree(_paths.FIXTURES, "specs")


def run(*args, cwd):
    env = dict(os.environ, PYTHONPATH=_paths.SRC)
    return subprocess.run([sys.executable, "-m", "wavecoord", "spec"] + list(args), cwd=cwd, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)


def git(cwd, *args):
    subprocess.run(["git"] + list(args), cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class Repo(unittest.TestCase):
    """A temp git repo with the fixture tree under specs/."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wc-spec-"))
        self.repo = os.path.join(self.tmp, "repo")
        shutil.copytree(FIX, os.path.join(self.repo, "specs"))
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "specs")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, rel):
        return os.path.join(self.repo, "specs", rel)


class LayoutTest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify("auth/_node.md")[:2], ("capability", "auth"))
        self.assertEqual(classify("auth/s/r/_node.md")[:2], ("requirement", "auth/s/r"))
        self.assertEqual(classify("auth/s/r/tasks/t.md")[:3], ("task", "auth/s/r/t", "auth/s/r"))
        self.assertEqual(classify("auth/s/decisions/2026-10-19-x.md")[:3],
                         ("decision", "auth/s/decisions/2026-10-19-x", "auth/s"))
        for bad in ("README.md", "auth/notes.txt", "Auth/_node.md", "a/b/c/d/_node.md", "auth/tasks/t.md",
                    "auth/decisions/x.md", "auth/decisions/2026-02-30-x.md", "a/b/c/tasks/T.md", "tasks/_node.md"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    classify(bad)

    def test_fixture_tree_is_clean(self):
        t = Tree(files())
        self.assertEqual([str(p) for p in checkmod.check(t)], [])
        self.assertEqual([str(p) for p in checkmod.lint(t)], [])
        self.assertEqual(t.children("auth/sessions"), ["auth/sessions/session-store", "auth/sessions/token-rotation"])
        self.assertEqual(len(t.decisions), 1)


class CheckRuleTest(unittest.TestCase):
    """One case per S1 rule: change one file of the clean fixture tree, expect that rule."""

    def rules(self, rel, change):
        fs = files()
        if change is None:
            del fs[rel]
        else:
            fs[rel] = change(fs[rel])
        return {p.rule for p in checkmod.check(Tree(fs)) if p.level == "error"}

    def test_rules(self):
        cases = [
            ("layout", "auth/sessions/_node.md", None),  # children without a parent record
            ("kind", ROT, lambda s: s.replace("kind: requirement", "kind: task")),
            ("schema", ROT, lambda s: s.replace("status: approved", "status: done")),
            ("schema", IMPL, lambda s: s.replace("  max_turns: 80", "  max_turns: 0")),
            ("section", ROT, lambda s: s.split("## Statement")[0] + "## Rationale\nwhy\n"),
            ("artifact", ROT, lambda s: s.replace(
                "    contract: rotate(refresh_token) -> TokenPair; the old refresh token is revoked\n", "")),
            ("artifact", ROT, lambda s: s.replace("    from: auth/sessions/session-store\n", "")),
            ("ears", ROT, lambda s: s.replace("the auth service shall issue", "the auth service issues")),
            ("requirement", ROT, lambda s: s.split("## Scenarios")[0] + "## Rationale\nwhy\n"),
            ("format", ROT, lambda s: s.replace("## Rationale", "\n## Rationale")),
            ("frame", ROT, lambda s: s.replace("status: approved\nkind: requirement\nschema: 1\n",
                                               "kind: requirement\nschema: 1\nstatus: approved\n")),
            ("parse", ROT, lambda s: s + "\n## Bogus\nx\n"),
        ]
        for rule, rel, change in cases:
            with self.subTest(rule=rule, rel=rel):
                self.assertIn(rule, self.rules(rel, change))

    def test_ears(self):
        ok = {"The store shall keep sessions.": "ubiquitous", "When x, the store shall y.": "event",
              "While x, the store shall y.": "state", "If x, then the store shall y.": "unwanted",
              "Where x, the store shall y.": "optional", "While x, when y, the store shall z.": "state+event"}
        for s, cls in ok.items():
            self.assertEqual(checkmod.ears_class(s), cls, s)
        for s in ("The store keeps sessions.", "If x, the store shall y.", "The x shall a.\nThe y shall b.",
                  "The x shall a and shall b."):
            self.assertIsNone(checkmod.ears_class(s), s)

    def test_lint(self):
        fs = files()
        fs["auth/_node.md"] = fs["auth/_node.md"].replace(
            "Access tokens live 15 minutes.\nRefresh", "Access tokens live 15 minutes. Refresh")
        fs[ROT] = fs[ROT].replace("## Rationale\n", "## Rationale\n| a | b |\n")
        rules = sorted(p.rule for p in checkmod.lint(Tree(fs)))
        self.assertEqual(rules, ["sentences", "table"])


class QueryTest(unittest.TestCase):
    def test_terms(self):
        t = Tree(files())
        ids = lambda expr: [n.id for n in query.query(t, expr)]
        self.assertEqual(ids("kind=task status=planned"), ["auth/sessions/token-rotation/rotate-impl",
                                                             "auth/sessions/token-rotation/rotate-tests"])
        self.assertEqual(ids("kind=task outputs~src/*"), ["auth/sessions/session-store/store-api",
                                                          "auth/sessions/token-rotation/rotate-impl"])
        self.assertEqual(ids("kind=requirement !needs"), ["auth/sessions/session-store"])
        self.assertEqual(ids("under=auth/sessions/session-store"), ["auth/sessions/session-store",
                                                                    "auth/sessions/session-store/store-api"])
        self.assertEqual(ids("kind=decision"), ["auth/decisions/2026-10-19-rotating-tokens"])
        self.assertEqual(ids("depth=2"), ["auth/sessions"])
        self.assertEqual(ids("kind!=task tags"), ["auth", "auth/sessions/token-rotation"])
        with self.assertRaises(query.QueryError):
            query.parse_query("=x")

    def test_rollup(self):
        t = Tree(files())
        self.assertEqual(query.rollup(t, "auth"), {"done": 1, "dropped": 0, "open": 2, "total": 3})


class CliTest(Repo):
    def test_reads(self):
        out = run("tree", "--json", cwd=self.repo)
        self.assertEqual(out.returncode, 0, out.stderr)
        tree = json.loads(out.stdout)
        self.assertEqual(tree[0]["id"], "auth")
        self.assertEqual(tree[0]["progress"]["done"], 1)
        out = run("show", "auth/sessions/token-rotation", "--json", cwd=self.repo)
        rec = json.loads(out.stdout)
        self.assertRegex(rec["fingerprint"], "^[0-9a-f]{8}$")
        self.assertEqual(rec["children"], ["auth/sessions/token-rotation/rotate-impl",
                                           "auth/sessions/token-rotation/rotate-tests"])
        self.assertEqual(run("fp", "auth/sessions/token-rotation", cwd=self.repo).stdout.strip(), rec["fingerprint"])
        out = run("query", "kind=task status=done", cwd=self.repo)
        self.assertIn("auth/sessions/session-store/store-api", out.stdout)
        self.assertEqual(run("check", cwd=self.repo).returncode, 0)
        self.assertEqual(run("show", "nope", cwd=self.repo).returncode, 2)

    def test_fmt_and_check_exit_codes(self):
        p = self.path(ROT)
        with open(p) as f:
            canon = f.read()
        with open(p, "w") as f:
            f.write(canon.replace("tags:\n  - security\n", "tags: [security]\n").replace("## Rationale", "\n## Rationale"))
        self.assertEqual(run("check", cwd=self.repo).returncode, 1)
        self.assertEqual(run("fmt", "--check", cwd=self.repo).returncode, 1)
        out = run("fmt", cwd=self.repo)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("formatted specs/" + ROT, out.stdout)
        with open(p) as f:
            self.assertEqual(f.read(), canon)
        self.assertEqual(run("fmt", "--check", cwd=self.repo).returncode, 0)

    def test_spec_dir_from_config(self):
        os.rename(os.path.join(self.repo, "specs"), os.path.join(self.repo, "reqs"))
        os.makedirs(os.path.join(self.repo, ".claude"))
        with open(os.path.join(self.repo, ".claude", "wavecoord.json"), "w") as f:
            json.dump({"project": "demo", "spec": {"dir": "reqs"}}, f)
        out = run("query", "kind=capability", cwd=self.repo)
        self.assertEqual(out.stdout.split(), ["capability", "active", "auth"])

    def test_git_ref_equals_working_tree(self):
        self.assertEqual(source.read_ref(self.repo, "main", "specs"), source.read_tree(self.repo, "specs"))
        with open(self.path(ROT), "a") as f:
            f.write("\nchanged after the commit\n")
        ref = run("show", "auth/sessions/token-rotation", "--ref", "main", cwd=self.repo).stdout
        with open(os.path.join(FIX, ROT)) as f:
            self.assertEqual(ref, f.read())
        self.assertEqual(source.read_ref(self.repo, "main", "nothing-here"), {})


@unittest.skipUnless(os.environ.get("WAVECOORD_PERF"), "WAVECOORD_PERF=1 runs the perf test")
class PerfTest(Repo):
    def test_2000_records_from_a_ref_under_a_second(self):
        with open(self.path(IMPL)) as f:
            task = f.read()
        for c in range(20):
            for r in range(25):
                d = self.path("cap%d/f/r%d" % (c, r))
                os.makedirs(os.path.join(d, "tasks"))
                for rel, src in (("_node.md", ROT), ):
                    with open(os.path.join(FIX, src)) as f:
                        text = f.read().replace("needs:\n  - auth/sessions/session-store\n", "")
                    with open(os.path.join(d, rel), "w") as f:
                        f.write(text)
                for k in range(3):
                    with open(os.path.join(d, "tasks", "t%d.md" % k), "w") as f:
                        f.write(task)
            for rel in ("cap%d/_node.md" % c, "cap%d/f/_node.md" % c):
                with open(os.path.join(FIX, "auth/_node.md" if rel.count("/") == 1 else "auth/sessions/_node.md")) as f:
                    text = f.read().replace("kind: capability", "kind: feature") if rel.count("/") == 2 else f.read()
                with open(self.path(rel), "w") as f:
                    f.write(text)
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "big")
        t0 = time.time()
        tree = Tree(source.read_ref(self.repo, "main", "specs"))
        checkmod.check(tree)
        took = time.time() - t0
        self.assertGreaterEqual(len(tree.nodes), 2000)
        self.assertLess(took, 1.0, "%d records in %.2f s" % (len(tree.nodes), took))


if __name__ == "__main__":
    unittest.main()
