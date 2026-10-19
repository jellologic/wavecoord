"""Spec tree records: parse, canonical render, round trip (P0), errors (docs/design/spec-layer.md)."""

import os
import random
import unittest

import _paths
from wavecoord.plan.spec import SpecError
from wavecoord.specs import doc
from wavecoord.specs.model import KINDS, STEP_WORDS, normalize

FIX = os.path.join(_paths.FIXTURES, "specs")
SEEDS = [int(s) for s in os.environ.get("WAVECOORD_SPEC_SEED", "").split(",") if s] or list(range(40))

# values that a naive writer gets wrong: numbers, booleans, colons, comments, quotes, dashes, unicode
TRICKY = ["00000000", "12", "1.5", "true", "null", "a: b", "x #y", "-dash", "'q'", '"dq"', "é ü 日本", "[list]",
          "{map}", "*star", "&amp", "ends:", "  pad", "back\\slash", "plain text", "f(x) -> y; z", "#hash"]


def fixtures():
    out = {}
    for d, _, files in os.walk(FIX):
        for f in files:
            p = os.path.join(d, f)
            with open(p, encoding="utf-8") as fh:
                out[os.path.relpath(p, FIX)] = fh.read()
    return out


def _word(r):
    return r.choice(["alpha", "beta", "gamma", "rotate", "token", "store", "x1", "auth", "Session"]) + str(r.randint(0, 99))


def _text(r):
    return r.choice(TRICKY + [_word(r) + " " + _word(r)])


def _name(r):
    return "%s-%d" % (r.choice(["a", "b", "core", "sync"]), r.randint(0, 999))


def _artifacts(r, inputs):
    out = []
    for i in range(r.randint(0, 3)):
        a = {"id": "x.%s.%d" % (_name(r), i), "kind": r.choice(["interface", "file", "api", "data"])}
        if inputs:
            if r.random() < 0.5:
                a["from"] = _name(r)
            else:
                a["external"] = True
                a["contract"] = _text(r)
        elif r.random() < 0.7:
            a["contract"] = _text(r)
        out.append(a)
    return out


def _prose(r):
    lines = [_text(r).strip() or "word" for _ in range(r.randint(1, 4))]
    if r.random() < 0.3:
        lines.insert(1, "")
    return "\n".join(l for l in lines).strip()


def random_record(r, kind):
    k = KINDS[kind]
    rec = {"status": r.choice(k.statuses), "kind": kind, "schema": 1, "title": _text(r).strip() or "title"}
    for key in k.keys:
        if key in ("title",):
            continue
        if key in ("needs", "conflicts_with", "aliases", "supersedes", "affects"):
            rec[key] = sorted({_name(r) + "/" + _name(r) for _ in range(r.randint(0, 3))})
        elif key in ("tags", "owners"):
            rec[key] = sorted({_name(r) for _ in range(r.randint(0, 3))})
        elif key in ("paths", "shared_paths"):
            rec[key] = sorted({"src/%s/**" % _name(r) for _ in range(r.randint(1, 3))})
        elif key in ("inputs", "outputs"):
            rec[key] = _artifacts(r, key == "inputs")
        elif key == "basis":
            rec[key] = r.choice(["00000000", "12345678", "a1b2c3d4", "deadbeef"])
        elif key == "class":
            rec[key] = r.choice("ABC")
        elif key == "size":
            rec[key] = r.choice(["XS", "S", "M", "L"])
        elif key == "exec":
            rec[key] = {"model": r.choice(["opus", "sonnet"]), "effort": "high", "max_turns": r.randint(1, 99),
                        "budget_usd": r.choice([1, 2.5]), "timeout_min": 30}
            if r.random() < 0.5:
                rec[key]["provider_fallback"] = ["a", "b"]
    secs = {}
    for s in k.sections:
        if not s.required and r.random() < 0.4:
            continue
        if s.type == "prose":
            secs[s.key] = _prose(r)
        elif s.type == "list":
            secs[s.key] = [(_text(r).strip() or "item") for _ in range(r.randint(1, 3))]
        elif s.type == "commands":
            secs[s.key] = ["python -m unittest tests.test_%s" % _name(r).replace("-", "_") for _ in range(r.randint(1, 3))]
        elif s.type == "scenarios":
            secs[s.key] = [{"name": _word(r) + " case", "steps": [[r.choice(STEP_WORDS), _text(r).strip() or "step"]
                                                                  for _ in range(r.randint(1, 4))]}
                           for _ in range(r.randint(1, 2))]
        elif s.type == "risks":
            secs[s.key] = [{"risk": _text(r).strip() or "risk", "mitigation": _word(r), "kind": "llm"}
                           for _ in range(r.randint(1, 2))]
    rec["sections"] = secs
    return rec


