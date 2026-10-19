"""The op API (docs/design/spec-layer.md, section 9.3): every write to the spec tree.

    changes, moves, problems = apply_ops(files, ops)

`files` is {path relative to spec.dir: text}. The ops run in memory, in order;
then the whole tree is checked. When an op fails, or the change adds an error
that the tree did not have before, nothing is returned but OpsError: the
caller writes nothing. `changes` is {path: new text, or None to delete};
`moves` lists (old path, new path) for `git mv`. Every record an op touches is
rendered canonically (rule M8).

A change is a plan change: it never writes a coordinator status (in_progress,
in_review, done) and never moves a task out of done, or out of in_progress or
in_review except to dropped (check.lead_transition). Only an internal caller
(migrate-plan, which keeps done tasks done) passes `coordinator=True`.
"""

import re

from wavecoord import schemaload
from wavecoord.plan.spec import SpecError

from . import doc
from .check import check, lead_transition, plan_change_problems
from .fingerprint import fingerprint
from .model import (ARTIFACT_LISTS, COORDINATOR_STATUSES, DECISIONS_DIR, KINDS, NODE_FILE, SCHEMA_VERSION,
                    SORTED_LISTS, STEP_WORDS, TASKS_DIR, shape_errors)
from .tree import Tree

SCALAR_KEYS = ("title", "basis", "class", "size")
# a sentence ends at . ! or ? before a space and a capital, a digit, a quote or a backquote
SENTENCE_END = re.compile(r"[.!?]\s+(?=[A-Z0-9\"'(`])")
# words whose period does not end a sentence (besides words with an inner dot: e.g., i.e., U.S., v1.2)
ABBREVIATIONS = {"approx", "ca", "cf", "dr", "esp", "etc", "excl", "fig", "incl", "inc", "jr", "ltd", "mr", "mrs",
                 "ms", "nr", "resp", "sr", "viz", "vs"}
NUMBERED = re.compile(r"^\s*[0-9]+[.)]\s")


class OpsError(ValueError):
    def __init__(self, errors):
        self.errors = list(errors)
        ValueError.__init__(self, "; ".join(self.errors))


# ------------------------------------------------------------------ ids and paths

def kind_of(nid):
    if "/%s/" % DECISIONS_DIR in nid:
        return "decision"
    depth = nid.count("/") + 1
    return {1: "capability", 2: "feature", 3: "requirement", 4: "task"}.get(depth)


def rel_of(nid):
    k = kind_of(nid)
    if k == "decision":
        return nid + ".md"
    if k == "task":
        parent, name = nid.rsplit("/", 1)
        return "%s/%s/%s.md" % (parent, TASKS_DIR, name)
    if k:
        return "%s/%s" % (nid, NODE_FILE)
    raise OpsError(["%s is not a node or decision id" % nid])


def id_of(rel):
    if rel.endswith("/" + NODE_FILE):
        return rel[:-len(NODE_FILE) - 1]
    if "/%s/" % TASKS_DIR in rel:
        a, b = rel.split("/%s/" % TASKS_DIR, 1)
        return "%s/%s" % (a, b[:-3])
    return rel[:-3]


def _ends_sentence(chunk, nxt):
    """Whether the period that ends `chunk` ends a sentence, given the character after the space."""
    words = chunk[:-1].split()
    w = words[-1].lstrip("(\"'`").lower() if words else ""
    if not w or w in ABBREVIATIONS or "." in w:
        return False
    if nxt.isdigit() and any(c.isdigit() for c in w):
        return False  # a number or a version: "v2. 0"
    return True


def _split_line(s):
    out, start = [], 0
    for m in SENTENCE_END.finditer(s):
        end = m.start() + 1
        if s[m.start()] == "." and not _ends_sentence(s[start:end], s[m.end()]):
            continue
        out.append(s[start:end].strip())
        start = m.end()
    out.append(s[start:].strip())
    return [p for p in out if p]


