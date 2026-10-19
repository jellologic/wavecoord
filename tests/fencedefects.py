"""The 24 seeded defects of tests/fence/README.md, section 2, as patches.

Each defect is a small monkeypatch of one function or constant of the code
under test. `patched(name)` applies one and undoes it on exit. The patches
are in a module (not in the test) so that a worker process of the parallel
runner can apply the same patch (`run_seed`).
"""

import contextlib
import fnmatch
import os

import _paths  # noqa: F401
from wavecoord import fence, fencecore, gatecheck, globs, guard, hooks

_orig = {}


def _o(mod, name):
    return _orig.setdefault((mod.__name__, name), getattr(mod, name))


def _covers_d01(glob, path, fold_case=False):
    """D01: a literal prefix compare without the `/`: `a/b` covers `a/bc`."""
    g = glob.rstrip("/")
    if not any(c in g for c in "*?[") and path.startswith(g):
        return True
    return _o(globs, "covers")(glob, path, fold_case)


def _covers_d16(glob, path, fold_case=False):
    """D16: `[` read as a wildcard (fnmatch)."""
    if fnmatch.fnmatchcase(path, glob):
        return True
    return _o(globs, "covers")(glob, path, fold_case)


def _cached_check():
    real = _o(fence, "server_check")
    cache = {}

    def check(client, body, retry=True):
        key = (body.get("agent"), tuple(body.get("paths") or ()), body.get("source"))
        if key in cache:
            return cache[key]
        r = real(client, body, retry)
        if r.ok and not r.violations:
            cache[key] = r
        return r
    return check


def _override_d06(env, cwd=None):
    real = _o(fence, "override_active")(env, cwd)
    if real is not None:
        return real
    if (env.get("WAVECOORD_FENCE_OFFLINE") or "").strip():
        return {"nonce": "env", "created": 0, "until": 0, "reason": "env"}
    cfg = fence.find_repo_config(cwd) if cwd else None
    if cfg and cfg.get("fence_offline"):
        return {"nonce": "cfg", "created": 0, "until": 0, "reason": "cfg"}
    return None


def _modes_d13(env, cwd):
    return fence.worktree_modes(cwd) or fence.launcher_modes(env) or dict(fencecore.DEFAULT_MODES)


def _writer_lease_d10(st, agent):
    mine = [l for l in st.d["leases"].values() if l["holder"] == agent]
    return max(mine, key=lambda l: l["id"]) if mine else None


def _parse_d20(status, resp):
    r = _o(fence, "parse_check_response")(status, resp)
    if r is None and status == 200:
        return [], None
    return r


def _foreign_nofold(glob, path, fold_case=False):
    return _o(fencecore, "foreign_hit")(glob, path, False)


