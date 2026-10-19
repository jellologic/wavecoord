"""The record kinds of the spec tree (docs/design/spec-layer.md, sections 2 and 3).

A record is a plain dict: the frontmatter keys plus `sections`, a dict of the
body sections by key. This module says, per kind, which keys and sections exist,
in which canonical order, which lists are kept sorted, and the section types.
The JSON shape is checked against schemas/spec-record.schema.json.
"""

import re

SCHEMA_VERSION = 1
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
DATE_SLUG_RE = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})-([a-z0-9][a-z0-9-]{0,62})$")
NODE_FILE = "_node.md"
TASKS_DIR = "tasks"
DECISIONS_DIR = "decisions"
RESERVED = (NODE_FILE[:-3], TASKS_DIR, DECISIONS_DIR)

# The first three frontmatter lines of every record, in this order (rule M13).
FRAME = ("status", "kind", "schema")

# Lists whose order carries no meaning: kept sorted (rule M4).
SORTED_LISTS = ("needs", "conflicts_with", "tags", "aliases", "owners", "supersedes", "affects", "paths",
                "shared_paths")
# Lists of artifacts: kept sorted by id; keys of an artifact in this order.
ARTIFACT_LISTS = ("inputs", "outputs")
ARTIFACT_KEYS = ("id", "kind", "from", "external", "contract")
# exec keys in the order of task-spec.schema.json#/definitions/exec.
EXEC_KEYS = ("model", "fallback_model", "effort", "max_turns", "budget_usd", "max_tokens", "timeout_min",
             "host_needs", "split_if", "autocompact_window", "provider", "provider_fallback")

class Section(object):
    def __init__(self, title, typ, required=False):
        self.title = title
        self.key = title.lower().replace(" ", "_").replace("-", "_")
        self.type = typ  # prose, list, commands, scenarios, risks
        self.required = required


class Kind(object):
    def __init__(self, name, depth, statuses, keys, sections):
        self.name = name
        self.depth = depth  # 1..4, or None for a decision (any node)
        self.statuses = statuses
        self.keys = keys  # canonical order after FRAME
        self.sections = sections

    def section(self, key):
        for s in self.sections:
            if s.key == key:
                return s
        return None


_NODE_TAIL = ("needs", "conflicts_with", "inputs", "outputs")

KINDS = {
    "capability": Kind("capability", 1, ("draft", "active", "deprecated"),
                       ("title", "owners") + _NODE_TAIL + ("tags", "aliases"),
                       [Section("Intent", "prose", True), Section("Non-goals", "list"), Section("Design", "prose"),
                        Section("Open questions", "list")]),
    "feature": Kind("feature", 2, ("draft", "active", "deprecated"),
                    ("title", "owners") + _NODE_TAIL + ("tags", "aliases"),
                    [Section("Intent", "prose", True), Section("Non-goals", "list"), Section("Design", "prose"),
                     Section("Open questions", "list")]),
    "requirement": Kind("requirement", 3, ("draft", "approved", "deprecated"),
                        ("title",) + _NODE_TAIL + ("supersedes", "tags", "aliases"),
                        [Section("Statement", "prose", True), Section("Scenarios", "scenarios"),
                         Section("Verify", "commands"), Section("Rationale", "prose"), Section("Notes", "prose")]),
    "task": Kind("task", 4, ("draft", "planned", "on_hold", "dropped", "in_progress", "in_review", "done"),
                 ("title", "basis") + _NODE_TAIL + ("paths", "shared_paths", "class", "size", "exec", "tags",
                                                    "aliases"),
                 [Section("Context", "prose", True), Section("Risks", "risks"), Section("Acceptance", "commands"),
                  Section("Prompt", "prose", True), Section("Notes", "prose")]),
    "decision": Kind("decision", None, ("proposed", "accepted", "rejected"),
                     ("title", "affects", "supersedes", "tags"),
                     [Section("Context", "prose", True), Section("Decision", "prose", True),
                      Section("Consequences", "prose", True), Section("Alternatives", "prose")]),
}
DEPTH_KIND = {1: "capability", 2: "feature", 3: "requirement", 4: "task"}

# The statuses the coordinator writes; a plan PR may not set them (section 7.1).
COORDINATOR_STATUSES = ("in_progress", "in_review", "done")
# Statuses that make a task count as finished in a roll-up.
DONE = ("done",)
CLOSED = ("done", "dropped")

STEP_WORDS = ("Given", "When", "Then", "And", "But")


def normalize(rec):
    """The canonical model of a record: empty lists and sections dropped, unordered lists sorted."""
    kind = KINDS[rec["kind"]]
    out = {}
    for k, v in rec.items():
        if k == "sections":
            continue
        if isinstance(v, list) and not v:
            continue
        if k in SORTED_LISTS:
            v = sorted(v)
        elif k in ARTIFACT_LISTS:
            v = sorted((dict(a) for a in v), key=lambda a: str(a.get("id")))
        elif k == "exec" and isinstance(v, dict):
            v = dict(v)
        out[k] = v
    secs = {}
    for s in kind.sections:
        v = (rec.get("sections") or {}).get(s.key)
        if v in (None, "", []):
            continue
        secs[s.key] = v
    out["sections"] = secs
    return out
