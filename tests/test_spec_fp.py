"""P7: the requirement fingerprint ignores what a task does not build against, and sees what it does."""

import copy
import os
import unittest

import _paths
from wavecoord.specs import doc
from wavecoord.specs.fingerprint import fingerprint

REQ = os.path.join(_paths.FIXTURES, "specs", "auth", "sessions", "token-rotation", "_node.md")


def base():
    with open(REQ, encoding="utf-8") as f:
        return doc.parse(f.read())


class FingerprintTest(unittest.TestCase):
    def test_format(self):
        fp = fingerprint(base())
        self.assertRegex(fp, r"^[0-9a-f]{8}$")
        self.assertEqual(fp, fingerprint(base()))

    def test_unchanged(self):
        fp = fingerprint(base())

        def same(name, change):
            r = base()
            change(r)
            with self.subTest(change=name):
                self.assertEqual(fingerprint(r), fp)

        same("title", lambda r: r.update(title="Another title"))
        same("status", lambda r: r.update(status="draft"))
        same("tags", lambda r: r.update(tags=["x"]))
        same("aliases", lambda r: r.update(aliases=["auth/old"]))
        same("rationale", lambda r: r["sections"].update(rationale="Other reasons."))
        same("notes", lambda r: r["sections"].update(notes="A note."))
        same("reflow", lambda r: r["sections"].update(statement="  " + r["sections"]["statement"].replace(" ", "   ")))
        same("backquotes", lambda r: r["sections"].update(verify=["`%s`" % c for c in r["sections"]["verify"]]))
        same("input from (a move rewrites it)", lambda r: r["inputs"][0].update({"from": "auth/sessions/storage"}))
        same("keyword case", lambda r: [s.__setitem__(0, s[0].lower()) for sc in r["sections"]["scenarios"]
                                        for s in sc["steps"]])

    def test_reorder_is_unchanged(self):
        r1, r2 = base(), base()
        r1["sections"]["verify"] = ["a", "b"]
        r2["sections"]["verify"] = ["b", "a"]
        sc = r1["sections"]["scenarios"][0]
        r1["sections"]["scenarios"] = [sc, {"name": "other", "steps": [["Given", "x"]]}]
        r2["sections"]["scenarios"] = [{"name": "other", "steps": [["Given", "x"]]}, copy.deepcopy(sc)]
        self.assertEqual(fingerprint(r1), fingerprint(r2))

    def test_changed(self):
        fp = fingerprint(base())

        def differs(name, change):
            r = base()
            change(r)
            with self.subTest(change=name):
                self.assertNotEqual(fingerprint(r), fp)

        differs("statement token", lambda r: r["sections"].update(
            statement=r["sections"]["statement"].replace("60 s", "120 s")))
        differs("step text", lambda r: r["sections"]["scenarios"][0]["steps"][0].__setitem__(1, "a session"))
        differs("step keyword", lambda r: r["sections"]["scenarios"][0]["steps"][1].__setitem__(0, "Given"))
        differs("verify", lambda r: r["sections"].update(verify=["python -m unittest tests.other"]))
        differs("output contract", lambda r: r["outputs"][0].update(contract="rotate() -> None"))
        differs("input", lambda r: r["inputs"].append({"id": "x.y", "kind": "data", "external": True,
                                                       "contract": "c"}))


if __name__ == "__main__":
    unittest.main()
