"""The merge properties of the spec tree (docs/design/spec-layer.md, sections 8 and 10), with real `git merge`.

P1  random ops on disjoint requirements on two branches merge with no conflict; the result is the
    base plus both branches' file changes, and it checks and formats clean.
P2  distinct sorted inserts into one list merge.
P3  genuine conflicts surface (the same statement, the same status).
P4  a status change and an edit of another key of the same record merge (rule M13).
P5  races that merge cleanly and that check reports: a move against a reference to the old id (an alias
    warning), a statement change against a task planned on the old fingerprint (suspect).

WAVECOORD_SPEC_SEED=<n> runs one seed; WAVECOORD_SPEC_RUNS=<n> adds random seeds;
WAVECOORD_SPEC_OPS=<n> sets the ops per branch (default 20).
"""

import os
import random
import shutil
import subprocess
import tempfile
import unittest

import _paths  # noqa: F401
import specsim
from wavecoord.specs import doc, source
from wavecoord.specs.check import check
from wavecoord.specs.ops import apply_ops
from wavecoord.specs.tree import Tree

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.invalid", GIT_CONFIG_NOSYSTEM="1")


def seeds():
    if os.environ.get("WAVECOORD_SPEC_SEED"):
        return [int(x) for x in os.environ["WAVECOORD_SPEC_SEED"].split(",")]
    extra = int(os.environ.get("WAVECOORD_SPEC_RUNS", "0"))
    return specsim.SEEDS + [random.randrange(1 << 30) for _ in range(extra)]


