"""The randomized fence invariant test (tests/fence/README.md, section 1).

CI runs the fixed seeds and one new random seed; the seed is printed on a
failure. WAVECOORD_FENCE_SEED=<n> runs one seed only; WAVECOORD_FENCE_RUNS=<n>
adds n more random seeds; WAVECOORD_FENCE_OPS=<n> sets the maximum number of
operations per seed (default 80; the soak setting is 300). A failing seed
goes into FIXED_SEEDS with its fix.
"""

import os
import random
import sys
import unittest

import _paths  # noqa: F401
import fencedefects
from test_fence_defects import CI_FS, pool

FIXED_SEEDS = list(range(1, 51))
CI_SEEDS = 5  # on a case-sensitive file system, this many fixed seeds also run with a case-insensitive probe


def seeds():
    one = os.environ.get("WAVECOORD_FENCE_SEED")
    if one:
        return [int(one)], []
    extra = [random.SystemRandom().randrange(10 ** 6, 10 ** 9) for _ in range(1 + int(os.environ.get(
        "WAVECOORD_FENCE_RUNS", "0")))]
    return FIXED_SEEDS + extra, extra


class InvariantTest(unittest.TestCase):
    def test_randomized_invariant(self):
        all_seeds, fresh = seeds()
        sys.stderr.write("\nfence invariant: %d seeds (new random: %s)\n" % (len(all_seeds), fresh))
        jobs = [(s, None, None, False) for s in all_seeds]
        if not CI_FS and not os.environ.get("WAVECOORD_FENCE_SEED"):
            jobs += [(s, None, None, True) for s in FIXED_SEEDS[:CI_SEEDS]]
        with pool() as ex:
            results = list(ex.map(fencedefects.run_seed, jobs))
        failures, totals = [], {}
        for seed, _d, f, st in results:
            failures += f
            for k, v in st.items():
                totals[k] = totals.get(k, 0) + v
        sys.stderr.write("fence invariant: %s\n" % totals)
        self.assertEqual(failures, [], "rerun one seed with WAVECOORD_FENCE_SEED=<seed>")
        self.assertGreater(totals.get("edit_allow", 0), 0, "the simulation made no allowed edit")
        self.assertGreater(totals.get("edit_deny", 0), 0, "the simulation made no denied edit")
        self.assertGreater(totals.get("merged", 0), 0, "the simulation merged nothing")


if __name__ == "__main__":
    unittest.main()
