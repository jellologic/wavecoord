"""`wavecoord spec migrate-plan`: move the flat task specs of plan/tasks into the spec tree (section 9.6).

Each task becomes `<cap>/unsorted/unsorted/tasks/<name>`, under a draft
capability, feature and requirement. Its needs, paths, exec, class, size and
sections are kept; `state: done` and `dropped` are kept, every other state
becomes `planned`; the phase becomes a tag; an old id that is a valid node
name stays as an alias. The lead then moves the tasks under real requirements
with `wavecoord spec mv`. The work is a list of `create` ops, so it is checked
like any other change.
"""

import re

LEGACY_FEATURE = "unsorted"
LEGACY_REQ = "unsorted"


def name_of(task_id, taken):
    base = re.sub(r"[^a-z0-9-]+", "-", task_id.lower()).strip("-")[:56] or "task"
    name, n = base, 2
    while name in taken:
        name = "%s-%d" % (base, n)
        n += 1
    taken.add(name)
    return name


def plan_ops(specs, cap="legacy"):
    """Flat task specs (task-spec.schema.json shape) -> (ops, {old id: new id})."""
    feat, req = "%s/%s" % (cap, LEGACY_FEATURE), "%s/%s/%s" % (cap, LEGACY_FEATURE, LEGACY_REQ)
    ops = [
        {"op": "create", "id": cap, "fields": {"title": "Legacy plan"},
         "sections": {"Intent": "Tasks moved from plan/tasks by migrate-plan."}},
        {"op": "create", "id": feat, "fields": {"title": "Unsorted tasks"},
         "sections": {"Intent": "Move each task under the requirement it serves."}},
        {"op": "create", "id": req, "fields": {"title": "Unsorted tasks"},
         "sections": {"Statement": "The legacy plan shall be sorted into requirements."}},
    ]
    taken, ids = set(), {}
    for s in specs:
        ids[s["id"]] = "%s/%s" % (req, name_of(s["id"], taken))
    for s in specs:
        new = ids[s["id"]]
        fields = {"title": s.get("title") or s["id"], "paths": list(s.get("paths") or []),
                  "class": s["class"], "size": s["size"], "exec": dict(s["exec"]),
                  "status": {"done": "done", "dropped": "dropped"}.get(s.get("state"), "planned")}
        if s.get("shared_paths"):
            fields["shared_paths"] = list(s["shared_paths"])
        needs = [ids[n] for n in s.get("needs") or [] if n in ids]
        if needs:
            fields["needs"] = needs
        phase = re.sub(r"[^a-z0-9._-]+", "-", str(s.get("phase") or "").lower()).strip("-.")
        if phase:
            fields["tags"] = [phase]
        old = s["id"]
        if re.match(r"^[a-z0-9][a-z0-9-]{0,62}$", old) and old != new.rsplit("/", 1)[1]:
            fields["aliases"] = [old]
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
