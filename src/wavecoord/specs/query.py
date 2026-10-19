"""Reads of the spec tree: the tree view, one record, and the query language.

Query terms (docs/design/spec-layer.md, section 9.2), separated by spaces, all
of which must hold:

    key=value    key!=value    key~glob    key    !key    under=<node id>

Keys are the frontmatter keys, plus id, path, depth and parent. For a list
the term holds when any item holds; for inputs and outputs the items are the
artifact ids. `~` is an fnmatch glob.

`status` (node -> status), where given, is the status the coordinator knows
better than the files (a running or merged task before its status commit
lands); the lead passes it, the CLI reads the files alone.
"""

import fnmatch
import shlex

from .fingerprint import fingerprint
from .model import CLOSED, DONE


class QueryError(ValueError):
    pass


def _values(n, key, status=None):
    rec = n.rec or {}
    if key == "status" and status and n.id in status:
        return [status[n.id]]
    if key == "id":
        return [n.id]
    if key == "path":
        return [n.path]
    if key == "depth":
        return [str(n.depth)] if n.depth else []
    if key == "parent":
        return [n.parent] if n.parent else []
    v = rec.get(key)
    if v is None:
        return []
    if isinstance(v, list):
        return [str(a.get("id")) if isinstance(a, dict) else str(a) for a in v]
    if isinstance(v, bool):
        return ["true" if v else "false"]
    return [str(v)]


def parse_query(expr):
    terms = []
    for t in shlex.split(expr or ""):
        if t.startswith("!"):
            if not t[1:]:
                raise QueryError("empty term %r" % t)
            terms.append(("absent", t[1:], None))
        elif "!=" in t:
            k, v = t.split("!=", 1)
            terms.append(("ne", k, v))
        elif "=" in t:
            k, v = t.split("=", 1)
            terms.append(("under" if k == "under" else "eq", k, v))
        elif "~" in t:
            k, v = t.split("~", 1)
            terms.append(("glob", k, v))
        else:
            terms.append(("present", t, None))
    for op, k, _v in terms:
        if not k:
            raise QueryError("a term needs a key")
    return terms


def match(n, terms, status=None):
    for op, k, v in terms:
        if op == "under":
            if not (n.id == v or n.id.startswith(v + "/")):
                return False
            continue
        vals = _values(n, k, status)
        if op == "eq" and v not in vals:
            return False
        if op == "ne" and v in vals:
            return False
        if op == "glob" and not any(fnmatch.fnmatchcase(x, v) for x in vals):
            return False
        if op == "present" and not vals:
            return False
        if op == "absent" and vals:
            return False
    return True


def query(tree, expr, decisions=True, status=None):
    terms = parse_query(expr)
    nodes = list(tree.nodes.values()) + (list(tree.decisions.values()) if decisions else [])
    return sorted((n for n in nodes if match(n, terms, status)), key=lambda n: n.id)


def summary(n, status=None):
    out = {"id": n.id, "kind": n.kind, "path": n.path, "status": (status or {}).get(n.id, n.status), "title": n.title}
    if n.kind == "requirement" and n.rec is not None:
        out["fingerprint"] = fingerprint(n.rec)
    return out


def record(tree, nid):
    n = tree.nodes.get(nid) or tree.decisions.get(nid)
    if n is None:
        rid = tree.resolve(nid)
        n = tree.nodes.get(rid) if rid else None
    if n is None:
        return None
    out = summary(n)
    out.update(parent=n.parent, children=tree.children(n.id) if n.kind != "decision" else [], record=n.rec,
               errors=tree.parse_errors.get(n.id, []))
    return out


def rollup(tree, nid, status=None):
    """{done, open, dropped, total}: the tasks under a node, from the status in the files (or `status`)."""
    st = [(status or {}).get(t, tree.nodes[t].status) for t in tree.tasks_under(nid)]
    done = sum(1 for x in st if x in DONE)
    dropped = sum(1 for x in st if x == "dropped")
    return {"done": done, "dropped": dropped, "open": len([x for x in st if x not in CLOSED]), "total": len(st)}


def tree_view(tree, root=None, status=None):
    """Nested dicts for --json, and lines for a terminal."""
    def build(nid):
        n = tree.nodes[nid]
        d = summary(n, status)
        if n.kind != "task":
            d["progress"] = rollup(tree, nid, status)
            d["children"] = [build(c) for c in tree.children(nid)]
        return d

    roots = [root] if root else tree.roots()
    data = [build(r) for r in roots if r in tree.nodes]
    lines = []

    def walk(d, depth):
        prog = ""
        if "progress" in d:
            p = d["progress"]
            prog = "  %d/%d done" % (p["done"], p["total"] - p["dropped"]) if p["total"] else ""
        name = d["id"].rsplit("/", 1)[-1]
        lines.append("%s%-*s %-11s %s%s" % ("  " * depth, max(1, 36 - 2 * depth), name, d["status"] or "?",
                                             d["title"] or "", prog))
        for c in d.get("children", []):
            walk(c, depth + 1)

    for d in data:
        walk(d, 0)
    return data, lines