class GitRepo(object):
    def __init__(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="wc-specmerge-"))
        self.git("init", "-q", "-b", "main")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args, check=True):
        p = subprocess.run(["git"] + list(args), cwd=self.root, env=GIT_ENV, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True)
        if check and p.returncode != 0:
            raise AssertionError("git %s: %s%s" % (" ".join(args), p.stdout, p.stderr))
        return p

    def write(self, files, msg):
        base = os.path.join(self.root, "specs")
        if os.path.isdir(base):
            shutil.rmtree(base)
        for rel, text in files.items():
            p = os.path.join(base, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", msg)

    def files(self):
        return source.read_tree(self.root, "specs")

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def overlay(base, *branches):
    out = dict(base)
    for b in branches:
        for p in set(base) | set(b):
            if base.get(p) != b.get(p):
                if p in b:
                    out[p] = b[p]
                else:
                    out.pop(p, None)
    return out


def changed(base, other):
    return {p for p in set(base) | set(other) if base.get(p) != other.get(p)}


class MergePropertyTest(unittest.TestCase):
    def test_p1_disjoint_ops_merge_clean(self):
        nops = int(os.environ.get("WAVECOORD_SPEC_OPS", "20"))
        for seed in seeds():
            with self.subTest(seed=seed):
                r = random.Random(seed)
                base = specsim.build(r)
                reqs = specsim.requirements(base)
                r.shuffle(reqs)
                own_a, own_b = sorted(reqs[::2]), sorted(reqs[1::2])
                fa, ops_a = specsim.branch_ops(r, base, own_a, "a", nops)
                fb, ops_b = specsim.branch_ops(r, base, own_b, "b", nops)
                self.assertTrue(ops_a and ops_b)
                self.assertFalse(changed(base, fa) & changed(base, fb), "branches touched the same files")
                repo = GitRepo()
                try:
                    repo.write(base, "base")
                    repo.git("checkout", "-q", "-b", "b")
                    repo.write(fb, "b")
                    repo.git("checkout", "-q", "main")
                    repo.write(fa, "a")
                    m = repo.git("merge", "--no-edit", "-q", "b", check=False)
                    self.assertEqual(m.returncode, 0, "seed %d: %s%s" % (seed, m.stdout, m.stderr))
                    merged = repo.files()
                finally:
                    repo.close()
                self.assertEqual(merged, overlay(base, fa, fb))
                tree = Tree(merged)
                errs = [str(p) for p in check(tree) if p.level == "error"]
                self.assertEqual(errs, [], "seed %d" % seed)
                for n in list(tree.nodes.values()):
                    self.assertEqual(doc.fmt(n.text, n.kind), n.text)


def _edit(files, ops):
    changes, _, _ = apply_ops(files, ops)
    out = dict(files)
    for p, t in changes.items():
        if t is None:
            out.pop(p, None)
        else:
            out[p] = t
    return out


class CaseTest(unittest.TestCase):
    """P2 to P5 as fixed cases on a small tree."""

    def setUp(self):
        self.base = specsim.build(random.Random(7), caps=1, feats=1, reqs=2, max_tasks=2)
        self.req = specsim.requirements(self.base)[0]
        self.base = _edit(self.base, [{"op": "add", "id": self.req, "key": "tags", "value": v} for v in ("b", "d", "f")])

    def merge(self, fa, fb):
        repo = GitRepo()
        try:
            repo.write(self.base, "base")
            repo.git("checkout", "-q", "-b", "b")
            repo.write(fb, "b")
            repo.git("checkout", "-q", "main")
            repo.write(fa, "a")
            m = repo.git("merge", "--no-edit", "-q", "b", check=False)
            return m.returncode, repo.files()
        finally:
            repo.close()

    def test_p2_sorted_inserts_merge(self):
        fa = _edit(self.base, [{"op": "add", "id": self.req, "key": "tags", "value": "a"}])
        fb = _edit(self.base, [{"op": "add", "id": self.req, "key": "tags", "value": "e"}])
        rc, merged = self.merge(fa, fb)
        self.assertEqual(rc, 0)
        self.assertEqual(doc.parse(merged[self.req + "/_node.md"])["tags"], ["a", "b", "d", "e", "f"])

    def test_p3_genuine_conflicts_surface(self):
        st = lambda s: [{"op": "set_section", "id": self.req, "section": "Statement",
                         "value": "When %s happens, the service shall answer it." % s}]
        rc, _ = self.merge(_edit(self.base, st("x")), _edit(self.base, st("y")))
        self.assertNotEqual(rc, 0)
        task = specsim.tasks_of(self.base, self.req)[0]
        rc, _ = self.merge(_edit(self.base, [{"op": "set_status", "id": task, "status": "on_hold"}]),
                           _edit(self.base, [{"op": "set_status", "id": task, "status": "draft"}]))
        self.assertNotEqual(rc, 0)

    def test_p4_status_and_other_keys_merge(self):
        task = specsim.tasks_of(self.base, self.req)[0]
        fa = _edit(self.base, [{"op": "set_status", "id": task, "status": "on_hold"}])
        fb = _edit(self.base, [{"op": "set", "id": task, "key": "title", "value": "A new title"},
                               {"op": "add", "id": task, "key": "tags", "value": "x"}])
        rc, merged = self.merge(fa, fb)
        self.assertEqual(rc, 0)
        rec = doc.parse(merged["%s/tasks/%s.md" % (self.req, task.rsplit("/", 1)[1])])
        self.assertEqual((rec["status"], rec["title"], rec["tags"]), ("on_hold", "A new title", ["x"]))

    def test_p5_races(self):
        other = specsim.requirements(self.base)[1]
        # a move on one branch, a reference to the old id on the other
        fa = _edit(self.base, [{"op": "mv", "from": other, "to": other.rsplit("/", 1)[0] + "/moved"}])
        fb = _edit(self.base, [{"op": "add", "id": self.req, "key": "conflicts_with", "value": other}])
        rc, merged = self.merge(fa, fb)
        self.assertEqual(rc, 0)
        probs = check(Tree(merged))
        self.assertEqual([p for p in probs if p.level == "error"], [])
        self.assertIn("alias", {p.rule for p in probs})
        # a statement change on one branch, a new task planned on the old fingerprint on the other
        fa = _edit(self.base, [{"op": "set_section", "id": self.req, "section": "Statement",
                                "value": "When the rules change, the service shall answer it."}])
        fb = _edit(self.base, [specsim._task(self.req + "/late", "src/late.py", [{"id": "src/late.py", "kind": "file"}])])
        rc, merged = self.merge(fa, fb)
        self.assertEqual(rc, 0)
        sus = {p.id for p in check(Tree(merged)) if p.rule == "suspect"}
        self.assertIn(self.req + "/late", sus)


if __name__ == "__main__":
    unittest.main()
