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
import fenceworld
from test_fence_defects import CI_FS, pool

FIXED_SEEDS = list(range(1, 51))
# Soak seeds that failed, with the operation count of the soak run (WAVECOORD_FENCE_OPS=300), 0.1.3:
# 72857574 lost an override journal entry (D23); 289598702 merged the agent's config trick.
REGRESSION_SEEDS = [(72857574, 300), (289598702, 229)]
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
        if not os.environ.get("WAVECOORD_FENCE_SEED"):
            jobs += [(s, None, n, False) for s, n in REGRESSION_SEEDS]
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


# --------------------------------------------------------------------------- the oracle's fold cache (0.1.3)
#
# CI failed seed 44 on macos-latest / 3.9 with six safety and integration failures and no fault. The fence was
# right: the oracle's fold cache was keyed by id(events), a pool process runs several seeds, and seed 44's
# event list got the id of an earlier seed's freed list. The oracle then folded seed 44's log on top of the
# other seed's leases. These tests keep that from coming back.

def _grant(seq, lid, task, holder, glob, mode="exclusive"):
    return {"seq": seq, "kind": "lease.grant", "payload": {
        "lease": {"id": lid, "task": task, "holder": holder, "epoch": 1},
        "reservations": [{"id": "%s-r" % lid, "lease_id": lid, "glob": glob, "mode": mode}]}}


def _view(st):
    return st.seq, st.leases, st.res


def _ofold_by_id(events, seq, _cache={}):
    """The 0.1.2 cache (keyed by id(events)): the seeded defect O1 for the check below."""
    key = fenceworld.id(events)
    st = _cache.get(key)
    st = fenceworld.OState() if st is None or st.seq > seq else st.copy()
    for ev in events[st.seq:seq]:
        st.apply(ev, events)
    if seq >= (_cache.get(key).seq if _cache.get(key) else 0):
        _cache[key] = st.copy()
    return st


def cache_violation(ofold):
    """None when a cached fold always equals a fresh fold, else what differs."""
    def fresh(events, seq):
        st = fenceworld.OState()
        for ev in events[:seq]:
            st.apply(ev, events)
        return st

    a = [_grant(1, "L1", "t0-1", "w0", "a/**"), _grant(2, "L2", "t1-1", "w1", "a/b/**")]
    # another seed's log, a new list (with the same id under the defect), at least as long as the cached fold
    b = [_grant(1, "L9", "t0-1", "w0", "a/b/z.py"), _grant(2, "L10", "t2-1", "w2", "docs/**", "shared"),
         _grant(3, "L11", "t3-1", "w3", "src/**")]
    for events, seq in ((a, 1), (a, 2), (a, 1), (b, 1), (b, 3), (b, 2), (a, 2), (b, 3)):
        if _view(ofold(events, seq)) != _view(fresh(events, seq)):
            return "fold of %s to seq %d differs from a fresh fold" % ("ab"[events is b], seq)
    st = ofold(b, 3)
    if not st.ok("w0", "t0-1", 1, "a/b/z.py") or st.foreign("a/b/z.py", exclude="L9"):
        return "w0's write of a/b/z.py in the second log is not ok (another log's lease leaked in)"
    return None


class OracleCacheTest(unittest.TestCase):
    def test_cached_fold_equals_fresh_fold(self):
        self.assertIsNone(cache_violation(fenceworld.ofold))

    def test_check_catches_the_id_keyed_cache(self):
        """Seeded defect O1: the 0.1.2 cache with a reused id (forced) must fail the check above."""
        fenceworld.id = lambda _x: 0
        try:
            self.assertIsNotNone(cache_violation(_ofold_by_id))
        finally:
            del fenceworld.id

    def test_seed_44_after_seed_5_in_one_process(self):
        """The CI sequence: seed 44 after another seed in the same process. Under the 0.1.2 cache with a
        reused id (forced here, as the allocator did on CI) this gave the exact CI failures (op 25: commit
        a/b/z.py by w0)."""
        import fencesim
        fenceworld.id = lambda _x: 0
        try:
            for seed in (5, 44):
                failures, _st = fencesim.run(seed)
                self.assertEqual(failures, [], "seed %d" % seed)
        finally:
            del fenceworld.id


if __name__ == "__main__":
    unittest.main()
