"""The rules across records (docs/design/spec-layer.md, sections 4 to 6): references, blockers, alignment
A1-A7, predicate P and waves, readiness, suspect tasks, trace. P8 and P9 on random trees."""

import random
import unittest

import _paths
import specsim
from wavecoord.globs import globs_overlap
from wavecoord.specs import doc, source
from wavecoord.specs.check import check
from wavecoord.specs.graph import Graph
from wavecoord.specs.tree import Tree

ROT = "auth/sessions/token-rotation/_node.md"
IMPL = "auth/sessions/token-rotation/tasks/rotate-impl.md"
TESTS = "auth/sessions/token-rotation/tasks/rotate-tests.md"
STORE = "auth/sessions/session-store/tasks/store-api.md"


def files():
    return source.read_tree(_paths.FIXTURES, "specs")


def edit(fs, rel, fn):
    rec = doc.parse(fs[rel])
    fn(rec)
    fs[rel] = doc.render(rec)
    return fs


def rules(fs, level="error"):
    return {p.rule for p in check(Tree(fs)) if p.level == level}


class FixtureReadTest(unittest.TestCase):
    def test_waves_ready_trace(self):
        g = Graph(Tree(files()))
        w = g.waves()
        self.assertEqual(w["waves"], [["auth/sessions/token-rotation/rotate-impl"],
                                      ["auth/sessions/token-rotation/rotate-tests"]])
        self.assertEqual(w["width"], 1)
        self.assertEqual(g.ready()["tasks"], ["auth/sessions/token-rotation/rotate-impl"])
        tr = g.trace("auth/sessions/token-rotation")
        self.assertEqual(tr["waits_for"], ["auth/sessions/session-store/store-api"])
        self.assertEqual(tr["decisions"], ["auth/decisions/2026-10-19-rotating-tokens"])
        self.assertEqual(g.suspects(), [])
        arts = {a["id"]: a for a in g.artifacts()}
        self.assertEqual(arts["auth.session-store.api"]["producers"], ["auth/sessions/session-store/store-api"])

    def test_ready_rules(self):
        fs = edit(files(), ROT, lambda r: r.update(status="draft"))
        why = Graph(Tree(fs)).ready()["why_not"]
        self.assertIn("is draft", why["auth/sessions/token-rotation/rotate-impl"])
        self.assertEqual(Graph(Tree(fs), require_approved=False).ready()["tasks"],
                         ["auth/sessions/token-rotation/rotate-impl"])
        fs = edit(files(), ROT, lambda r: r["sections"].update(statement=r["sections"]["statement"] + " now"))
        g = Graph(Tree(fs))
        self.assertEqual(g.ready()["tasks"], ["auth/sessions/token-rotation/rotate-impl"])  # suspect: warn
        self.assertIn("suspect", g.ready("refuse")["why_not"]["auth/sessions/token-rotation/rotate-impl"])
        self.assertEqual({s["task"] for s in g.suspects()}, {"auth/sessions/token-rotation/rotate-impl",
                                                             "auth/sessions/token-rotation/rotate-tests"})