def split_sentences(text):
    """One sentence per line (rule M6). Lines that are code (fenced or indented), lists (also numbered),
    tables, headings or quotes are kept as they are; so is any indented line (a list item's continuation).
    A period after an abbreviation (approx., etc.), after a word with an inner dot (e.g., U.S.) or between
    numbers does not end a sentence."""
    out, fstate = [], None
    for line in (text or "").strip("\n").split("\n"):
        s = line.strip()
        fstate, in_fence = doc.fence(fstate, line)
        if (in_fence or not s or s.startswith(("-", "*", "+", "|", "#", ">")) or line[:1].isspace()
                or NUMBERED.match(line)):
            out.append(line.rstrip())
            continue
        out.extend(_split_line(s))
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def _one_line(v, what):
    """Refuse a line break inside a value that is written on one line (rule M2)."""
    if isinstance(v, str):
        if "\n" in v or "\r" in v:
            raise OpsError(["%s is one line: no line breaks" % what])
    elif isinstance(v, list):
        for x in v:
            _one_line(x, what)
    elif isinstance(v, dict):
        for k, x in v.items():
            _one_line(x, "%s.%s" % (what, k))


# ------------------------------------------------------------------ the work area

class Work(object):
    def __init__(self, files, coordinator=False):
        self.files = dict(files)
        self.moves = []
        self.coordinator = coordinator
        self.created = []  # tasks created without a basis: theirs is set after the last op

    def has(self, nid):
        return rel_of(nid) in self.files

    def load(self, nid, kind=None):
        rel = rel_of(nid)
        if rel not in self.files:
            raise OpsError(["no record %s" % nid])
        k = kind or kind_of(nid)
        try:
            rec = doc.parse(self.files[rel], k)
        except SpecError as e:
            raise OpsError(["%s does not parse: %s" % (nid, "; ".join(e.errors[:3]))])
        errs = shape_errors(rec, k)
        if errs:  # writing it back would lose data: fix the file first
            raise OpsError(["%s cannot be changed until it is fixed: %s" % (nid, "; ".join(errs[:3]))])
        return rec

    def save(self, nid, rec):
        self.files[rel_of(nid)] = doc.render(rec)


def _section(kind, name):
    k = KINDS[kind]
    key = name.lower().replace(" ", "_").replace("-", "_")
    s = k.section(key)
    if s is None:
        raise OpsError(["a %s has no section %s (it has: %s)" % (kind, name, ", ".join(x.title for x in k.sections))])
    return s


def _step(st):
    if (not isinstance(st, list) or len(st) != 2 or not all(isinstance(x, str) for x in st)
            or st[0].capitalize() not in STEP_WORDS or not st[1].strip()):
        raise OpsError(["a step is [Given|When|Then|And|But, text], not %r" % (st,)])
    _one_line(st[1], "a step")
    return [st[0].capitalize(), st[1].strip()]


def _section_value(sec, value):
    if sec.type == "prose":
        if not isinstance(value, str):
            raise OpsError(["%s is text" % sec.title])
        if sec.key == "statement":  # one EARS sentence: never split, abbreviations or not
            return "\n".join(l.rstrip() for l in value.strip().split("\n"))
        return split_sentences(value)
    if sec.type in ("list", "commands"):
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() and "\n" not in v for v in value):
            raise OpsError(["%s is a list of one-line items" % sec.title])
        return [v.strip() for v in value]
    if sec.type == "scenarios":
        out = []
        for sc in value if isinstance(value, list) else [None]:
            if (not isinstance(sc, dict) or not isinstance(sc.get("name"), str) or not sc["name"].strip()
                    or not isinstance(sc.get("steps"), list) or not sc["steps"]):
                raise OpsError(["%s is a list of {name, steps: [[Given|When|Then|And|But, text]]}" % sec.title])
            _one_line(sc["name"], "a scenario name")
            out.append({"name": sc["name"].strip(), "steps": [_step(st) for st in sc["steps"]]})
        return out
    if sec.type == "risks":
        if not isinstance(value, list) or not all(isinstance(r, dict) and r.get("risk") for r in value):
            raise OpsError(["%s is a list of {risk, mitigation, kind}" % sec.title])
        _one_line(value, "a risk")
        return value
    raise OpsError(["section type %s" % sec.type])


# ------------------------------------------------------------------ the ops

