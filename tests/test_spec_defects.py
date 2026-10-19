"""Every seeded defect of the spec tree (specdefects.py) is caught by at least one of its named tests."""

import io
import unittest

import _paths  # noqa: F401
import specdefects


class SeededDefectTest(unittest.TestCase):
    def test_each_defect_is_caught(self):
        loader = unittest.defaultTestLoader
        for did, (what, make, tests) in sorted(specdefects.DEFECTS.items()):
            with self.subTest(defect=did, what=what):
                with make():
                    suite = loader.loadTestsFromNames(tests)
                    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
                self.assertTrue(result.failures or result.errors, "%s (%s) was not caught by %s" % (did, what, tests))

    def test_without_defects_the_named_tests_pass(self):
        names = sorted({t for _, _, tests in specdefects.DEFECTS.values() for t in tests})
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(
            unittest.defaultTestLoader.loadTestsFromNames(names))
        self.assertTrue(result.wasSuccessful(), [str(f[0]) for f in result.failures + result.errors])


if __name__ == "__main__":
    unittest.main()