class FixtureTest(unittest.TestCase):
    def test_fixtures_are_canonical(self):
        files = fixtures()
        self.assertEqual(len(files), 8)
        for rel, text in files.items():
            with self.subTest(rel=rel):
                self.assertEqual(doc.fmt(text), text)

    def test_parse_shapes(self):
        rec = doc.parse(fixtures()["auth/sessions/token-rotation/_node.md"])
        self.assertEqual(rec["kind"], "requirement")
        self.assertEqual(rec["inputs"], [{"id": "auth.session-store.api", "kind": "interface",
                                          "from": "auth/sessions/session-store"}])
        self.assertEqual(rec["sections"]["scenarios"][0]["steps"][0], ["Given", "a session whose token expires in 30 s"])
        self.assertEqual(rec["sections"]["verify"], ["python -m unittest tests.test_rotate"])
        task = doc.parse(fixtures()["auth/sessions/token-rotation/tasks/rotate-impl.md"])
        self.assertEqual(task["exec"]["max_turns"], 80)
        self.assertEqual(task["sections"]["risks"][0]["kind"], "llm")


class RoundTripTest(unittest.TestCase):
    """P0: parse(render(r)) == normalize(r); render is idempotent."""

    def test_random_records(self):
        for seed in SEEDS:
            r = random.Random(seed)
            for kind in KINDS:
                rec = random_record(r, kind)
                with self.subTest(seed=seed, kind=kind):
                    text = doc.render(rec)
                    back = doc.parse(text)
                    self.assertEqual(back, normalize(rec), text)
                    self.assertEqual(doc.render(back), text)

    def test_scalars_survive(self):
        for v in TRICKY:
            with self.subTest(v=v):
                rec = {"status": "draft", "kind": "feature", "schema": 1, "title": v.strip() or "t",
                       "tags": [], "sections": {"intent": "x"}}
                self.assertEqual(doc.parse(doc.render(rec))["title"], v.strip())

    def test_non_canonical_input_formats_to_the_same_record(self):
        canon = fixtures()["auth/sessions/token-rotation/tasks/rotate-impl.md"]
        messy = (canon.replace("status: planned\nkind: task\nschema: 1\n", "kind: task\nschema: 1\nstatus: planned\n")
                 .replace("paths:\n  - src/auth/rotate.py\n", "paths: [src/auth/rotate.py]\n")
                 .replace("## Context", "\n\n## context")
                 .replace("- `python -m unittest tests.test_rotate`", "-   `python -m unittest tests.test_rotate`  "))
        self.assertNotEqual(messy, canon)
        self.assertEqual(doc.parse(messy), doc.parse(canon))
        self.assertEqual(doc.fmt(messy), canon)

    def test_render_order_and_block_lists(self):
        text = doc.fmt(fixtures()["auth/sessions/token-rotation/_node.md"])
        lines = text.split("\n")
        self.assertEqual(lines[1:4], ["status: approved", "kind: requirement", "schema: 1"])
        self.assertNotIn("[", "\n".join(lines[:lines.index("---", 1)]))
        rec = doc.parse(text)
        rec["tags"] = ["zeta", "alpha"]
        self.assertIn("tags:\n  - alpha\n  - zeta\n", doc.render(rec))


class ErrorTest(unittest.TestCase):
    def test_rejects(self):
        base = fixtures()["auth/sessions/token-rotation/_node.md"]
        cases = {
            "unknown section": base + "\n## Extra\ntext\n",
            "text outside": base.replace("## Statement", "stray\n\n## Statement"),
            "duplicate section": base + "\n## Rationale\nagain\n",
            "bad step": base.replace("- Given a session", "- Assuming a session"),
            "step before scenario": base.replace("### Near-expiry token is refreshed\n", ""),
            "unknown kind": base.replace("kind: requirement", "kind: epic"),
            "no frontmatter": base.split("---\n", 2)[2],
            "nested in artifact": base.replace("    from: auth/sessions/session-store",
                                               "    from:\n      x: 1"),
        }
        for what, text in cases.items():
            with self.subTest(what=what):
                with self.assertRaises(SpecError):
                    doc.parse(text)

    def test_headings_inside_code_fences_are_text(self):
        rec = doc.parse(fixtures()["auth/_node.md"])
        rec["sections"]["design"] = "Example:\n```\n## not a section\n```"
        self.assertEqual(doc.parse(doc.render(rec))["sections"]["design"], rec["sections"]["design"])