class RuleTest(unittest.TestCase):
    """P9 on the fixture tree: one injected misalignment per case, the rule that reports it."""

    def test_rules(self):
        cases = [
            ("A1", STORE, lambda r: r.update(outputs=[o for o in r["outputs"] if o["kind"] != "interface"])),
            ("A2", TESTS, lambda r: r["outputs"].append({"id": "src/auth/rotate.py", "kind": "file"}) or
             r["paths"].append("src/auth/rotate.py")),
            ("A3", IMPL, lambda r: r["inputs"][0].update(id="auth.nothing")),
            ("A4", TESTS, lambda r: r.pop("needs")),
            ("A5", IMPL, lambda r: r["outputs"].append({"id": "lib/other.py", "kind": "file"})),
            ("ref", IMPL, lambda r: r["needs"].append("auth/nowhere")),
            ("needs", IMPL, lambda r: r["needs"].append("auth/sessions")),
            ("cycle", "auth/sessions/session-store/_node.md",
             lambda r: r.update(needs=["auth/sessions/token-rotation"])),
            ("P", TESTS, lambda r: r.update(needs=[], inputs=[], paths=["src/auth/**"])),
        ]
        for rule, rel, fn in cases:
            with self.subTest(rule=rule):
                self.assertIn(rule, rules(edit(files(), rel, fn)))

    def test_warnings(self):
        fs = edit(files(), IMPL, lambda r: r["paths"].append("docs/extra.md"))
        self.assertIn("A6", rules(fs, "warning"))
        fs = edit(files(), ROT, lambda r: r["outputs"].append({"id": "auth.unused", "kind": "data"}))
        fs = edit(fs, IMPL, lambda r: r["outputs"].append({"id": "auth.unused", "kind": "data"}))
        self.assertIn("A7", rules(fs, "warning"))
        fs = edit(files(), IMPL, lambda r: r.update(inputs=[]))
        self.assertIn("needs", rules(fs, "warning"))

    def test_conflicts_with_keeps_tasks_apart(self):
        fs = edit(files(), TESTS, lambda r: r.update(needs=[], inputs=[]))
        g = Graph(Tree(fs))
        self.assertEqual(g.waves()["width"], 2)
        fs = edit(fs, TESTS, lambda r: r.update(conflicts_with=["auth/sessions/token-rotation/rotate-impl"]))
        self.assertEqual(Graph(Tree(fs)).waves()["width"], 1)

    def test_partial_segment_glob_overlaps(self):
        # `src/a*` overlaps `src/auth/rotate.py`; the bucketing must not miss it
        fs = edit(files(), TESTS, lambda r: r.update(needs=[], inputs=[], paths=["s*"], outputs=[]))
        self.assertIn("P", rules(fs))


class RandomTreeTest(unittest.TestCase):
    def test_p8_waves_are_parallel_safe(self):
        for seed in specsim.SEEDS:
            g = Graph(Tree(specsim.build(random.Random(seed))))
            self.assertEqual([str(p) for p in g.problems if p.level == "error"], [], seed)
            w = g.waves()
            self.assertEqual(w["unplaced"], [])
            placed = []
            for wave in w["waves"]:
                for i, a in enumerate(wave):
                    deps = g.reach(a)
                    self.assertTrue(deps <= set(placed), (seed, a))  # every need runs in an earlier wave
                    for b in wave[i + 1:]:  # an independent check of predicate P
                        self.assertNotIn(b, deps)
                        self.assertNotIn(a, g.reach(b))
                        for x in g.recs[a]["paths"]:
                            for y in g.recs[b]["paths"]:
                                self.assertFalse(globs_overlap(x, y), (seed, a, b))
                placed.extend(wave)

    def test_p9_injected_misalignment(self):
        for seed in specsim.SEEDS:
            r = random.Random(seed)
            fs = specsim.build(r)
            req = r.choice(specsim.requirements(fs))
            t0 = "%s/tasks/t0.md" % req
            api = "x." + req.replace("/", ".")
            cases = [
                ("A1", lambda x: x.update(outputs=[o for o in x["outputs"] if o["id"] != api])),
                ("A5", lambda x: x["outputs"].append({"id": "elsewhere/f.py", "kind": "file"})),
                ("ref", lambda x: x.update(needs=["cap9/nope"])),
            ]
            for rule, fn in cases:
                with self.subTest(seed=seed, rule=rule):
                    self.assertIn(rule, rules(edit(dict(fs), t0, fn)))
            # a task that may run alongside req's tasks (no needs either way) also writes req's folder
            g = Graph(Tree(fs))
            mine = set(g.expand(req))
            free = [q for q in specsim.requirements(fs) if q != req and not any(
                q + "/t0" in g.reach(t) or t in g.reach(q + "/t0") for t in mine)]
            if free:
                clash = edit(dict(fs), "%s/tasks/t0.md" % free[0], lambda x: x["paths"].append("src/%s/**" % req))
                self.assertIn("P", rules(clash), seed)


IMPL_ID = "auth/sessions/token-rotation/rotate-impl"
TESTS_ID = "auth/sessions/token-rotation/rotate-tests"
STORE_ID = "auth/sessions/session-store/store-api"


def planned_store():
    return edit(files(), STORE, lambda r: r.update(status="planned"))


