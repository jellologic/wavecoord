"""`wavecoord spec check` and `lint`: the rules of docs/design/spec-layer.md.

Each record on its own: the layout (section 2), the record frame, keys, types
and sections (section 3), the artifact keys (3.6), EARS for an approved
requirement (3.4) and the canonical form (rule M8). The rules across records
(references, blockers, alignment A1-A7, predicate P, suspect tasks) are in
graph.py; `check` runs both.

    problems = check(tree)     # [Problem]; level "error" or "warning"
"""

import re

from . import doc
from .model import COORDINATOR_STATUSES, FRAME, KINDS, SCHEMA_VERSION

CONTRACT_KINDS = ("interface", "api", "schema", "event")
SENTENCE_RE = re.compile(r"[a-z0-9)\]\"'][.!?] +[A-Z]")
EARS_RE = re.compile(r"\bshall\b", re.I)


def canonical_glob(g):
    """A path or glob with `./` segments and repeated `/` removed: `./src//a.py` -> `src/a.py`."""
    parts = [p for p in str(g).split("/") if p not in ("", ".")]
    return "/".join(parts) + ("/" if str(g).endswith("/") and parts else "")


class Problem(object):
    __slots__ = ("level", "rule", "id", "path", "msg")

    def __init__(self, level, rule, nid, path, msg):
        self.level, self.rule, self.id, self.path, self.msg = level, rule, nid, path, msg

    def to_json(self):
        return {"level": self.level, "rule": self.rule, "id": self.id, "path": self.path, "msg": self.msg}

    def __str__(self):
        return "%s %s %s: %s" % (self.level, self.rule, self.path, self.msg)


def ears_class(statement):
    """The EARS pattern of a statement, or None when it is not one sentence with `shall` exactly once."""
    s = (statement or "").strip()
    if not s or "\n" in s or len(EARS_RE.findall(s)) != 1:
        return None
    low = s.lower()
    classes = []
    for word, name in (("where ", "optional"), ("while ", "state"), ("when ", "event"), ("if ", "unwanted")):
        if low.startswith(word) or (", " + word) in low:
            classes.append(name)
    if "unwanted" in classes and not re.search(r"\bthen\b", low):
        return None
    return "+".join(classes) if classes else "ubiquitous"


def check_record(n):
    """Problems of one record on its own."""
    out = []

    def err(rule, msg, level="error"):
        out.append(Problem(level, rule, n.id, n.path, msg))

    rec = n.raw
    if rec is None:
        return out
    kind = KINDS[n.kind]
    if rec.get("kind") != n.kind:
        err("kind", "the path makes this a %s, but kind is %r" % (n.kind, rec.get("kind")))
        return out
    if rec.get("schema") != SCHEMA_VERSION:
        err("schema", "schema %r is not supported (this wavecoord reads schema %d)" % (rec.get("schema"),
                                                                                         SCHEMA_VERSION))
    for e in n.invalid:  # spec-record.schema.json and exec, from tree.record_errors
        err("schema", e)
    if out:
        return out  # the rules below read the record's fields as the schema types them
    for key in ("paths", "shared_paths"):
        for g in rec.get(key) or []:
            if canonical_glob(g) != g:
                err("paths", "%s %s: write it as %s (no ./, // or /./), so overlaps and leases compare it"
                    % (key, g, canonical_glob(g)))
    for s in kind.sections:
        if s.required and not rec["sections"].get(s.key):
            err("section", "the section %s is required for a %s" % (s.title, n.kind))
    for lst in ("inputs", "outputs"):
        seen = set()
        for a in rec.get(lst) or []:
            if not isinstance(a, dict):
                continue
            aid = a.get("id")
            if aid in seen:
                err("artifact", "%s lists %s twice" % (lst, aid))
            seen.add(aid)
            if lst == "outputs" and a.get("kind") in CONTRACT_KINDS and not a.get("contract"):
                err("artifact", "output %s (%s) needs a contract" % (aid, a.get("kind")))
            if lst == "outputs" and ("from" in a or "external" in a):
                err("artifact", "output %s: from and external belong to inputs" % aid)
            if lst == "inputs":
                if bool(a.get("from")) == bool(a.get("external")):
                    err("artifact", "input %s needs exactly one of from and external: true" % aid)
                if a.get("external") and not a.get("contract"):
                    err("artifact", "external input %s needs a contract (what is assumed)" % aid)
            if a.get("kind") == "file" and any(c in str(aid) for c in "*?["):
                err("artifact", "%s is a glob: use kind files" % aid)
    if n.kind == "requirement" and rec.get("status") == "approved":
        st = rec["sections"].get("statement")
        if ears_class(st) is None:
            err("ears", "an approved requirement has one Statement sentence, with `shall` exactly once, in EARS"
                        " form (When/While/If ... then/Where/The ...)")
        if not rec["sections"].get("scenarios") and not rec["sections"].get("verify"):
            err("requirement", "an approved requirement has at least one Scenario or Verify command")
    if n.kind == "task" and len(n.id.replace("/", ".")) > 64:
        err("id", "the coordinator's task id %s is longer than 64 characters: use shorter names" % n.id.replace(
            "/", "."))
    keys = [k for k in _frontmatter_keys(n.text)]
    if keys[:3] != list(FRAME) and not out:
        err("frame", "the frontmatter starts with status, kind and schema (rule M13)")
    if not out:
        canon = doc.render(rec)
        if canon != n.text:
            err("format", "not in canonical form (run `wavecoord spec fmt`)")
    return out


