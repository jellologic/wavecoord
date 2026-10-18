"""Seeded defects (tests/fence/README.md, section 2).

For each of the 22 defects, the test applies its patch (tests/fencedefects.py)
and runs (1) the defect's named table check and (2) the randomized
simulation on the first fixed seeds. At least one must fail, else the
defect is not caught and the suite fails. Without a patch, every table
check passes. The result table is printed.

WAVECOORD_FENCE_DEFECT_SEEDS=<n> runs the simulation on n seeds per defect
(default 2).
"""

import io
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
import multiprocessing

import _paths  # noqa: F401
import fencedefects
from fencesim import Sim
from fenceworld import git
from wavecoord import fence, gatecheck, guard
from wavecoord.core import ApiError, lead_ctx


def fs_case_insensitive():
    d = tempfile.mkdtemp()
    try:
        open(os.path.join(d, "a"), "w").close()
        return os.path.exists(os.path.join(d, "A"))
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


CI_FS = fs_case_insensitive()


class Table(object):
    """Named checks: each returns None when the fence behaves, else a description of the failure."""

    def __init__(self, ci=None):
        self.ci = ci
        self.sim = Sim(0, ci=ci, n_workers=2)
        self.w = self.sim.w
        self.a, self.b = self.w.workers

    def close(self):
        self.w.close()

    def edit(self, wk, target, env_extra=None):
        env = self.w.worker_env(wk)
        env.update(env_extra or {})
        inp = {"session_id": "s", "cwd": wk.wt, "hook_event_name": "PreToolUse", "tool_name": "Edit",
               "tool_input": {"file_path": target}}
        return self.sim.run_hook("PreToolUse", inp, env)[0]

    def expect(self, got, want, what):
        return None if got == want else "%s: got %s, want %s" % (what, got, want)

    # one check per defect
    def D01(self):
        self.w.assign(self.a, [("a/b", "exclusive")])
        self.w.assign(self.b, [("a/**", "shared")])
        return self.expect(self.edit(self.b, "a/bc/w.py"), "allow", "a/bc/w.py with a foreign exclusive a/b") or \
            self.expect(self.edit(self.a, "a/bc/w.py"), "deny", "a/bc/w.py outside the lease a/b")

    def D02(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        self.w.proxy.fault = "trickle"
        try:
            return self.expect(self.edit(self.a, "a/x.py"), "deny", "an answer slower than the budget")
        finally:
            self.w.proxy.fault = None

    def D03(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        self.w.coord.claim(lead_ctx(), {"task": self.a.task, "attempt": 2, "holder": self.a.agent, "host": "local",
                                        "steal": True, "force": True,
                                        "reservations": [{"glob": "a/**", "mode": "exclusive"}]})
        return self.expect(self.edit(self.a, "a/x.py"), "deny", "an edit with the old epoch after a new grant")

    def D04(self):
        self.w.assign(self.a, [("docs/**", "shared")])
        self.w.assign(self.b, [("docs/**", "shared")])
        return self.expect(self.edit(self.b, "docs/api.md"), "allow", "two shared holders")

    def D05(self):
        self.w.assign(self.a, [("docs/**", "exclusive")])
        self.w.assign(self.b, [("docs/**", "shared")])
        return self.expect(self.edit(self.b, "docs/api.md"), "deny", "inside a foreign exclusive")

    def D06(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        self.w.proxy.fault = "down"
        try:
            return self.expect(self.edit(self.a, "a/x.py", {"WAVECOORD_FENCE_OFFLINE": "1"}), "deny",
                               "coordinator down, WAVECOORD_FENCE_OFFLINE=1, no override file")
        finally:
            self.w.proxy.fault = None

    def D07(self):
        self.w.assign(self.a, [("docs/**", "shared")])
        self.w.assign(self.b, [("a/**", "exclusive")])
        return self.expect(self.edit(self.a, "docs/link/x.py"), "deny", "docs/link/x.py is a/x.py (symlink)") or \
            self.expect(self.edit(self.a, os.path.join(self.a.wt, "docs", "..", "a", "y.py")), "deny", "docs/../a/y.py")

    def D08(self):
        self.w.assign(self.b, [("a/**", "exclusive")])
        self.w.assign(self.a, [("A/**", "shared")])
        return self.expect(self.edit(self.a, "A/x.py"), "deny", "A/x.py on a case-insensitive file system")

    def D09(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        s = self.sim.agent_req({"lease_id": self.a.lease_id, "glob": "docs/api.md", "mode": "shared",
                                "origin": "hot"}, self.a.agent)
        if s != 200:
            return "hot mark refused: %s" % s
        return self.expect(self.edit(self.a, "docs/api.md"), "deny", "a hot mark does not widen the lease")

    def D10(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        self.w.coord.release(lead_ctx(), self.a.lease_id)
        return self.expect(self.edit(self.a, "a/x.py"), "deny", "an edit after a release")

    def D11(self):
        self.w.assign(self.a, [("a/x*.py", "exclusive")])
        return self.expect(self.edit(self.a, "a/xyz/q.go"), "deny", "a/xyz/q.go is not in a/x*.py")

    def D12(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        first = self.edit(self.a, "a/x.py")
        self.w.coord.claim(lead_ctx(), {"task": self.a.task, "attempt": 2, "holder": "thief", "host": "local",
                                        "steal": True, "force": True, "reservations": []})
        return self.expect(first, "allow", "first edit") or \
            self.expect(self.edit(self.a, "a/x.py"), "deny", "the same edit after a steal")

    def D13(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        with open(os.path.join(self.a.wt, ".claude", "wavecoord.json"), "w") as f:
            json.dump({"project": "sim", "fence": "off"}, f)
        try:
            return self.expect(self.edit(self.a, "docs/api.md"), "deny", "the worktree config sets fence off")
        finally:
            git(self.a.wt, "checkout", "-q", "--", ".claude/wavecoord.json")

    def D14(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        self.sim.op_expire()
        if self.w.coord.state.lease(self.a.lease_id)["state"] != "stale":
            return "the lease did not turn stale"
        return self.expect(self.edit(self.a, "docs/api.md"), "deny", "outside the lease while it is stale")

    def D15(self):
        self.w.assign(self.b, [("docs/**", "exclusive")])
        self.w.assign(self.a, [("a/**", "exclusive")])
        git(self.a.wt, "mv", "docs/api.md", "a/api.md")
        rc = guard.check("staged", cwd=self.a.wt, env=self.w.commit_env(self.a), err=io.StringIO())
        git(self.a.wt, "reset", "-q", "--hard", "HEAD")
        return self.expect(rc, 1, "a rename of a foreign file into the lease")

    def D16(self):
        self.w.assign(self.a, [("x[1].py", "exclusive")])
        return self.expect(self.edit(self.a, "x1.py"), "deny", "x1.py is not x[1].py") or \
            self.expect(self.edit(self.a, "x[1].py"), "allow", "x[1].py is in the lease")

    def D17(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        return self.expect(self.edit(self.a, os.path.join(self.b.wt, "a", "x.py")), "deny", "a sibling worktree")

    def D18(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        with open(os.path.join(self.a.wt, "docs", "api.md"), "a") as f:
            f.write("x\n")
        git(self.a.wt, "add", "-A")
        rc = guard.check("staged", cwd=self.a.wt, env=self.w.commit_env(self.a), err=io.StringIO())
        git(self.a.wt, "reset", "-q", "--hard", "HEAD")
        return self.expect(rc, 1, "a commit outside the lease from a Bash env with no identity")

    def D19(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        with open(os.path.join(self.a.wt, "a", "x.py"), "a") as f:
            f.write("x\n")
        git(self.a.wt, "commit", "-q", "-am", "c")
        sha = git(self.a.wt, "rev-parse", "HEAD").strip()
        self.w.proxy.fault = "down"
        try:
            code = gatecheck.run(None, self.a.task, 1, "origin/main..%s" % sha, None, "%s@%d" % (self.a.task, self.a.epoch),
                                 self.w.repo, env=self.w.base_env("gate"), out=io.StringIO(), err=io.StringIO())
        finally:
            self.w.proxy.fault = None
        return None if code != 0 else "gate-check exit 0 with the coordinator down"

    def D20(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        self.w.proxy.fault = "notjson"
        try:
            return self.expect(self.edit(self.a, "a/x.py"), "deny", "a 200 answer that is not JSON")
        finally:
            self.w.proxy.fault = None

    def D21(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        s = self.sim.agent_req({"lease_id": self.a.lease_id, "glob": "**", "mode": "exclusive"}, self.a.agent)
        return self.expect(s, 403, "an agent token adds an exclusive **")

    def D22(self):
        self.w.assign(self.a, [("docs/**", "exclusive")])
        self.w.assign(self.b, [("a/**", "exclusive")])
        try:
            self.w.coord.reserve(lead_ctx(), {"lease_id": self.b.lease_id,
                                              "reservations": [{"glob": "docs/api.md", "mode": "exclusive"}]})
        except ApiError as e:
            return self.expect(e.code, "conflict", "an overlapping exclusive grant")
        return "an overlapping exclusive grant was made in enforce mode"


def table_check(name):
    """Run one named check in a fresh world. -> None or the failure text."""
    ci = (name == "D08") and not CI_FS
    with fencedefects.ci_forced(ci):
        t = Table(ci=ci or None)
        try:
            return getattr(t, name)()
        finally:
            t.close()


def table_job(args):
    """Worker-process entry: (check name, defect to apply or None) -> (name, defect, failure or None)."""
    name, defect = args
    if defect:
        with fencedefects.patched(defect):
            return name, defect, table_check(name)
    return name, defect, table_check(name)


def pool():
    n = min(8, max(2, (os.cpu_count() or 2)))
    return ProcessPoolExecutor(max_workers=n, mp_context=multiprocessing.get_context("spawn"))


class DefectTest(unittest.TestCase):
    def test_table_checks_pass_without_defects(self):
        with pool() as ex:
            res = list(ex.map(table_job, [(n, None) for n in sorted(fencedefects.DEFECTS)]))
        for name, _d, failure in res:
            with self.subTest(defect=name):
                self.assertIsNone(failure)

    def test_every_defect_is_caught(self):
        from test_fence_invariant import FIXED_SEEDS
        k = int(os.environ.get("WAVECOORD_FENCE_DEFECT_SEEDS", "2"))
        jobs = [(s, name, None, (name == "D08") and not CI_FS) for name in sorted(fencedefects.DEFECTS)
                for s in FIXED_SEEDS[:k]]
        with pool() as ex:
            tables = ex.map(table_job, [(n, n) for n in sorted(fencedefects.DEFECTS)])
            futs = list(ex.map(fencedefects.run_seed, jobs))
            table_hits = {name: failure for name, _d, failure in tables}
        random_hits = {}
        for seed, name, failures, _st in futs:
            if failures:
                random_hits.setdefault(name, []).append((seed, failures[0]))
        rows, missed = [], []
        for name in sorted(fencedefects.DEFECTS):
            t = table_hits[name]
            r = random_hits.get(name)
            if t is None and not r:
                missed.append(name)
            rows.append("%s  table:%-6s random:%-14s %s" % (
                name, "caught" if t else "MISSED", ("caught (seed %s)" % r[0][0]) if r else "-",
                fencedefects.DEFECTS[name][0]))
        sys.stderr.write("\nseeded defects (%d seeds each in the simulation):\n  %s\n" % (k, "\n  ".join(rows)))
        self.assertEqual(missed, [], "defects that no test caught")


if __name__ == "__main__":
    unittest.main()
