"""`wavecoord spec migrate-plan`: move the flat task specs of plan/tasks into the spec tree (section 9.6).

Each task becomes `<cap>/unsorted/unsorted/tasks/<name>`, under an active
capability and feature and an approved requirement, so the migrated tree runs
as the flat plan did (the plan PR that carries it is where the lead approves
it). Its needs, paths, exec, class, size and sections are kept; the phase
becomes a tag.

The status comes from the coordinator's effective state when it is known
(`states`, {old id: state}): merged -> done, a live attempt -> in_progress, a
parked lease -> in_review; else from the file: done and dropped are kept,
blocked becomes on_hold, every other state planned. The old id is kept in
`aliases`, so the coordinator's leases, attempts and merges under the old id
still name the task. Two open tasks whose paths overlap get `conflicts_with`
(the flat store serialized them with leases), so predicate P holds without new
needs. The work is a list of ops, so it is checked like any other change.
"""

import re

from wavecoord.globs import globs_overlap

LEGACY_FEATURE = "unsorted"
LEGACY_REQ = "unsorted"
MAX_COORD_ID = 64
SUFFIX_ROOM = 4  # "-NNN" for a name that is taken
STATUS_OF = {"done": "done", "dropped": "dropped", "blocked": "on_hold", "active": "in_progress",
             "review": "in_review"}


class MigrateError(ValueError):
    pass


def name_of(task_id, taken, room=56):
    base = re.sub(r"[^a-z0-9-]+", "-", task_id.lower()).strip("-")[:room].strip("-") or "task"
    name, n = base, 2
    while name in taken:
        name = "%s-%d" % (base, n)
        n += 1
    taken.add(name)
    return name


def _status(spec, states):
    """The task's status in the tree: the coordinator's state over the file's."""
    st = spec.get("state")
    live = (states or {}).get(spec["id"])
    if st not in ("done", "dropped") and live in ("done", "active", "review"):
        st = live
    elif st in ("active", "review"):
        st = "todo"  # in the file, but nothing live: the attempt ended without a merge
    return STATUS_OF.get(st, "planned")


def _overlap(a, b):
    for x in a.get("paths") or []:
        for y in (b.get("paths") or []) + (b.get("shared_paths") or []):
            if globs_overlap(x, y):
                return True
    for x in a.get("shared_paths") or []:
        for y in b.get("paths") or []:
            if globs_overlap(x, y):
                return True
    return False


def _reach(specs):
    """old id -> every old id it waits for, directly or not."""
    needs = {s["id"]: set(n for n in s.get("needs") or []) for s in specs}
    out = {}
    for i in needs:
        seen, stack = set(), list(needs[i])
        while stack:
            x = stack.pop()
            if x in seen or x not in needs:
                continue
            seen.add(x)
            stack.extend(needs[x])
        out[i] = seen
    return out


def plan_ops(specs, cap="legacy", states=None, taken_ids=()):
    """Flat task specs (task-spec.schema.json shape) -> (ops, {old id: new id}).

    states: the coordinator's effective states by old id; taken_ids: node ids already in the tree."""
    feat, req = "%s/%s" % (cap, LEGACY_FEATURE), "%s/%s/%s" % (cap, LEGACY_FEATURE, LEGACY_REQ)
    room = MAX_COORD_ID - len(req) - 1 - SUFFIX_ROOM
    if room < 8:
        raise MigrateError("--into %s is too long: task ids would pass %d characters" % (cap, MAX_COORD_ID))
    ops = [
        {"op": "create", "id": cap, "fields": {"title": "Legacy plan", "status": "active"},
         "sections": {"Intent": "Tasks moved from plan/tasks by migrate-plan."}},
        {"op": "create", "id": feat, "fields": {"title": "Unsorted tasks", "status": "active"},
         "sections": {"Intent": "Move each task under the requirement it serves."}},
        {"op": "create", "id": req, "fields": {"title": "Unsorted tasks", "status": "approved"},
         "sections": {"Statement": "The legacy tasks shall run as they did in plan/tasks.",
                      "Scenarios": [{"name": "A legacy task", "steps": [
                          ["Given", "a task moved from plan/tasks"], ["When", "its worker finishes"],
                          ["Then", "the task's Acceptance passes"]]}]}},
    ]
    taken, ids = set(), {}
    for s in specs:
        ids[s["id"]] = "%s/%s" % (req, name_of(s["id"], taken, room))
    status = {s["id"]: _status(s, states) for s in specs}
    reach = _reach(specs)
    conflicts = {}
    for i, a in enumerate(specs):  # open tasks that could run together and write the same paths
        for b in specs[:i]:
            if "done" in (status[a["id"]], status[b["id"]]) or "dropped" in (status[a["id"]], status[b["id"]]):
                continue
            if b["id"] in reach[a["id"]] or a["id"] in reach[b["id"]] or not _overlap(a, b):
                continue
            conflicts.setdefault(a["id"], []).append(ids[b["id"]])
    taken_ids = set(taken_ids) | set(ids.values()) | {cap, feat, req}
    for s in specs:
        new = ids[s["id"]]
        fields = {"title": s.get("title") or s["id"], "paths": list(s.get("paths") or []),
                  "class": s["class"], "size": s["size"], "exec": dict(s["exec"]), "status": status[s["id"]]}
        if s.get("shared_paths"):
            fields["shared_paths"] = list(s["shared_paths"])
        needs = [ids[n] for n in s.get("needs") or [] if n in ids]
        if needs:
            fields["needs"] = needs
        if conflicts.get(s["id"]):
            fields["conflicts_with"] = sorted(conflicts[s["id"]])
        phase = re.sub(r"[^a-z0-9._-]+", "-", str(s.get("phase") or "").lower()).strip("-._")[:64].rstrip("-._")
        if phase:
            fields["tags"] = [phase]
        old = s["id"]
        if old not in taken_ids:
            fields["aliases"] = [old]  # the coordinator's id of the task until now
        sections = {}
        sec = s.get("sections") or {}
        for key, title in (("context", "Context"), ("risks", "Risks"), ("acceptance", "Acceptance"),
                           ("prompt", "Prompt"), ("notes", "Notes")):
            if sec.get(key):
                sections[title] = sec[key]
        sections.setdefault("Context", "Moved from plan/tasks/%s.md." % s["id"])
        sections.setdefault("Prompt", "See the context.")
        ops.append({"op": "create", "id": new, "fields": fields, "sections": sections})
    return ops, ids