DEFECTS = {
    "D01": ("overlap off by one: a/b covers a/bc",
            [(globs, "covers", lambda: _covers_d01), (fencecore, "covers", lambda: _covers_d01)]),
    "D02": ("fail-open on timeout: the hook prints nothing when the budget ends",
            [(hooks, "timeout_output", lambda: (lambda event, env, cwd: None))]),
    "D03": ("stale epoch ignored", [(fencecore, "epoch_ok", lambda: (lambda st, lease, epoch: True))]),
    "D04": ("shared handled as exclusive", [(fencecore, "is_exclusive", lambda: (lambda r: True))]),
    "D05": ("exclusive handled as shared", [(fencecore, "is_exclusive", lambda: (lambda r: False))]),
    "D06": ("override read from an agent-writable place (env or worktree config)",
            [(fence, "override_active", lambda: _override_d06)]),
    "D07": ("no path normalization: symlinks and .. not resolved",
            [(fence, "resolve_target", lambda: (lambda path, cwd: os.path.abspath(
                path if os.path.isabs(path) else os.path.join(cwd or os.getcwd(), path))))]),
    "D08": ("case-insensitive file system: A/x.py passes against an exclusive a/**",
            [(fence, "canon_case", lambda: (lambda top, rel: rel)), (fencecore, "foreign_hit", lambda: _foreign_nofold)]),
    "D09": ("hot-file marks counted in R(L)",
            [(fencecore, "grant_set", lambda: (lambda st, lease: [r for r in st.live_reservations()
                                                                   if r["lease_id"] == lease["id"]]))]),
    "D10": ("a released (not stolen) lease passes (no no_lease)",
            [(fencecore, "writer_lease", lambda: _writer_lease_d10),
             (fencecore, "epoch_now", lambda: (lambda st, task: st.last_epoch(task))),
             (fencecore, "lease_checked", lambda: (lambda l: l["state"] == "active"))]),
    "D11": ("globs_overlap used for outside_lease",
            [(fencecore, "member", lambda: (lambda g, p: globs.globs_overlap(p, g)))]),
    "D12": ("an allow cached for the session", [(fence, "server_check", _cached_check)]),
    "D13": ("the fence mode read from the worktree config", [(fence, "modes", lambda: _modes_d13)]),
    "D14": ("outside_lease checked only for an active lease",
            [(fencecore, "lease_checked", lambda: (lambda l: l["state"] == "active"))]),
    "D15": ("diffs without --no-renames", [(fence, "DIFF_RENAME_FLAGS", lambda: [])]),
    "D16": ("[ in a path read as a wildcard",
            [(globs, "covers", lambda: _covers_d16), (fencecore, "covers", lambda: _covers_d16)]),
    "D17": ("a target in a sibling worktree treated as outside the repo",
            [(fence, "same_repo", lambda: (lambda a, b: False))]),
    "D18": ("the guard with no identity passes", [(guard, "read_identity", lambda: (lambda top: None))]),
    "D19": ("gate-check exits 0 when the coordinator is unavailable", [(gatecheck, "UNAVAILABLE_EXIT", lambda: 0)]),
    "D20": ("a 200 with a body that is not valid JSON read as no violations",
            [(fence, "parse_check_response", lambda: _parse_d20)]),
    "D21": ("an agent token can add a claim exclusive reservation **",
            [(fencecore, "agent_reservation_ok", lambda: (lambda it: True))]),
    "D22": ("exclusive overlap granted in enforce mode",
            [(fencecore, "grant_conflicts", lambda: (lambda st, items, exclude_lease=None: []))]),
    "D23": ("the override journal flush drops the whole batch on a deduplicated ack",
            [(fence, "acked_entries", lambda: (lambda notice, batch: set(r.get("n") for r in batch)))]),
    "D24": ("a task PR that changes the lead's config (fence modes, claude_flags, hooks) passes the merge check",
            [(gatecheck, "config_changes", lambda: (lambda repo, base, head, paths: []))]),
    "D25": ("a task PR that changes the spec tree passes the merge check",
            [(gatecheck, "spec_changes", lambda: (lambda repo, base, head, paths: []))]),
}


@contextlib.contextmanager
def patched(name):
    saved = []
    for mod, attr, make in DEFECTS[name][1]:
        _o(mod, attr)
        saved.append((mod, attr, getattr(mod, attr)))
        setattr(mod, attr, make())
    try:
        yield
    finally:
        for mod, attr, old in reversed(saved):
            setattr(mod, attr, old)


@contextlib.contextmanager
def ci_forced(on):
    """Report a case-insensitive file system (for D08 on Linux; see the README)."""
    if not on:
        yield
        return
    old = fence.case_insensitive
    fence.case_insensitive = lambda top: True
    try:
        yield
    finally:
        fence.case_insensitive = old


def run_seed(args):
    """Worker-process entry: (seed, defect or None, ops or None, ci) -> (seed, defect, failures, stats)."""
    seed, defect, ops, ci = args
    import traceback
    import fencesim
    try:
        with ci_forced(ci):
            if defect:
                with patched(defect):
                    f, st = fencesim.run(seed, ops=ops, ci=ci or None)
            else:
                f, st = fencesim.run(seed, ops=ops, ci=ci or None)
    except Exception:  # noqa: BLE001 - a crash of the simulation is a failure, with its traceback
        return seed, defect, ["seed %s: the simulation crashed:\n%s" % (seed, traceback.format_exc()[-1500:])], {}
    return seed, defect, f, st
