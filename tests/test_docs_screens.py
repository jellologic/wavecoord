"""The SVG renderer of scripts/docs-screens.py (the demo itself is not run here)."""

import importlib.util
import os
import unittest
import xml.etree.ElementTree as ET

import _paths

SVG = "{http://www.w3.org/2000/svg}"


def load():
    spec = importlib.util.spec_from_file_location("docs_screens",
                                                  os.path.join(_paths.ROOT, "scripts", "docs-screens.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.mod = load()

    def rows(self, svg):
        root = ET.fromstring(svg)
        g = [e for e in root.iter(SVG + "g")][0]
        return root, ["".join(t.itertext()) for t in g.iter(SVG + "text")]

    def test_escape_wrap_and_xml(self):
        out = "a <b> & \"c\"\n" + "x" * 25 + " " + "y" * 20 + "\n\x1b[1;32mok\x1b[0m done\x1b[K"
        svg = self.mod.render_svg("wavecoord spec <tree>", [("wavecoord spec tree", out)], cols=30)
        root, rows = self.rows(svg)  # valid XML
        self.assertEqual(root.tag, SVG + "svg")
        self.assertEqual(root.find(SVG + "title").text, "wavecoord spec <tree>")
        self.assertEqual(rows, ["$ wavecoord spec tree", 'a <b> & "c"', "x" * 25 + " ", "y" * 20, "ok done"])
        self.assertIn("&lt;b&gt; &amp; &quot;c&quot;", svg)
        self.assertNotIn("\x1b", svg)
        self.assertIn('fill="%s"' % self.mod.PALETTE[32], svg)
        self.assertIn('font-weight="bold"', svg)
        self.assertTrue(all(len(r) <= 30 for r in rows))
        self.assertEqual(svg.count("<circle"), 3)

    def test_hard_wrap_and_clip(self):
        svg = self.mod.render_svg("t", [("cmd", "z" * 70 + "\n" + "\n".join("l%d" % i for i in range(20)))],
                                  cols=30, max_rows=8)
        _, rows = self.rows(svg)
        self.assertEqual(rows[1:4], ["z" * 30, "z" * 30, "z" * 10])
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[-1], "... (17 more lines)")

    def test_blocks_are_separated(self):
        svg = self.mod.render_svg("t", [("one", "1"), ("two", "")])
        _, rows = self.rows(svg)
        self.assertEqual(rows, ["$ one", "1", "$ two"])


if __name__ == "__main__":
    unittest.main()
