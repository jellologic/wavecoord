"""The spec tree as the lead's plan store, without a coordinator: conflicts_with in readiness and in a wave, the
coordinator's state through aliases after a move, an orphan task, and a stale in_progress in the files."""

import unittest

import _paths

from wavecoord.specs import source
from wavecoord.specs.check import check
from wavecoord.specs.ops import apply_ops
from wavecoord.specs.plan import SpecTreePlan
from wavecoord.specs.status import set_status_line, wanted
from wavecoord.specs.tree import Tree

REQ = "auth/sessions/token-rotation"
IMPL = "auth.sessions.token-rotation.rotate-impl"
TESTS = "auth.sessions.token-rotation.rotate-tests"
DB = "auth.sessions.token-rotation.db-migrate"
CORE = "auth.sessions.token-rotation.rotate-core"
EXEC = {"model": "sonnet", "effort": "low", "max_turns": 5, "budget_usd": 1, "timeout_min": 5}


def fixture(ops=()):
    fs = source.read_tree(_paths.FIXTURES, "specs")
    if ops:
        changes, _, _ = apply_ops(fs, list(ops))
        for k, v in changes.items():
            if v is None:
                fs.pop(k, None)
            else:
                fs[k] = v
    return fs


def ids(r):
    return [t["id"] for t in r["tasks"]]


def set_status(fs, tid, st):
    rel = tid.replace(".", "/").rsplit("/", 1)
    rel = "%s/tasks/%s.md" % (rel[0], rel[1])
    fs[rel] = set_status_line(fs[rel], st)


DB_TASK = {"op": "create", "id": REQ + "/db-migrate", "fields": {
    "title": "Migrate the token table", "status": "planned", "paths": ["db/migrations/**"], "class": "B",
    "size": "S", "exec": EXEC, "conflicts_with": [REQ + "/rotate-impl"],
    "outputs": [{"id": "db/migrations/**", "kind": "files"}]},
    "sections": {"Context": "The token table changes.", "Prompt": "Migrate it."}}


class ConflictsTest(unittest.TestCase):
    """Items 1 and 19: conflicts_with is part of predicate P for readiness and for a wave."""

    def setUp(self):
        self.fs = fixture([DB_TASK])
        self.assertEqual([p for p in check(Tree(self.fs)) if p.level == "error"], [])

    def test_not_ready_while_the_conflicting_task_runs(self):
        plan = SpecTreePlan(self.fs)
        self.assertEqual(ids(plan.ready({}, [])), [DB, IMPL])
        for st in ("active", "review"):
            r = plan.ready({IMPL: st}, [])
            self.assertEqual(ids(r), [], st)
            self.assertIn("conflicts with %s" % IMPL, r["why_not"][DB])
        self.assertEqual(ids(plan.ready({IMPL: "done"}, [])), [DB, TESTS])

    def test_clash_between_ready_tasks(self):
        plan = SpecTreePlan(self.fs)
        a, b = plan.ready({}, [])["tasks"]
        self.assertIn("conflicts with", plan.clash(a, b))
        self.assertIn("conflicts with", plan.clash(b, a))
        free = dict(DB_TASK, fields=dict(DB_TASK["fields"]))
        del free["fields"]["conflicts_with"]
        other = SpecTreePlan(fixture([free]))
        a, b = other.ready({}, [])["tasks"]
        self.assertIsNone(other.clash(a, b))
        self.assertEqual(ids(other.ready({IMPL: "active"}, [])), [DB])