def op_create(w, op):
    nid = op["id"]
    kind = kind_of(nid)
    if kind is None:
        raise OpsError(["%s: an id has 1 to 4 names, or names a decision" % nid])
    if op.get("kind") and op["kind"] != kind:
        raise OpsError(["%s is a %s by its depth, not a %s" % (nid, kind, op["kind"])])
    if w.has(nid):
        raise OpsError(["%s exists" % nid])
    parent = nid.split("/%s/" % DECISIONS_DIR)[0] if kind == "decision" else nid.rsplit("/", 1)[0] if "/" in nid else None
    if parent and not w.has(parent):
        raise OpsError(["%s: the parent %s does not exist" % (nid, parent)])
    rec = {"status": "proposed" if kind == "decision" else "draft", "kind": kind, "schema": SCHEMA_VERSION}
    for k, v in (op.get("fields") or {}).items():
        if k in ("kind", "schema", "sections"):
            raise OpsError(["%s: %s is not a field to set" % (nid, k)])
        _one_line(v, k)
        rec[k] = v
    if kind == "task" and rec.get("status") in COORDINATOR_STATUSES and not w.coordinator:
        raise OpsError(["%s: %s is written by the coordinator, not in a plan change" % (nid, rec["status"])])
    rec["sections"] = {}
    for name, v in (op.get("sections") or {}).items():
        sec = _section(kind, name)
        rec["sections"][sec.key] = _section_value(sec, v)
    if kind == "task" and "basis" not in rec:
        w.created.append(nid)  # the basis is the requirement after the last op (apply_ops)
    w.save(nid, rec)


def op_set(w, op):
    rec = w.load(op["id"])
    key, value = op["key"], op.get("value")
    _one_line(value, key)
    if key.startswith("exec."):
        if rec["kind"] != "task":
            raise OpsError(["only a task has exec"])
        ex = dict(rec.get("exec") or {})
        if value is None:
            ex.pop(key[5:], None)
        else:
            ex[key[5:]] = value
        rec["exec"] = ex
    elif key in SCALAR_KEYS or key == "exec":
        if value is None:
            rec.pop(key, None)
        else:
            rec[key] = value
    else:
        raise OpsError(["set changes %s or exec.<key>; use set_status, add, remove or set_artifact for %s" % (
            ", ".join(SCALAR_KEYS), key)])
    w.save(op["id"], rec)


def op_set_status(w, op):
    rec = w.load(op["id"])
    st = op["status"]
    if st not in KINDS[rec["kind"]].statuses:
        raise OpsError(["%s is not a status of a %s (%s)" % (st, rec["kind"], ", ".join(KINDS[rec["kind"]].statuses))])
    if rec["kind"] == "task" and not w.coordinator and st != rec.get("status"):
        why = lead_transition(rec.get("status"), st)
        if why:
            raise OpsError(["%s: %s" % (op["id"], why)])
    rec["status"] = st
    w.save(op["id"], rec)


def op_set_section(w, op):
    rec = w.load(op["id"])
    sec = _section(rec["kind"], op["section"])
    v = op.get("value")
    if v in (None, "", []):
        rec["sections"].pop(sec.key, None)
    else:
        rec["sections"][sec.key] = _section_value(sec, v)
    w.save(op["id"], rec)


def _list_target(rec, key):
    if key in SORTED_LISTS:
        return "field"
    sec = KINDS[rec["kind"]].section(key.lower().replace(" ", "_").replace("-", "_"))
    if sec is not None and sec.type in ("list", "commands"):
        return sec.key
    raise OpsError(["%s is not a list (lists: %s, or a list section)" % (key, ", ".join(SORTED_LISTS))])


def op_add(w, op):
    rec = w.load(op["id"])
    target = _list_target(rec, op["key"])
    v = op["value"]
    if target == "field":
        cur = list(rec.get(op["key"]) or [])
        if v not in cur:
            cur.append(v)
        rec[op["key"]] = cur  # render sorts it (rule M4)
    else:
        cur = list(rec["sections"].get(target) or [])
        if v not in cur:
            cur.append(v)
        rec["sections"][target] = cur
    w.save(op["id"], rec)


def op_remove(w, op):
    rec = w.load(op["id"])
    target = _list_target(rec, op["key"])
    lst = rec.get(op["key"]) if target == "field" else rec["sections"].get(target)
    if op["value"] not in (lst or []):
        raise OpsError(["%s has no %s %s" % (op["id"], op["key"], op["value"])])
    lst.remove(op["value"])
    w.save(op["id"], rec)


