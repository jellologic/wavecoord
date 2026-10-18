"""The server side of the edit fence (docs/design/edit-fence.md, section 2). Stdlib only.

`evaluate` is the predicate `ok(W)` of section 2.2 over one folded State. The
server calls it under its write lock, so the State is one consistent point in
the order of all events (the decision time `t`).

Each small function below is one rule of the design. The seeded-defect tests
(tests/test_fence_defects.py) patch these functions one at a time, so keep
each rule in its own function.
"""

from wavecoord.globs import covers, globs_overlap

MODES = ("off", "advisory", "enforce")
POINTS = ("edit", "commit", "push", "merge")
LEAD_ACTIONS = ("warn", "ask", "deny")
DEFAULT_MODES = {"edit": "advisory", "commit": "advisory", "push": "advisory", "merge": "advisory", "lead": "ask"}
GRANT_ORIGINS = ("claim", "lead")
VIOLATION_KINDS = ("no_lease", "stale_epoch", "outside_lease", "foreign_exclusive")


# ----------------------------------------------------------------- config

def parse_modes(raw):
    """The `fence` config value -> {edit, commit, push, merge, lead}. Bad values give the defaults.

    The value is a mode word for every point ("enforce"), or an object with
    one key per point and `lead`.
    """
    out = dict(DEFAULT_MODES)
    if isinstance(raw, str) and raw in MODES:
        for p in POINTS:
            out[p] = raw
    elif isinstance(raw, dict):
        for p in POINTS:
            if raw.get(p) in MODES:
                out[p] = raw[p]
        if raw.get("lead") in LEAD_ACTIONS:
            out["lead"] = raw["lead"]
    return out


def stricter(a, b):
    """The stricter of two mode dicts (per point; for `lead`, deny > ask > warn)."""
    out = {}
    for p in POINTS:
        out[p] = MODES[max(MODES.index(a.get(p, "advisory")), MODES.index(b.get(p, "advisory")))]
    out["lead"] = LEAD_ACTIONS[max(LEAD_ACTIONS.index(a.get("lead", "ask")), LEAD_ACTIONS.index(b.get("lead", "ask")))]
    return out


def any_enforce(modes):
    return any(modes.get(p) == "enforce" for p in POINTS)


# ----------------------------------------------------------------- paths

def path_syntax_error(p):
    """Why the server refuses path `p` (400), or None. The server never trusts client normalization."""
    if not isinstance(p, str) or not p:
        return "empty path"
    if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        return "absolute path %r" % p
    if "\\" in p:
        return "backslash in path %r" % p
    if any(seg in (".", "..", "") for seg in p.split("/")):
        return "path %r has a '.', '..' or empty segment" % p
    return None


def globbable(p):
    """Can any valid glob name path `p`? A space or a control character cannot be in a glob."""
    return not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in p)


# ----------------------------------------------------------------- the rules (patch points)

def is_exclusive(r):
    return r.get("mode") == "exclusive"


def reservation_origin(r):
    return r.get("origin") or "claim"


def grant_set(st, lease):
    """R(L): the live claim and lead reservations of lease L. Hot-file marks are not in it."""
    return [r for r in st.live_reservations()
            if r["lease_id"] == lease["id"] and reservation_origin(r) in GRANT_ORIGINS]


def lease_checked(lease):
    """outside_lease applies to every non-released lease (active, stale, parked)."""
    return lease.get("state") != "released"


def member(glob, path):
    """Membership for outside_lease: exact covers, never the conservative overlap."""
    return covers(glob, path)


def foreign_hit(glob, path, fold_case=False):
    """The foreign check: covers, case-folded on a case-insensitive file system."""
    return covers(glob, path, fold_case=fold_case)


def epoch_now(st, task):
    l = st.task_lease(task)
    return l["epoch"] if l else None


def epoch_ok(st, lease, epoch):
    return epoch == lease["epoch"] and epoch_now(st, lease["task"]) == epoch


def writer_lease(st, agent):
    """lease(A, t): the one non-released lease of agent A, or None."""
    return st.agent_lease(agent) if agent else None


def agent_reservation_ok(item):
    """An agent token may add only shared hot-file marks (decision D4)."""
    return item.get("mode") == "shared" and (item.get("origin") or "hot") == "hot"


def grant_conflicts(st, items, exclude_lease=None):
    """Overlaps of new grants with live grants of other leases (at least one exclusive)."""
    out = []
    for it in items:
        for r in st.live_reservations():
            if r["lease_id"] == exclude_lease or reservation_origin(r) not in GRANT_ORIGINS:
                continue
            if "exclusive" not in (it["mode"], r["mode"]):
                continue
            if globs_overlap(it["glob"], r["glob"]):
                out.append({"glob": it["glob"], "lease_id": r["lease_id"], "task": r["task"],
                            "other_glob": r["glob"], "mode": r["mode"]})
    return out


# ----------------------------------------------------------------- the predicate

def _v(path, kind, lease=None, glob=None, task=None, holder=None):
    return {"path": path, "kind": kind, "lease_id": lease["id"] if lease else None,
            "task": task if task is not None else (lease["task"] if lease else None),
            "holder": holder if holder is not None else (lease["holder"] if lease else None), "glob": glob}


def evaluate(st, agent, paths, task=None, epoch=None, fold_case=False):
    """The violations of writes of `paths` by `agent` with claim (task, epoch). [] means ok for all.

    A write is a worker write when the request names a task or an epoch, or
    when the agent holds a lease. Otherwise (the lead, other sessions) only
    foreign_exclusive applies.
    """
    L = writer_lease(st, agent)
    worker = task is not None or epoch is not None or L is not None
    live = st.live_reservations()
    out = []
    for p in paths:
        if worker:
            if L is None or (task is not None and task != L["task"]):
                out.append(_v(p, "no_lease", L, task=task, holder=agent))
            else:
                if epoch is not None and not epoch_ok(st, L, epoch):
                    out.append(_v(p, "stale_epoch", L))
                if lease_checked(L) and not (globbable(p) and any(member(r["glob"], p) for r in grant_set(st, L))):
                    out.append(_v(p, "outside_lease", L))
        for r in live:
            if L is not None and r["lease_id"] == L["id"]:
                continue
            if not is_exclusive(r):
                continue
            other = st.lease(r["lease_id"])
            if other is None or other["state"] == "released":
                continue
            if foreign_hit(r["glob"], p, fold_case):
                out.append(_v(p, "foreign_exclusive", other, glob=r["glob"]))
    return out


def attempt_lease(st, task, n):
    """(attempt record, its lease) for attempt `task`/`n`, or (None, None)."""
    a = st.get("attempts", "%s/%d" % (task, n))
    if a is None:
        return None, None
    lid = a.get("lease_id")
    return a, (st.lease(lid) if lid is not None else None)


def lineage_ok(st, task, n, claimed, lease):
    """Is `lease` (the current lease of attempt task/n) a newer lease of the same attempt as epoch `claimed`?

    A resolver is a --continue run of the same attempt on the same branch: it
    takes over the attempt's lease with a new epoch. The PR body still names
    the older epoch, so the gate accepts it when that older lease was a lease
    of the same attempt (lead decision on PR #8).
    """
    if lease is None or lease.get("state") == "released" or lease.get("attempt") != n or lease.get("task") != task:
        return False
    if not isinstance(claimed, int) or lease["epoch"] <= claimed:
        return False
    return any(l["task"] == task and l.get("attempt") == n and l["epoch"] == claimed and l["id"] != lease["id"]
               for l in st.d["leases"].values())


def verdict(violations):
    return "pass" if not violations else "fail"
