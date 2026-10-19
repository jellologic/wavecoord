"""`wavecoord spec check` and `lint`: the rules of docs/design/spec-layer.md.

S1 checks each record on its own: the layout (section 2), the record frame,
keys, types and sections (section 3), the artifact keys (3.6), EARS for an
approved requirement (3.4) and the canonical form (rule M8). The rules across
records (references, alignment A1-A7, blockers, predicate P) come in S2.

    problems = check(tree)     # [Problem]; level "error" or "warning"
"""

import re

from wavecoord import schemaload

from . import doc
from .model import FRAME, KINDS, SCHEMA_VERSION

CONTRACT_KINDS = ("interface", "api", "schema", "event")
SENTENCE_RE = re.compile(r"[a-z0-9)\]\"'][.!?] +[A-Z]")
EARS_RE = re.compile(r"\bshall\b", re.I)


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

    rec = n.rec
    if rec is None:
        return out
    kind = KINDS[n.kind]
    if rec.get("kind") != n.kind:
        err("kind", "the path makes this a %s, but kind is %r" % (n.kind, rec.get("kind")))
        return out
    if rec.get("schema") != SCHEMA_VERSION:
        err("schema", "schema %r is not supported (this wavecoord reads schema %d)" % (rec.get("schema"),
                                                                                         SCHEMA_VERSION))
    for e in schemaload.errors(rec, "spec-record", "#/definitions/" + n.kind):
        err("schema", e)
    if n.kind == "task" and isinstance(rec.get("exec"), dict):
        for e in schemaload.errors(rec["exec"], "task-spec", "#/definitions/exec"):
            err("schema", "exec: " + e)
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


def check(tree):
    out = []
    for path, msg in tree.layout_errors:
        out.append(Problem("error", "layout", None, path, msg))
    for nid, errs in sorted(tree.parse_errors.items()):
        node = tree.nodes.get(nid) or tree.decisions.get(nid)
        for e in errs:
            out.append(Problem("error", "parse", nid, node.path, e))
    for n in list(tree.nodes.values()) + list(tree.decisions.values()):
        out.extend(check_record(n))
    return sorted(out, key=lambda p: (p.path or "", p.rule, p.msg))


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
            fence = False
            for line in v.split("\n"):
                if line.lstrip().startswith("```"):
                    fence = not fence
                    continue
                if fence or line.lstrip().startswith(("-", "*", "|", "#", ">")) or line.startswith("    "):
                    if line.lstrip().startswith("|") and s.key != "design" and not fence:
                        out.append(Problem("warning", "table", n.id, n.path,
                                           "%s: a table outside Design (rule M7)" % s.title))
                    continue
                if SENTENCE_RE.search(line):
                    out.append(Problem("warning", "sentences", n.id, n.path,
                                       "%s: one sentence per line (rule M6): %r" % (s.title, line[:80])))
    return out
