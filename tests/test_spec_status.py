"""Status commits, verify at merge, the merge trailers (phase S4; docs/design/spec-layer.md, sections 7 and 7.5).

Unit tests of the reconcile and the one-line write; then a task's life on a real coordinator, launcher, fake
claude and fake gh: in_progress at dispatch, in_review when its PR is open, done at the merge, each as a status
commit on main; the refinery's Verify from main; the fallback to a status PR when main refuses the push."""

import os
import random
import stat
import unittest

import _paths
import specsim
from launcher_env import sh
from test_spec_lead import SpecLeadCase

from wavecoord.specs import doc, source
from wavecoord.specs.ops import apply_ops
from wavecoord.specs import status
from wavecoord.specs.status import wanted
from wavecoord.specs.tree import Tree

IMPL = "auth.sessions.token-rotation.rotate-impl"
IMPL_FILE = "specs/auth/sessions/token-rotation/tasks/rotate-impl.md"


def fixture_with(verify="true", acceptance="true", drop_tests=False):
    """The fixture tree, with checks that run in a test repo. drop_tests: rotate-impl is the requirement's last task."""
    fs = source.read_tree(_paths.FIXTURES, "specs")
    ops = [{"op": "set_status", "id": "auth/sessions/token-rotation/rotate-tests", "status": "dropped"}] if drop_tests else []
    ops += [{"op": "set_section", "id": "auth/sessions/token-rotation", "section": "Verify", "value": [verify]},
           {"op": "set_section", "id": "auth/sessions/token-rotation/rotate-impl", "section": "Acceptance",
            "value": [acceptance]},
           {"op": "rebase_basis", "task": "auth/sessions/token-rotation/rotate-impl"},
           {"op": "rebase_basis", "task": "auth/sessions/token-rotation/rotate-tests"}]
    changes, _, _ = apply_ops(fs, ops)
    fs.update({k: v for k, v in changes.items() if v is not None})
    return fs


class UnitTest(unittest.TestCase):
    def test_wanted(self):
        t = Tree(source.read_tree(_paths.FIXTURES, "specs"))
        ch = wanted(t, {IMPL: "active", "auth.sessions.session-store.store-api": "review"})
        self.assertEqual([(c[0], c[2], c[3]) for c in ch],
                         [("auth/sessions/token-rotation/rotate-impl", "planned", "in_progress")])  # done is never undone
        fs = source.read_tree(_paths.FIXTURES, "specs")
        rel = "auth/sessions/token-rotation/tasks/rotate-tests.md"
        fs[rel] = status.set_status_line(fs[rel], "in_review")
        ch = wanted(Tree(fs), {})
        self.assertEqual([(c[0], c[3]) for c in ch], [("auth/sessions/token-rotation/rotate-tests", "planned")])

    def test_set_status_line_changes_one_line(self):
        canon = source.read_tree(_paths.FIXTURES, "specs")["auth/sessions/token-rotation/tasks/rotate-impl.md"]
        # a valid file that is not in canonical form: a status write must not reformat it (S08)
        messy = canon.replace("paths:\n  - src/auth/rotate.py\n", "paths: [src/auth/rotate.py]\n")
        for text in (canon, messy):
            new = status.set_status_line(text, "in_progress")
            diff = [(a, b) for a, b in zip(text.split("\n"), new.split("\n")) if a != b]
            self.assertEqual(diff, [("status: planned", "status: in_progress")])
            self.assertEqual(len(new.split("\n")), len(text.split("\n")))
        self.assertIn("## Prompt\nImplement", status.set_status_line(canon, "done"))


class StatusFlowCase(SpecLeadCase):
    EXTRA_CFG = {"spec": {"status_flush_s": 0}}

    def setUp(self):
        self.FILES = fixture_with(**self.CHECKS)
        super().setUp()

    CHECKS = {}

    def main_status(self, path=IMPL_FILE):
        sh(["git", "fetch", "-q", "origin"], self.repo)
        return doc.parse(sh(["git", "show", "main:" + path], self.origin) + "\n")["status"]

    def status_commits(self):
        out = sh(["git", "log", "--format=%an|%s|%b", "main"], self.origin)
        return [l for l in out.split("\n") if "wavecoord: status" in l or "Wavecoord-Status:" in l]

    def run_impl(self):
        os.environ.update(FAKE_CLAUDE_COMMIT="1", FAKE_CLAUDE_COMMIT_FILE="src/auth/rotate.py",
                          FAKE_CLAUDE_COMMIT_TEXT="rotate\n")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FAKE_CLAUDE_COMMIT", "FAKE_CLAUDE_COMMIT_FILE",
                                                                   "FAKE_CLAUDE_COMMIT_TEXT")])
        d = self.lead.dispatch(IMPL)
        self.assertEqual(self.main_status(), "in_progress")
        self.collect_until(d["id"])
        return d