def _frontmatter_keys(text):
    lines = text.replace("\r\n", "\n").split("\n")
    out = []
    for l in lines[1:]:
        if l.strip() == "---":
            break
        if l and not l.startswith((" ", "#")) and ":" in l:
            out.append(l.split(":", 1)[0])
    return out


def check(tree, graph=None):
    """Every rule: each record on its own, then the rules across records (graph.py)."""
    out = []
    for path, msg in tree.layout_errors:
        out.append(Problem("error", "layout", None, path, msg))
    for nid, errs in sorted(tree.parse_errors.items()):
        node = tree.nodes.get(nid) or tree.decisions.get(nid)
        for e in errs:
            out.append(Problem("error", "parse", nid, node.path, e))
    for n in list(tree.nodes.values()) + list(tree.decisions.values()):
        out.extend(check_record(n))
    if graph is None:
        from .graph import Graph
        graph = Graph(tree)
    out.extend(graph.problems)
    return sorted(out, key=lambda p: (p.path or "", p.rule, p.msg))


def lead_transition(old, new):
    """Why a plan change (an op, a hand edit in a plan PR) may not move a task from status `old` to `new`, or
    None (section 7.1). The coordinator writes in_progress, in_review and done; done is final; a live task
    (in_progress, in_review) may only be dropped. `old` is None for a new task."""
    if new in COORDINATOR_STATUSES:
        return "%s is written by the coordinator, not in a plan change" % new
    if old == "done":
        return "done is final: a plan change does not undo it (plan a new task instead)"
    if old in ("in_progress", "in_review") and new != "dropped":
        return "the task is %s: a plan change may only drop it" % old
    return None


WRITER_STATUSES = ("planned", "in_progress", "in_review", "done")  # what the status writer writes over and to


def _status_only(old, new):
    """Whether record text `new` differs from `old` in its frontmatter `status:` line only."""
    a, b = (old or "").split("\n"), (new or "").split("\n")
    if len(a) != len(b):
        return False
    diff = [(x, y) for x, y in zip(a, b) if x != y]
    return len(diff) == 1 and all(l.rstrip("\r").startswith("status:") for l in diff[0])


STATUS_TRAILER_RE = re.compile(r"^Wavecoord-Status: (\S+) (\S+) (\S+)\s*$", re.M)


def status_trailers(messages):
    """{(coordinator task id, from, to)} of the `Wavecoord-Status:` lines in commit messages (the status writer
    puts one per change in its commit)."""
    return set(STATUS_TRAILER_RE.findall(messages or ""))