class AliasStateTest(unittest.TestCase):
    """Items 3, 18, 29 and 56: a moved task keeps the coordinator's state of its old id."""

    def setUp(self):
        self.fs = fixture([{"op": "mv", "from": REQ + "/rotate-impl", "to": REQ + "/rotate-core"}])
        self.tree = Tree(self.fs)

    def test_merged_under_the_old_id(self):
        plan = SpecTreePlan(self.fs)
        r = plan.ready({IMPL: "done"}, [])
        self.assertEqual(ids(r), [TESTS])
        self.assertNotIn(CORE, ids(r))

    def test_running_under_the_old_id(self):
        set_status(self.fs, CORE, "in_progress")
        plan = SpecTreePlan(self.fs)
        r = plan.ready({IMPL: "active"}, [])
        self.assertEqual(ids(r), [])
        self.assertEqual(r["why_not"][CORE], "status in_progress")
        self.assertEqual(wanted(Tree(self.fs), {IMPL: "active"}), [])
        self.assertEqual([w[3] for w in wanted(Tree(self.fs), {IMPL: "done"})], ["done"])
        self.assertEqual([w[3] for w in wanted(Tree(self.fs), {IMPL: "review"})], ["in_review"])

    def test_get_by_alias_or_tree_id(self):
        plan = SpecTreePlan(self.fs)
        for name in (IMPL, CORE, CORE.replace(".", "/"), (REQ + "/rotate-impl")):
            spec = plan.get(name)
            self.assertEqual(spec["id"], CORE, name)
        self.assertEqual(plan.ids(plan.get(CORE)), [CORE, IMPL])

    def test_strongest_state_wins(self):
        plan = SpecTreePlan(self.fs)
        g = plan.graph({IMPL: "done", CORE: "active"})
        self.assertEqual({n["id"]: n["state"] for n in g["nodes"]}[CORE], "done")


class OrphanTest(unittest.TestCase):
    """Item 22: a task under a folder without _node.md is reported, not a crash."""

    def setUp(self):
        self.fs = fixture()
        self.fs["auth/sessions/orphan/tasks/t1.md"] = self.fs[
            "auth/sessions/token-rotation/tasks/rotate-impl.md"].replace("src/auth/rotate.py", "src/orphan.py")

    def test_reads_do_not_crash(self):
        for approved in (True, False):
            plan = SpecTreePlan(self.fs, require_approved=approved)
            plan.graph({})
            self.assertEqual(plan.get("auth.sessions.orphan.t1")["tree"]["requirement"], "auth/sessions/orphan")
            r = plan.ready({}, [])
            self.assertNotIn("auth.sessions.orphan.t1", ids(r))
            self.assertIn("auth.sessions.orphan.t1", r["why_not"])
            self.assertFalse(plan.validate()["ok"])


class StaleStatusTest(unittest.TestCase):
    """Item 87: in_progress or in_review in the files with nothing live is planned for readiness."""

    def test_stale_status_is_ready_again(self):
        for st in ("in_progress", "in_review"):
            fs = fixture()
            set_status(fs, IMPL, st)
            plan = SpecTreePlan(fs)
            self.assertEqual(ids(plan.ready({}, [])), [IMPL], st)
            self.assertEqual(plan.get(IMPL)["state"], "todo")
            self.assertEqual(ids(plan.ready({IMPL: "active"}, [])), [])


class ProducerTest(unittest.TestCase):
    """Item 57: each input of the shaped spec names the task that makes it."""

    def test_inputs_carry_producers(self):
        fs = fixture([{"op": "set_artifact", "id": REQ + "/rotate-impl", "list": "inputs", "artifact":
                       {"id": "auth.session-store.api", "kind": "interface", "from": "auth/sessions/session-store"}}])
        spec = SpecTreePlan(fs).get(IMPL)
        self.assertEqual(spec["tree"]["inputs"][0]["producers"], ["auth/sessions/session-store/store-api"])
        from wavecoord.launcher.client import prompt_from_spec
        self.assertIn("from `auth/sessions/session-store` (made by `auth/sessions/session-store/store-api`)",
                      prompt_from_spec(spec))


class FlatIdSpellsANodeTest(unittest.TestCase):
    """Review item 8: a migrated flat id with dots that also spells a feature's path still names its task."""

    def test_get_resolves_the_alias_first(self):
        from wavecoord.specs import migrate
        from wavecoord.specs.plan import task_of
        flat = {"id": "auth.sessions", "title": "Old", "phase": "p1", "paths": ["old/**"], "class": "B",
                "size": "S", "exec": dict(EXEC), "state": "todo", "sections": {"prompt": "Do it."}}
        fs = source.read_tree(_paths.FIXTURES, "specs")
        ops, _ = migrate.plan_ops([flat], states={})
        changes, _, _ = apply_ops(fs, ops, coordinator=True)
        fs.update({k: v for k, v in changes.items() if v is not None})
        plan = SpecTreePlan(fs)
        tid = task_of(plan.tree, "auth.sessions")
        self.assertTrue(tid.startswith("legacy/"), tid)
        self.assertEqual(plan.get("auth.sessions")["tree"]["id"], tid)
        self.assertEqual(plan.get(IMPL)["tree"]["id"], REQ + "/rotate-impl")


if __name__ == "__main__":
    unittest.main()