class FailClosedTest(unittest.TestCase):
    """A need that is invalid, does not resolve or stands for no task blocks (items 15, 20, 21, 46)."""

    def test_baseline(self):
        self.assertEqual(Graph(Tree(planned_store())).ready()["tasks"], [STORE_ID])

    def test_unparsable_or_invalid_blocker_blocks(self):
        corrupt = {
            "extra section": lambda t: t + "\n## Extra\ntext\n",
            "conflict markers": lambda t: t.replace("## Context", "<<<<<<< HEAD\n## Context"),
            "scalar paths": lambda t: t.replace("paths:\n  - src/auth/store.py\n  - tests/test_store.py",
                                                "paths: src/auth/store.py"),
            "scalar exec": lambda t: t.replace("exec:\n  model: sonnet\n  effort: medium\n  max_turns: 40\n"
                                               "  budget_usd: 2\n  timeout_min: 30", "exec: sonnet"),
        }
        for what, fn in corrupt.items():
            with self.subTest(what=what):
                fs = planned_store()
                new = fn(fs[STORE])
                self.assertNotEqual(new, fs[STORE])
                fs[STORE] = new
                g = Graph(Tree(fs))
                r = g.ready()
                self.assertNotIn(IMPL_ID, r["tasks"])
                self.assertIn(STORE_ID, r["why_not"][IMPL_ID])
                self.assertNotIn(IMPL_ID, [t for w in g.waves()["waves"] for t in w])
                self.assertTrue([p for p in check(Tree(fs)) if p.level == "error" and p.id == STORE_ID])

    def test_malformed_task_does_not_crash_the_plan(self):
        for what, old, new in (("paths", "paths:\n  - src/auth/rotate.py", "paths: src/auth/rotate.py"),
                               ("exec", "exec:\n  model: opus\n  effort: high\n  max_turns: 80\n  budget_usd: 5\n"
                                        "  timeout_min: 60", "exec: opus")):
            with self.subTest(what=what):
                fs = files()
                self.assertIn(old, fs[IMPL])
                fs[IMPL] = fs[IMPL].replace(old, new)
                probs = check(Tree(fs))
                self.assertIn("schema", {p.rule for p in probs if p.id == IMPL_ID and p.level == "error"})
                g = Graph(Tree(fs))
                self.assertNotIn(IMPL_ID, g.ready()["tasks"])
                self.assertIn(IMPL_ID, g.ready()["why_not"][TESTS_ID])
                g.waves()

    def test_unresolved_need_blocks(self):
        fs = edit(files(), TESTS, lambda r: r["needs"].append("auth/sessions/token-rotation/rotate-imp"))
        fs = edit(fs, IMPL, lambda r: r.update(status="done"))
        r = Graph(Tree(fs)).ready()
        self.assertNotIn(TESTS_ID, r["tasks"])
        self.assertIn("rotate-imp", r["why_not"][TESTS_ID])

    def test_need_on_a_node_with_no_tasks_blocks_and_warns(self):
        fs = files()
        del fs[STORE]
        g = Graph(Tree(fs))
        r = g.ready()
        self.assertNotIn(IMPL_ID, r["tasks"])
        self.assertIn("no tasks", r["why_not"][IMPL_ID])
        warns = [p for p in check(Tree(fs)) if p.level == "warning" and p.rule == "needs"]
        self.assertTrue([p for p in warns if "no task" in p.msg], [str(p) for p in warns])


class DroppedTest(unittest.TestCase):
    """Dropped tasks neither block nor produce (item 45)."""

    def test_dropped_task_in_a_needed_subtree_does_not_block(self):
        fs = files()
        cache = STORE.replace("store-api", "store-cache")
        fs[cache] = fs[STORE].replace("status: done", "status: dropped").replace(
            "auth.session-store.api", "auth.session-store.cache").replace("src/auth/store.py", "src/auth/cache.py"
            ).replace("tests/test_store.py", "tests/test_cache.py")
        g = Graph(Tree(fs))
        self.assertEqual(g.ready()["tasks"], [IMPL_ID])
        self.assertEqual(g.waves()["unplaced"], [])

    def test_dropped_producer_is_no_producer(self):
        fs = edit(files(), IMPL, lambda r: r.update(status="dropped"))
        errs = {p.rule for p in check(Tree(fs)) if p.level == "error"}
        self.assertIn("A3", errs)
        self.assertIn("A1", errs)
        # a replacement with the same outputs is not a second producer
        fs[IMPL.replace("rotate-impl", "rotate-impl2")] = files()[IMPL]
        fs = edit(fs, TESTS, lambda r: r.update(needs=["auth/sessions/token-rotation/rotate-impl2"]) or
                  r["inputs"][0].update({"from": "auth/sessions/token-rotation/rotate-impl2"}))
        errs = {p.rule for p in check(Tree(fs)) if p.level == "error"}
        self.assertFalse(errs & {"A1", "A2", "A3"}, errs)


