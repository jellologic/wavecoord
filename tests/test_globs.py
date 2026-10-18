import unittest

import _paths  # noqa: F401
from wavecoord.globs import globs_overlap, literal_prefix, valid_glob


class GlobsTest(unittest.TestCase):
    CASES = [
        # (a, b, overlap)
        ("src/auth/**", "src/auth/x.py", True),
        ("src/auth/**", "src/api/**", False),
        ("src/**", "src/auth/**", True),
        ("docs/api.md", "docs/api.md", True),
        ("docs/api.md", "docs/api.mdx", False),
        ("docs", "docs/api.md", True),
        ("docs/", "docs/api.md", True),
        ("docs/api.md", "docs/**", True),
        ("doc", "docs/**", False),
        ("docs/*.md", "docs/a.md", True),
        ("docs/*.md", "src/a.md", False),
        ("*.md", "src/a.py", True),  # conservative: empty literal prefix overlaps everything
        ("src/a?.py", "src/ab.py", True),
        ("src/[ab].py", "lib/a.py", False),
        ("tests/test_a.py", "tests/test_b.py", False),
        ("src/auth", "src/authz/x.py", False),
        ("src/auth*", "src/authz/x.py", True),
    ]

    def test_cases(self):
        for a, b, want in self.CASES:
            self.assertEqual(globs_overlap(a, b), want, (a, b))
            self.assertEqual(globs_overlap(b, a), want, (b, a))

    def test_literal_prefix(self):
        self.assertEqual(literal_prefix("src/**"), ("src/", True))
        self.assertEqual(literal_prefix("a/b?c[d]"), ("a/b", True))
        self.assertEqual(literal_prefix("a/b"), ("a/b", False))

    def test_valid_glob(self):
        for g in ("src/**", "a.md", "docs/*.md"):
            self.assertTrue(valid_glob(g), g)
        for g in ("", "/abs", "a b", "../x", "a/../b", "{a,b}", "a/..", 3):
            self.assertFalse(valid_glob(g), g)


if __name__ == "__main__":
    unittest.main()