def op_set_artifact(w, op):
    rec = w.load(op["id"])
    lst = op["list"]
    if lst not in ARTIFACT_LISTS:
        raise OpsError(["list is inputs or outputs"])
    art = dict(op["artifact"])
    _one_line(art, "artifact")
    cur = [a for a in rec.get(lst) or [] if a.get("id") != art.get("id")]
    rec[lst] = cur + [art]
    w.save(op["id"], rec)


def op_remove_artifact(w, op):
    rec = w.load(op["id"])
    lst = op["list"]
    cur = rec.get(lst) or []
    keep = [a for a in cur if a.get("id") != op["artifact"]]
    if len(keep) == len(cur):
        raise OpsError(["%s has no %s %s" % (op["id"], lst, op["artifact"])])
    rec[lst] = keep
    w.save(op["id"], rec)


def op_set_scenario(w, op):
    rec = w.load(op["id"])
    if rec["kind"] != "requirement":
        raise OpsError(["only a requirement has scenarios"])
    sc = _section_value(_section("requirement", "scenarios"), [{"name": op["name"], "steps": op["steps"]}])[0]
    cur = list(rec["sections"].get("scenarios") or [])
    for i, x in enumerate(cur):
        if x["name"] == op["name"]:
            cur[i] = sc
            break
    else:
        cur.append(sc)
    rec["sections"]["scenarios"] = cur
    w.save(op["id"], rec)


def op_remove_scenario(w, op):
    rec = w.load(op["id"])
    cur = rec["sections"].get("scenarios") or []
    keep = [x for x in cur if x["name"] != op["name"]]
    if len(keep) == len(cur):
        raise OpsError(["%s has no scenario %r" % (op["id"], op["name"])])
    rec["sections"]["scenarios"] = keep
    w.save(op["id"], rec)


def _subtree_rels(w, nid):
    if kind_of(nid) == "task":
        return [rel_of(nid)]
    return sorted(r for r in w.files if r.startswith(nid + "/"))


def _map_id(x, old, new):
    if x == old:
        return new
    if x.startswith(old + "/"):
        return new + x[len(old):]
    return x


def _rewrite_refs(w, old, new, skip=()):
    for rel in sorted(w.files):
        if rel in skip:
            continue
        nid = id_of(rel)
        k = kind_of(nid)
        try:
            rec = doc.parse(w.files[rel], k)
        except SpecError:
            continue
        changed = False
        for key in ("needs", "conflicts_with", "supersedes", "affects"):
            if rec.get(key):
                new_list = [_map_id(x, old, new) for x in rec[key]]
                if new_list != rec[key]:
                    rec[key], changed = new_list, True
        for a in rec.get("inputs") or []:
            if a.get("from") and _map_id(a["from"], old, new) != a["from"]:
                a["from"], changed = _map_id(a["from"], old, new), True
        if changed:
            w.files[rel] = doc.render(rec)


def op_mv(w, op):
    old, new = op["from"], op["to"]
    k = kind_of(old)
    if k in (None, "decision") or kind_of(new) != k:
        raise OpsError(["mv moves a node to an id of the same depth (%s is a %s, %s a %s)" % (
            old, k, new, kind_of(new))])
    if not w.has(old):
        raise OpsError(["no record %s" % old])
    if w.has(new) or any(r.startswith(new + "/") for r in w.files):
        raise OpsError(["%s exists" % new])
    if "/" in new and not w.has(new.rsplit("/", 1)[0]):
        raise OpsError(["the new parent %s does not exist" % new.rsplit("/", 1)[0]])
    rels = _subtree_rels(w, old)
    moved, texts = {}, {}
    for rel in rels:
        try:
            nid = id_of(rel)
            if rel_of(nid) != rel:
                raise OpsError([])
        except OpsError:
            raise OpsError(["%s is not a record of the spec tree: move or delete it first" % rel])
        moved[rel] = rel_of(_map_id(nid, old, new))
        if kind_of(nid) != "decision":
            rec = w.load(nid)
            # moving back to an old id: that id is no longer an alias (it is the node again)
            keep = [a for a in rec.get("aliases") or [] if a != new and not a.startswith(new + "/")]
            rec["aliases"] = sorted(set(keep + [nid]))
            texts[rel] = doc.render(rec)
    for rel, nrel in moved.items():
        text = w.files.pop(rel)
        w.files[nrel] = texts.get(rel, text)
        w.moves.append((rel, nrel))
    _rewrite_refs(w, old, new)