class SpecFirstTest(unittest.TestCase):
    """A requirement written before its tasks (the /wavecoord:spec flow) only warns for A1 and A3 (item 6)."""

    def test_no_tasks_yet(self):
        fs = files()
        for rel in (IMPL, TESTS, STORE):
            del fs[rel]
        probs = check(Tree(fs))
        self.assertEqual([str(p) for p in probs if p.level == "error"], [])
        self.assertTrue({"A1", "A3"} <= {p.rule for p in probs if p.level == "warning"})


class ReadyRulesTest(unittest.TestCase):
    def test_refuse_treats_missing_basis_as_suspect(self):
        fs = edit(files(), IMPL, lambda r: r.pop("basis"))
        g = Graph(Tree(fs))
        self.assertEqual(g.ready("warn")["tasks"], [IMPL_ID])
        self.assertIn("basis", g.ready("refuse")["why_not"][IMPL_ID])

    def test_supersedes(self):
        store_req = "auth/sessions/session-store/_node.md"
        cases = {
            "self": (ROT, lambda r: r.update(supersedes=["auth/sessions/token-rotation"])),
            "mutual": (ROT, lambda r: r.update(supersedes=["auth/sessions/session-store"])),
        }
        fs = edit(files(), store_req, lambda r: r.update(supersedes=["auth/sessions/token-rotation"]))
        self.assertIn("supersedes", rules(fs, "warning"))  # superseded but still approved
        g = Graph(Tree(fs))
        self.assertIn("superseded", g.ready()["why_not"][IMPL_ID])
        self.assertEqual(g.trace("auth/sessions/token-rotation")["superseded_by"], ["auth/sessions/session-store"])
        for what, (rel, fn) in cases.items():
            with self.subTest(what=what):
                self.assertIn("supersedes", rules(edit(dict(fs) if what == "mutual" else files(), rel, fn)))

    def test_waves_hold_tasks_that_cannot_run(self):
        fs = edit(files(), IMPL, lambda r: r.update(status="on_hold"))
        w = Graph(Tree(fs)).waves()
        self.assertEqual(w["waves"], [])
        self.assertIn(IMPL_ID, w["held"])
        self.assertEqual(w["unplaced"], [TESTS_ID])
        fs = edit(files(), ROT, lambda r: r.update(status="draft"))
        self.assertIn(IMPL_ID, Graph(Tree(fs)).waves()["held"])

    def test_waves_root_resolves_and_must_exist(self):
        g = Graph(Tree(files()))
        self.assertEqual(g.waves("auth/sessions/token-rotation/")["waves"][0], [IMPL_ID])
        with self.assertRaises(KeyError):
            g.waves("auth/nope")

    def test_a4_accepts_a_transitive_producer(self):
        fs = files()
        fs[TESTS.replace("rotate-tests", "docs")] = files()[TESTS]
        fs = edit(fs, TESTS.replace("rotate-tests", "docs"), lambda r: r.update(
            needs=[TESTS_ID], paths=["docs/rotate.md"], outputs=[{"id": "docs/rotate.md", "kind": "file"}]))
        self.assertNotIn("A4", rules(fs))

    def test_long_needs_chain(self):
        fs = files()
        req = "auth/sessions/token-rotation"
        n = 1500
        for i in range(n):
            rel = "%s/tasks/c%d.md" % (req, i)
            fs[rel] = files()[TESTS]
            fs = edit(fs, rel, lambda r, i=i: r.update(needs=["%s/c%d" % (req, i + 1)] if i + 1 < n else [],
                                                        inputs=[], outputs=[], paths=["chain/c%d.py" % i]))
        g = Graph(Tree(fs))
        self.assertEqual(len(g.waves()["waves"]), n)

    def test_non_canonical_paths(self):
        fs = edit(files(), TESTS, lambda r: r.update(needs=[], inputs=[], paths=["./src/auth/rotate.py"], outputs=[]))
        self.assertIn("paths", rules(fs))
        g = Graph(Tree(fs))
        self.assertFalse(g.parallel_ok(IMPL_ID, TESTS_ID))


if __name__ == "__main__":
    unittest.main()
