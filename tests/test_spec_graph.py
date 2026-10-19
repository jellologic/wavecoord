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


if __name__ == "__main__":
    unittest.main()
