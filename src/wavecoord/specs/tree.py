"""The spec tree: one pass over the files of the spec directory (docs/design/spec-layer.md, section 2).

    t = Tree(files)            # files: {path relative to spec.dir: text} (source.py)
    t.nodes["auth/sessions"]   # -> Node(id, kind, path, rec, text, parent)
    t.children("auth")         # ids of the direct children, sorted
    t.layout_errors            # [(path, message)]: files outside the layout

The path is the ID and the folder is the parent: the parent is never read from
a field. A file that does not parse keeps `rec = None` and its error in
`parse_errors`; the rest of the tree is still built.
"""

import datetime

from wavecoord.plan.spec import SpecError

from . import doc
from .model import DATE_SLUG_RE, DECISIONS_DIR, DEPTH_KIND, NAME_RE, NODE_FILE, RESERVED, TASKS_DIR


class Node(object):
    __slots__ = ("id", "kind", "path", "rec", "text", "parent", "depth")

    def __init__(self, nid, kind, path, text, parent, depth):
        self.id, self.kind, self.path, self.text, self.parent, self.depth = nid, kind, path, text, parent, depth
        self.rec = None

    @property
    def status(self):
        return (self.rec or {}).get("status")

    @property
    def title(self):
        return (self.rec or {}).get("title")


def _names_ok(names):
    return all(NAME_RE.match(n) and n not in RESERVED for n in names)


def classify(rel):
    """A path relative to spec.dir -> (kind, id, parent id, depth) or raises ValueError(message)."""
    parts = rel.split("/")
    if not rel.endswith(".md"):
        raise ValueError("only .md records belong in the spec tree")
    if parts[-1] == NODE_FILE:
        dirs = parts[:-1]
        if not 1 <= len(dirs) <= 3:
            raise ValueError("a %s is at depth 1 to 3 (capability, feature, requirement)" % NODE_FILE)
        if not _names_ok(dirs):
            raise ValueError("names are lowercase letters, digits and '-' (and not %s)" % ", ".join(RESERVED))
        return DEPTH_KIND[len(dirs)], "/".join(dirs), "/".join(dirs[:-1]) or None, len(dirs)
    if len(parts) >= 3 and parts[-2] == DECISIONS_DIR:
        dirs = parts[:-2]
        name = parts[-1][:-3]
        m = DATE_SLUG_RE.match(name)
        if not 1 <= len(dirs) <= 3 or not _names_ok(dirs):
            raise ValueError("decisions/ belongs to a capability, feature or requirement folder")
        if not m:
            raise ValueError("a decision is named <YYYY-MM-DD>-<slug>.md")
        try:
            datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            raise ValueError("the date %s-%s-%s is not a valid date" % m.group(1, 2, 3))
        node = "/".join(dirs)
        return "decision", "%s/%s/%s" % (node, DECISIONS_DIR, name), node, None
    if len(parts) == 5 and parts[3] == TASKS_DIR:
        dirs, name = parts[:3], parts[4][:-3]
        if not _names_ok(dirs + [name]):
            raise ValueError("names are lowercase letters, digits and '-' (and not %s)" % ", ".join(RESERVED))
        return "task", "/".join(dirs + [name]), "/".join(dirs), 4
    if TASKS_DIR in parts[:-1]:
        raise ValueError("tasks/ belongs to a requirement folder (depth 3)")
    raise ValueError("not a record of the spec tree (%s, tasks/<task>.md or decisions/<date>-<slug>.md)" % NODE_FILE)


class Tree(object):
    def __init__(self, files):
        self.files = dict(files)
        self.nodes = {}  # node id -> Node (capability, feature, requirement, task)
        self.decisions = {}  # decision id -> Node
        self.layout_errors = []
        self.parse_errors = {}  # node or decision id -> [messages]
        self._children = {}
        for rel in sorted(files):
            try:
                kind, nid, parent, depth = classify(rel)
            except ValueError as e:
                self.layout_errors.append((rel, str(e)))
                continue
            n = Node(nid, kind, rel, files[rel], parent, depth)
            try:
                n.rec = doc.parse(files[rel], kind)
            except SpecError as e:
                self.parse_errors[nid] = e.errors
            if kind == "decision":
                self.decisions[nid] = n
            else:
                self.nodes[nid] = n
                if parent:
                    self._children.setdefault(parent, []).append(nid)
        for nid, n in sorted(self.nodes.items()):
            if n.parent and n.parent not in self.nodes:
                self.layout_errors.append((n.path, "the parent %s has no %s" % (n.parent, NODE_FILE)))
        for did, d in sorted(self.decisions.items()):
            if d.parent not in self.nodes:
                self.layout_errors.append((d.path, "the node %s has no %s" % (d.parent, NODE_FILE)))

    def children(self, nid):
        return sorted(self._children.get(nid, []))

    def roots(self):
        return sorted(n for n, node in self.nodes.items() if node.parent is None)

    def subtree(self, nid):
        """Ids of nid and every node below it, depth first."""
        out, stack = [], [nid]
        while stack:
            cur = stack.pop()
            out.append(cur)
            stack.extend(reversed(self.children(cur)))
        return out

    def tasks_under(self, nid):
        return [x for x in self.subtree(nid) if self.nodes[x].kind == "task"]

    def resolve(self, nid):
        """A node id, or an alias of one -> the node id, or None."""
        if nid in self.nodes:
            return nid
        for n in self.nodes.values():
            if nid in ((n.rec or {}).get("aliases") or []):
                return n.id
        return None