class StatusLifecycleTest(StatusFlowCase):
    CHECKS = {"verify": "true", "acceptance": "true"}

    def test_status_follows_the_task_and_verify_passes(self):
        d = self.run_impl()
        self.assertEqual(self.main_status(), "in_review")
        pr = [p for p in self.gh()["prs"] if p["head"] == d["branch"]][0]
        self.assertTrue(pr["body"].rstrip().endswith(
            "Spec-Task: auth/sessions/token-rotation/rotate-impl\nSpec-Basis: %s" % doc.parse(
                self.FILES["auth/sessions/token-rotation/tasks/rotate-impl.md"])["basis"]), pr["body"][-200:])
        out = self.lead.refinery_merge()
        self.assertEqual([r["state"] for r in out["results"]], ["merged"], out)
        self.assertEqual(self.main_status(), "done")
        merge = self.events("refinery.merge")[-1]["payload"]
        # rotate-tests is still open: the requirement's Verify waits for the last task; Acceptance ran
        self.assertEqual((merge["spec"]["verified"], merge["spec"]["requirement_pending"]),
                         (False, ["auth/sessions/token-rotation/rotate-tests"]))
        self.assertEqual(merge["spec"]["requirement"], "auth/sessions/token-rotation")
        commits = self.status_commits()
        self.assertTrue(any("Wavecoord-Status: %s planned in_progress" % IMPL in c for c in commits), commits)
        self.assertTrue(any("Wavecoord-Status: %s in_review done" % IMPL in c for c in commits), commits)
        self.assertTrue(all(c.startswith("wavecoord|") for c in commits if "|wavecoord: status" in c), commits)
        st = [e["payload"] for e in self.events("spec.status")]
        self.assertEqual([c["to"] for p in st for c in p["changes"] if c["task"] == IMPL],
                         ["in_progress", "in_review", "done"])
        # the merged task unblocks the next one
        self.assertIn("auth.sessions.token-rotation.rotate-tests", [t["id"] for t in self.lead.plan_ready()["tasks"]])
        # every status commit changed status lines only
        for sha in sh(["git", "log", "--format=%H", "--grep=^wavecoord: status", "main"], self.origin).split():
            diff = sh(["git", "show", "--format=", "-U0", sha], self.origin)
            changed = [l for l in diff.split("\n") if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
            self.assertTrue(changed and all(l[1:].startswith("status: ") for l in changed), diff)


class VerifyFailsTest(StatusFlowCase):
    CHECKS = {"verify": "false", "acceptance": "true", "drop_tests": True}

    def test_verify_from_main_blocks(self):
        self.run_impl()
        out = self.lead.refinery_merge()
        r = out["results"][0]
        self.assertEqual((r["state"], r["reason"]), ("blocked", "verify_failed"), r)
        self.assertEqual(self.main_status(), "in_review")


class VerifyLastTaskTest(StatusFlowCase):
    CHECKS = {"verify": "test -d specs", "acceptance": "true", "drop_tests": True}

    def test_the_last_task_verifies_the_requirement(self):
        self.run_impl()
        out = self.lead.refinery_merge()
        self.assertEqual([r["state"] for r in out["results"]], ["merged"], out)
        spec = self.events("refinery.merge")[-1]["payload"]["spec"]
        self.assertEqual((spec["verified"], spec["requirement_pending"], spec["commands"]), (True, [], 2))


class StatusPrFallbackTest(StatusFlowCase):
    CHECKS = {"verify": "true", "acceptance": "true"}

    def test_protected_main_gets_a_status_pr(self):
        hook = os.path.join(self.origin, "hooks", "pre-receive")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\nwhile read old new ref; do\n"
                    "  if [ \"$ref\" = refs/heads/main ] && git log -1 --format=%s \"$new\" | grep -q '^wavecoord: status';"
                    " then echo 'main is protected' >&2; exit 1; fi\ndone\n")
        os.chmod(hook, os.stat(hook).st_mode | stat.S_IXUSR)
        os.environ.update(FAKE_CLAUDE_SLEEP="5")
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_SLEEP", None)
        self.lead.dispatch(IMPL)
        self.assertEqual(self.main_status(), "in_progress")  # merged through the status PR
        st = self.events("spec.status")[-1]["payload"]
        self.assertEqual((st["via"], st["merged"]), ("pr", True))
        heads = [p["head"] for p in self.gh()["prs"]]
        self.assertIn("wavecoord/status", heads)


class RandomTreeStatusTest(SpecLeadCase):
    """Status commits on a random tree: one flush changes exactly the tasks whose coordinator state moved."""

    FILES = specsim.build(random.Random(5), caps=1, feats=2, reqs=2, max_tasks=2)
    EXTRA_CFG = {"spec": {"status_flush_s": 0}}

    def test_flush_writes_only_what_moved(self):
        from wavecoord.specs.plan import coord_id
        tasks = sorted(n.id for n in Tree(self.FILES).nodes.values() if n.kind == "task")
        states = {coord_id(tasks[0]): "active", coord_id(tasks[1]): "review"}
        self.lead.states = lambda snap=None: states
        out = self.lead.status_flush(force=True)
        self.assertEqual(sorted((c["task"], c["to"]) for c in out["changes"]),
                         sorted([(coord_id(tasks[0]), "in_progress"), (coord_id(tasks[1]), "in_review")]))
        self.assertEqual(self.lead.status_flush(force=True), {"changes": []})  # idempotent
        err, out = self.mcp("spec_status_sync")
        self.assertFalse(err, out)
        self.assertEqual(out, {"changes": []})


if __name__ == "__main__":
    unittest.main()