def status_commit(before, after, trailers=()):
    """Whether the change from tree `before` to tree `after` is a status commit of the coordinator (section 7.2):
    no record added, removed or moved, every changed record a task whose `status:` line alone changed, each from
    and to a status the writer writes (planned, in_progress, in_review, done), and each change named by a
    `Wavecoord-Status:` trailer in `trailers` (status_trailers). That is what the status writer pushes, or carries
    on its `wavecoord/status` PR; a hand edit has no trailer."""
    if set(before.nodes) != set(after.nodes) or set(before.decisions) != set(after.decisions):
        return False
    trailers = set(trailers or ())
    changed = 0
    for nid, n in list(after.nodes.items()) + list(after.decisions.items()):
        old = before.nodes.get(nid) or before.decisions.get(nid)
        if old.text == n.text:
            continue
        if n.kind != "task" or not _status_only(old.text, n.text):
            return False
        if old.status not in WRITER_STATUSES or n.status not in WRITER_STATUSES:
            return False
        if (nid.replace("/", "."), old.status, n.status) not in trailers:
            return False
        changed += 1
    return changed > 0


def _flat_alias(n, before):
    """The flat plan id (no `/`) that a new task carries in `aliases`, which no node of `before` holds: the mark
    of a task that `wavecoord spec migrate-plan` moved from plan/tasks, or None."""
    aliases = (n.rec or n.raw or {}).get("aliases")
    for a in aliases if isinstance(aliases, list) else []:
        if isinstance(a, str) and "/" not in a and a not in before.nodes and a.replace(".", "/") not in before.nodes:
            return a
    return None


def plan_change_problems(before, after, ci=False, trailers=()):
    """Errors (rule `status`) for the task statuses that a plan change sets against section 7.1, from the
    tree before and the tree after. A moved task is compared with its old id (an alias).

    `ci` (`wavecoord spec check --base`, which sees whole PRs): a status commit of the coordinator (status_commit,
    with the commits' `trailers`) is no plan change, and a new task migrated from the flat plan (an alias with no `/`) may carry the status
    migrate-plan gave it from the coordinator. The op API never gets these exemptions."""
    out = []
    if ci and status_commit(before, after, trailers):
        return out
    for nid, n in sorted(after.nodes.items()):
        if n.kind != "task" or n.status is None:
            continue
        old = before.nodes.get(nid)
        if old is None:
            aliases = (n.rec or n.raw or {}).get("aliases")
            for a in aliases if isinstance(aliases, list) else []:
                if a in before.nodes:
                    old = before.nodes[a]
                    break
        was = old.status if old is not None else None
        if was == n.status:
            continue
        if ci and old is None and n.status in WRITER_STATUSES and _flat_alias(n, before):
            continue
        why = lead_transition(was, n.status)
        if why:
            out.append(Problem("error", "status", nid, n.path, "%s -> %s: %s" % (was or "(new)", n.status, why)))
    return out


def lint(tree):
    """Style warnings: several sentences on one line (M6), tables outside Design (M7)."""
    out = []
    for n in sorted(list(tree.nodes.values()) + list(tree.decisions.values()), key=lambda x: x.path):
        if n.rec is None:
            continue
        kind = KINDS[n.kind]
        for s in kind.sections:
            v = n.rec["sections"].get(s.key)
            if s.type != "prose" or not v:
                continue
            fstate = None
            for line in v.split("\n"):
                fstate, fence = doc.fence(fstate, line)
                if fence or line.lstrip().startswith(("-", "*", "|", "#", ">")) or line.startswith("    "):
                    if line.lstrip().startswith("|") and s.key != "design" and not fence:
                        out.append(Problem("warning", "table", n.id, n.path,
                                           "%s: a table outside Design (rule M7)" % s.title))
                    continue
                if SENTENCE_RE.search(line):
                    out.append(Problem("warning", "sentences", n.id, n.path,
                                       "%s: one sentence per line (rule M6): %r" % (s.title, line[:80])))
    return out