class NoDataLossTest(unittest.TestCase):
    """fmt and render refuse a record they cannot write back whole (items 0, 10, 11)."""

    def test_kind_mismatch_and_unknown_kind(self):
        base = fixtures()["auth/sessions/token-rotation/_node.md"]
        for what, text in (("feature", base.replace("kind: requirement", "kind: feature")),
                           ("typo", base.replace("kind: requirement", "kind: requirment"))):
            with self.subTest(what=what):
                with self.assertRaises(SpecError) as cm:
                    doc.fmt(text, "requirement")
                self.assertIn("kind", str(cm.exception))

    def test_list_keys_hold_lists(self):
        base = fixtures()["auth/sessions/token-rotation/_node.md"]
        cases = {
            "scalar needs": base.replace("needs:\n  - auth/sessions/session-store", "needs: auth/sessions/session-store"),
            "mixed tags": base.replace("tags:\n  - security", "tags: [1, security]"),
            "scalar inputs": base.replace("inputs:\n  - id: auth.session-store.api\n    kind: interface\n"
                                          "    from: auth/sessions/session-store", "inputs:\n  - foo"),
        }
        for what, text in cases.items():
            with self.subTest(what=what):
                self.assertNotEqual(text, base)
                with self.assertRaises(SpecError):
                    doc.fmt(text, "requirement")
        task = fixtures()["auth/sessions/token-rotation/tasks/rotate-impl.md"]
        bad = task.replace("exec:\n  model: opus\n  effort: high\n  max_turns: 80\n  budget_usd: 5\n  timeout_min: 60",
                           "exec: opus")
        self.assertNotEqual(bad, task)
        with self.assertRaises(SpecError):
            doc.fmt(bad, "task")

    def test_unknown_keys_are_refused(self):
        base = fixtures()["auth/sessions/token-rotation/_node.md"]
        text = base.replace("title: Token rotation on expiry\n", "title: Token rotation on expiry\nowner: alice\n")
        with self.assertRaises(SpecError) as cm:
            doc.fmt(text, "requirement")
        self.assertIn("owner", str(cm.exception))
        rec = doc.parse(base)
        rec["need"] = ["a/b"]
        with self.assertRaises(SpecError):
            doc.render(rec)


class FenceTest(unittest.TestCase):
    """Code fences follow Markdown: ``` or ~~~, closed by the same character at least as long (item 39)."""

    def test_inline_triple_backticks_do_not_open_a_fence(self):
        base = fixtures()["auth/sessions/token-rotation/_node.md"]
        text = base.replace("When an access token", "```x``` When an access token")
        rec = doc.parse(text)
        self.assertTrue(rec["sections"]["statement"].startswith("```x```"))
        self.assertIn("scenarios", rec["sections"])
        self.assertIn("verify", rec["sections"])

    def test_tilde_and_long_fences(self):
        rec = doc.parse(fixtures()["auth/_node.md"])
        for design in ("Example:\n~~~md\n## Open questions\n- x\n~~~",
                       "Example:\n````\n```\n## Open questions\n````",
                       "Example:\n```\n~~~\n## Open questions\n```"):
            with self.subTest(design=design):
                rec["sections"]["design"] = design
                back = doc.parse(doc.render(rec))
                self.assertEqual(back["sections"]["design"], design)
                self.assertNotIn("open_questions", back["sections"])


class ScalarRoundTripTest(unittest.TestCase):
    """render writes scalars that read back as the same value (item 40)."""

    def test_control_characters_flow_items_and_floats(self):
        rec = doc.parse(fixtures()["auth/sessions/token-rotation/tasks/rotate-impl.md"])
        rec["title"] = "a\tb"
        rec["exec"] = dict(rec["exec"], host_needs=["gpu, large", "x]"], budget_usd=0.00001)
        text = doc.render(rec)
        back = doc.parse(text)
        self.assertEqual(back["title"], "a\tb")
        self.assertEqual(back["exec"]["host_needs"], ["gpu, large", "x]"])
        self.assertEqual(back["exec"]["budget_usd"], 0.00001)
        self.assertEqual(doc.render(back), text)


if __name__ == "__main__":
    unittest.main()
