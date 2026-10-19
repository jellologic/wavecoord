"""migrate-plan (docs/design/spec-layer.md, section 9.6): the coordinator's state carries over, the old ids stay
aliases, overlapping open tasks migrate, odd ids and phases migrate, and the migrated tree runs as is."""

import json
import os
import shutil
import tempfile
import unittest

import _paths
from test_spec_ops import run

from wavecoord.specs import source
from wavecoord.specs.check import check
from wavecoord.specs.migrate import plan_ops
from wavecoord.specs.ops import apply_ops
from wavecoord.specs.plan import SpecTreePlan, coord_id
from wavecoord.specs.tree import Tree

EXEC = {"model": "sonnet", "effort": "low", "max_turns": 5, "budget_usd": 1, "timeout_min": 5}


def flat(tid, state="todo", needs=(), paths=None, phase="p1"):
    return {"id": tid, "title": tid, "phase": phase, "needs": list(needs), "paths": paths or ["src/%s.py" % tid],
            "class": "B", "size": "S", "state": state, "exec": dict(EXEC),
            "sections": {"context": "c", "prompt": "p"}}


def migrate(specs, states=None):
    ops, ids = plan_ops(specs, states=states)
    changes, _, problems = apply_ops({}, ops, coordinator=True)  # as migrate-plan does
    files = {k: v for k, v in changes.items() if v is not None}
    return files, ids, problems


def record(files, new):
    return Tree(files).nodes[new].rec


class MigrateTest(unittest.TestCase):
    def test_coordinator_state_carries_over(self):
        """Item 4: a merged task is done, a parked one in_review, a running one in_progress."""
        specs = [flat("login"), flat("logout", needs=["login"]), flat("signup"), flat("reset")]
        states = {"login": "done", "logout": "review", "signup": "active"}
        files, ids, _ = migrate(specs, states)
        self.assertEqual([record(files, ids[i])["status"] for i in ("login", "logout", "signup", "reset")],
                         ["done", "in_review", "in_progress", "planned"])
        plan = SpecTreePlan(files)
        self.assertEqual([t["id"] for t in plan.ready(states, [])["tasks"]], [coord_id(ids["reset"])])
        # the coordinator's state stays under the old ids: a merge after the move still lands
        later = dict(states, logout="done")
        self.assertEqual({n["id"]: n["state"] for n in plan.graph(later)["nodes"]}[coord_id(ids["logout"])], "done")

    def test_flat_states(self):
        """Item 66: blocked is on_hold; a file's active or review without a live attempt is planned."""
        files, ids, _ = migrate([flat("a", "blocked"), flat("b", "active"), flat("c", "review"), flat("d", "done"),
                                 flat("e", "dropped")])
        self.assertEqual([record(files, ids[i])["status"] for i in "abcde"],
                         ["on_hold", "planned", "planned", "done", "dropped"])

    def test_old_ids_stay_aliases(self):
        """Items 59 and 66: any flat id migrates, is kept as an alias, and names the task for the coordinator."""
        long_id = "a" * 64
        specs = [flat("Auth.Login"), flat("p1_task"), flat("login"), flat(long_id), flat("x" * 40 + "_Y")]
        files, ids, problems = migrate(specs, {"Auth.Login": "done", long_id: "active"})
        self.assertEqual([p for p in check(Tree(files)) if p.level == "error"], [])
        for old, new in ids.items():
            self.assertEqual(record(files, new)["aliases"], [old])
            self.assertLessEqual(len(coord_id(new)), 64)
        plan = SpecTreePlan(files)
        self.assertEqual(plan.get("Auth.Login")["id"], coord_id(ids["Auth.Login"]))
        self.assertEqual(plan.get("p1_task")["id"], coord_id(ids["p1_task"]))
        g = {n["id"]: n["state"] for n in plan.graph({"Auth.Login": "done", long_id: "active"})["nodes"]}
        self.assertEqual(g[coord_id(ids["Auth.Login"])], "done")
        self.assertEqual(g[coord_id(ids[long_id])], "active")

    def test_phase_tags(self):
        """Item 91: a phase that starts with '_' is a valid tag."""
        files, ids, _ = migrate([flat("a", phase="_backlog"), flat("b", phase="__"), flat("c", phase="P1 Core")])
        self.assertEqual(record(files, ids["a"])["tags"], ["backlog"])
        self.assertNotIn("tags", record(files, ids["b"]))
        self.assertEqual(record(files, ids["c"])["tags"], ["p1-core"])

    def test_overlapping_open_tasks(self):
        """Item 25: two open tasks that write the same file migrate; they still never run together."""
        specs = [flat("a", paths=["src/core.py"]), flat("b", "blocked", paths=["src/core.py"]),
                 flat("c", paths=["src/core.py"], needs=["a"]), flat("d", "done", paths=["src/core.py"])]
        files, ids, _ = migrate(specs)
        self.assertEqual([p for p in check(Tree(files)) if p.level == "error"], [])
        self.assertEqual(record(files, ids["b"])["conflicts_with"], [ids["a"]])
        self.assertEqual(record(files, ids["c"])["conflicts_with"], [ids["b"]])  # not a: c needs a
        files, ids, _ = migrate([flat("a", paths=["src/core.py"]), flat("b", paths=["src/core.py"])])
        plan = SpecTreePlan(files)
        a, b = plan.ready({}, [])["tasks"]
        self.assertTrue(plan.clash(a, b))
        self.assertEqual([t["id"] for t in plan.ready({"a": "active"}, [])["tasks"]], [])

    def test_migrated_tree_runs(self):
        """Item 74: with require_approved on, the migrated tasks are ready, and check passes."""
        from wavecoord.plan.adapters import builtin_from_tree
        specs, errors = builtin_from_tree(_paths.FIXTURES, {"dir": "plan/tasks"}).load()
        self.assertEqual(errors, {})
        files, ids, _ = migrate(specs)
        self.assertEqual([p for p in check(Tree(files)) if p.level == "error"], [])
        ready = SpecTreePlan(files, require_approved=True).ready({}, [])
        self.assertTrue(ready["tasks"], ready["why_not"])
        self.assertFalse([w for w in ready["why_not"].values() if "requirement" in w or "is draft" in w])


class MigrateCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wc-migr-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copytree(os.path.join(_paths.FIXTURES, "plan", "tasks"), os.path.join(self.tmp, "plan", "tasks"))
        os.makedirs(os.path.join(self.tmp, ".git"))
        os.makedirs(os.path.join(self.tmp, ".claude"))
        with open(os.path.join(self.tmp, ".claude", "wavecoord.json"), "w") as f:
            json.dump({"project": "demo", "coord_url": "http://127.0.0.1:9"}, f)

    def test_states_from_a_file(self):
        p = os.path.join(self.tmp, "states.json")
        with open(p, "w") as f:
            json.dump({"session-store": "done", "api-auth": "active"}, f)
        out = run("migrate-plan", "--states", p, cwd=self.tmp)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("plan PR", out.stdout)
        fs = Tree(source.read_tree(self.tmp, "specs"))
        self.assertEqual(fs.nodes["legacy/unsorted/unsorted/session-store"].rec["status"], "done")
        self.assertEqual(fs.nodes["legacy/unsorted/unsorted/api-auth"].rec["status"], "in_progress")

    def test_refuses_without_the_coordinator(self):
        out = run("migrate-plan", cwd=self.tmp)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("--no-coord", out.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "specs")))


if __name__ == "__main__":
    unittest.main()