def op_rebase_basis(w, op):
    t = op["task"]
    if kind_of(t) != "task":
        raise OpsError(["%s is not a task" % t])
    rec = w.load(t)
    rec["basis"] = fingerprint(w.load(t.rsplit("/", 1)[0]))
    w.save(t, rec)


def op_supersede(w, op):
    if op["old"] == op["new"]:
        raise OpsError(["a requirement cannot supersede itself"])
    old, new = w.load(op["old"]), w.load(op["new"])
    if old["kind"] != "requirement" or new["kind"] != "requirement":
        raise OpsError(["supersede takes two requirements"])
    new["supersedes"] = sorted(set((new.get("supersedes") or []) + [op["old"]]))
    old["status"] = "deprecated"
    w.save(op["new"], new)
    w.save(op["old"], old)


def op_delete(w, op):
    nid = op["id"]
    if not w.has(nid):
        raise OpsError(["no record %s" % nid])
    rels = set(_subtree_rels(w, nid)) if kind_of(nid) != "decision" else {rel_of(nid)}
    if not op.get("force"):
        if len(rels) > 1:
            raise OpsError(["%s has children; delete them first, or use force (prefer status dropped or"
                            " deprecated)" % nid])
        users = []
        for rel, text in w.files.items():
            if rel in rels:
                continue
            try:
                rec = doc.parse(text, kind_of(id_of(rel)))
            except SpecError:
                continue
            refs = [x for k in ("needs", "conflicts_with", "supersedes", "affects") for x in rec.get(k) or []]
            refs += [a.get("from") for a in rec.get("inputs") or [] if a.get("from")]
            if any(r == nid or r.startswith(nid + "/") for r in refs):
                users.append(id_of(rel))
        if users:
            raise OpsError(["%s is referenced by %s; change them first (force deletes children, not references;"
                            " prefer status dropped or deprecated)" % (nid, ", ".join(users[:5]))])
    for rel in rels:
        del w.files[rel]


def apply_ops(files, ops, coordinator=False):
    """-> (changes, moves, problems after). Raises OpsError: nothing to write.
    `coordinator=True` is for internal callers only (migrate-plan): it allows coordinator statuses."""
    errs = []
    if not isinstance(ops, list):
        raise OpsError(["ops is a list of ops"])
    for i, op in enumerate(ops):
        for e in schemaload.errors(op, "spec-ops", "#/definitions/op"):
            errs.append("op %d: %s" % (i + 1, e))
    if errs:
        raise OpsError(errs)
    w = Work(files, coordinator)
    for i, op in enumerate(ops):
        try:
            globals()["op_" + op["op"]](w, op)  # looked up at call time
        except OpsError as e:
            raise OpsError(["op %d (%s): %s" % (i + 1, op["op"], m) for m in e.errors])
        except (SpecError, ValueError, TypeError, KeyError, AttributeError, IndexError) as e:
            msg = "; ".join(e.errors[:3]) if isinstance(e, SpecError) else "%s: %s" % (type(e).__name__, e)
            raise OpsError(["op %d (%s): %s" % (i + 1, op["op"], msg)])
    for nid in w.created:  # the basis of a new task is its requirement as the change leaves it
        if w.has(nid) and w.has(nid.rsplit("/", 1)[0]):
            rec = w.load(nid)
            if "basis" not in rec:
                rec["basis"] = fingerprint(w.load(nid.rsplit("/", 1)[0]))
                w.save(nid, rec)
    before_tree = Tree(files)
    before = {(p.rule, p.path, p.msg) for p in check(before_tree) if p.level == "error"}
    tree = Tree(w.files)
    problems = check(tree)
    new = [p for p in problems if p.level == "error" and (p.rule, p.path, p.msg) not in before]
    if not coordinator:
        new += plan_change_problems(before_tree, tree)
    if new:
        raise OpsError(["the change leaves the tree invalid: %s" % p for p in new])
    changes = {rel: text for rel, text in w.files.items() if files.get(rel) != text}
    for rel in files:
        if rel not in w.files:
            changes[rel] = None
    return changes, w.moves, problems
