"""The rules across records (docs/design/spec-layer.md, sections 4 to 6) and the reads built on them.

    g = Graph(tree)
    g.problems             # references, blockers, alignment A1-A7, predicate P, suspect tasks
    g.deps(task)           # the tasks it waits for: its needs and its ancestors' needs, expanded
    g.waves()              # {"waves": [[task]], "critical_path": [task], "width": n}
    g.ready()              # {"tasks": [task], "why_not": {task: reason}}
    g.trace(node)          # ancestors, children, needs, needed by, artifacts, decisions

Only records that parse take part. A task is closed when its status is done or
dropped; waves and readiness are over the open tasks, and a closed need counts
as met only when it is done.
"""

from wavecoord.globs import covers, globs_overlap, literal_prefix

from .check import Problem
from .fingerprint import fingerprint
from .model import CLOSED, DONE

REF_KEYS = ("needs", "conflicts_with", "supersedes")


class Graph(object):
    def __init__(self, tree, require_approved=True, status_override=None):
        self.t = tree
        self.require_approved = require_approved
        self.override = dict(status_override or {})  # node -> status the coordinator knows better
        self.problems = []
        self.recs = {nid: n.rec for nid, n in tree.nodes.items() if n.rec is not None}
        self.tasks = sorted(nid for nid in self.recs if tree.nodes[nid].kind == "task")
        self._refs()
        self._task_deps()
        self._artifacts()
        self._alignment()
        self._parallel()
        self._suspect()

    # ---------------------------------------------------------------- helpers

    def _p(self, level, rule, nid, msg):
        n = self.t.nodes.get(nid) or self.t.decisions.get(nid)
        self.problems.append(Problem(level, rule, nid, n.path if n else None, msg))

    def status(self, nid):
        return self.override.get(nid) or (self.recs.get(nid) or {}).get("status")

    def expand(self, nid):
        """A node -> the tasks it stands for in `needs`: itself, or every task in its subtree."""
        return [x for x in self.t.subtree(nid) if self.t.nodes[x].kind == "task" and x in self.recs]

    # ---------------------------------------------------------------- references and blockers

    def _refs(self):
        self.needs = {}  # node -> resolved direct needs
        self.conflicts = {}
        for path, msg in self.t.alias_errors:
            self.problems.append(Problem("error", "alias", None, path, msg))
        for nid, rec in sorted(self.recs.items()):
            anc = set(self.t.ancestors(nid))
            for key in REF_KEYS:
                out = []
                for ref in rec.get(key) or []:
                    rid = self.t.resolve(ref)
                    if rid is None:
                        self._p("error", "ref", nid, "%s: %s does not exist" % (key, ref))
                        continue
                    if rid != ref:
                        self._p("warning", "alias", nid, "%s: %s is an old id of %s; use the new id" % (key, ref, rid))
                    if key == "needs" and (rid == nid or rid in anc or nid in set(self.t.ancestors(rid))):
                        self._p("error", "needs", nid, "needs %s: a node cannot need itself, an ancestor or a"
                                                       " descendant" % ref)
                        continue
                    if key == "supersedes" and self.t.nodes[rid].kind != "requirement":
                        self._p("error", "ref", nid, "supersedes %s: not a requirement" % ref)
                        continue
                    out.append(rid)
                if key == "needs":
                    self.needs[nid] = out
                elif key == "conflicts_with":
                    self.conflicts[nid] = out
            for a in rec.get("inputs") or []:
                if a.get("from") and self.t.resolve(a["from"]) is None:
                    self._p("error", "ref", nid, "input %s: from %s does not exist" % (a.get("id"), a["from"]))
        for did, d in sorted(self.t.decisions.items()):
            for ref in (d.rec or {}).get("affects") or []:
                if self.t.resolve(ref) is None:
                    self._p("error", "ref", did, "affects %s does not exist" % ref)
            for ref in (d.rec or {}).get("supersedes") or []:
                if ref not in self.t.decisions:
                    self._p("error", "ref", did, "supersedes %s: no such decision" % ref)

    def node_deps(self, nid):
        """The tasks that a node waits for: the expansion of its needs and of its ancestors' needs."""
        out = set()
        for x in [nid] + self.t.ancestors(nid):
            for r in self.needs.get(x, []):
                out.update(self.expand(r))
        return out

    def _task_deps(self):
        self._deps = {t: self.node_deps(t) for t in self.tasks}
        # cycles, on the expanded task graph
        color, stack_path, self.cycle = {}, [], None

        def visit(t):
            color[t] = 1
            stack_path.append(t)
            for d in sorted(self._deps.get(t, ())):
                if color.get(d) == 1 and self.cycle is None:
                    self.cycle = stack_path[stack_path.index(d):] + [d]
                elif d not in color:
                    visit(d)
            stack_path.pop()
            color[t] = 2

        for t in self.tasks:
            if t not in color:
                visit(t)
        if self.cycle:
            self._p("error", "cycle", self.cycle[0], "needs form a cycle: %s" % " -> ".join(self.cycle))
        self._reach = {}

    def deps(self, task):
        return set(self._deps.get(task, ()))

    def reach(self, task):
        """Every task that `task` waits for, directly or not."""
        if task in self._reach:
            return self._reach[task]
        out, stack = set(), list(self._deps.get(task, ()))
        while stack:
            x = stack.pop()
            if x in out or x == task:
                continue
            out.add(x)
            stack.extend(self._deps.get(x, ()))
        self._reach[task] = out
        return out

    # ---------------------------------------------------------------- artifacts and alignment

    def _artifacts(self):
        self.producers = {}  # artifact id -> [task]
        self.consumers = {}  # artifact id -> [node]
        for t in self.tasks:
            for a in self.recs[t].get("outputs") or []:
                self.producers.setdefault(a["id"], []).append(t)
        for nid, rec in self.recs.items():
            for a in rec.get("inputs") or []:
                self.consumers.setdefault(a["id"], []).append(nid)

    def _artifact(self, nid, lst, aid):
        for a in self.recs.get(nid, {}).get(lst) or []:
            if a.get("id") == aid:
                return a
        return None

    def _alignment(self):
        t = self.t
        for aid, prods in sorted(self.producers.items()):  # A2
            if len(prods) > 1:
                for p in prods:
                    self._p("error", "A2", p, "output %s is also produced by %s" % (
                        aid, ", ".join(x for x in prods if x != p)))
        for nid, rec in sorted(self.recs.items()):
            node = t.nodes[nid]
            sub = set(self.expand(nid)) if node.kind != "task" else {nid}
            for a in rec.get("outputs") or []:
                if node.kind != "task":  # A1
                    under = [p for p in self.producers.get(a["id"], []) if p in sub]
                    if len(under) != 1:
                        self._p("error", "A1", nid, "output %s is produced by %d tasks under it (exactly 1 needed)"
                                % (a["id"], len(under)))
                    else:
                        pa = self._artifact(under[0], "outputs", a["id"])
                        if pa.get("kind") != a.get("kind") or (a.get("contract") and pa.get("contract")
                                                              != a.get("contract")):
                            self._p("error", "A1", nid, "output %s: %s produces it with another kind or contract"
                                    % (a["id"], under[0]))
                elif a.get("kind") in ("file", "files"):  # A5
                    paths = rec.get("paths") or []
                    if not any(covers(g, a["id"]) or g == a["id"] for g in paths):
                        self._p("error", "A5", nid, "file output %s is outside the task's paths" % a["id"])
                if not self._output_used(nid, a):  # A7
                    self._p("warning", "A7", nid, "output %s is not an output of the parent and no node consumes"
                                                  " it" % a["id"])
            deps = self.node_deps(nid) if node.kind != "task" else self.deps(nid)
            for a in rec.get("inputs") or []:  # A3, A4
                if a.get("external") or not a.get("from"):
                    continue
                src = t.resolve(a["from"])
                if src is None:
                    continue
                made = [p for p in self.producers.get(a["id"], []) if p in set(self.expand(src))]
                if not made:
                    self._p("error", "A3", nid, "input %s: no task under %s produces it" % (a["id"], a["from"]))
                    continue
                missing = [p for p in made if p not in sub and p not in deps]
                if missing:
                    self._p("error", "A4", nid, "input %s comes from %s, but needs does not reach %s" % (
                        a["id"], a["from"], ", ".join(missing)))
            if node.kind == "task":  # A6
                outs = [a["id"] for a in rec.get("outputs") or [] if a.get("kind") in ("file", "files")]
                for g in rec.get("paths") or []:
                    if not any(covers(g, o) or g == o or globs_overlap(g, o) for o in outs):
                        self._p("warning", "A6", nid, "paths %s: no output says what the task hands over there" % g)
            for r in self.needs.get(nid, []):  # needs that no input justifies
                if not self._consumes_from(nid, r):
                    self._p("warning", "needs", nid, "needs %s, but no input comes from it (keep it if the order"
                                                     " is real)" % r)

    def _consumes_from(self, nid, r):
        """An input of nid comes from r, from a node under r, or from a node that r is under."""
        for a in self.recs[nid].get("inputs") or []:
            src = self.t.resolve(a.get("from") or "")
            if src and (src == r or r in self.t.ancestors(src) or src in self.t.ancestors(r)):
                return True
        return False

    def _output_used(self, nid, a):
        node = self.t.nodes[nid]
        if node.kind == "task" and a.get("kind") in ("file", "files"):
            return True
        if node.parent and self._artifact(node.parent, "outputs", a["id"]):
            return True
        return bool(self.consumers.get(a["id"]))

    # ---------------------------------------------------------------- parallelism (section 6)

    def open_tasks(self):
        return [t for t in self.tasks if self.status(t) not in CLOSED]

    def _conflict(self, a, b):
        for x, y in ((a, b), (b, a)):
            for c in self.conflicts.get(x, []) + [c for anc in self.t.ancestors(x) for c in self.conflicts.get(anc, [])]:
                if y == c or c in self.t.ancestors(y):
                    return True
        return False

    def _paths_clash(self, a, b):
        ra, rb = self.recs[a], self.recs[b]
        ea, eb = ra.get("paths") or [], rb.get("paths") or []
        sa, sb = ra.get("shared_paths") or [], rb.get("shared_paths") or []
        for x in ea:
            for y in eb + sb:
                if globs_overlap(x, y):
                    return "%s / %s" % (x, y)
        for x in sa:
            for y in eb:
                if globs_overlap(x, y):
                    return "%s / %s" % (x, y)
        return None

    def parallel_ok(self, a, b):
        """Predicate P."""
        if b in self.reach(a) or a in self.reach(b):
            return False
        if self._paths_clash(a, b):
            return False
        oa = {x["id"] for x in self.recs[a].get("outputs") or []}
        if oa & {x["id"] for x in self.recs[b].get("outputs") or []}:
            return False
        return not self._conflict(a, b)

    def _candidates(self, tasks):
        """Pairs of tasks whose paths may overlap: tasks that share the first literal segment of a glob,
        and every pair with a task whose glob starts with a wildcard."""
        buckets, wild = {}, set()
        for t in tasks:
            for g in (self.recs[t].get("paths") or []) + (self.recs[t].get("shared_paths") or []):
                pre, wild_card = literal_prefix(g)
                # the first segment is fixed only when the wildcard comes after its "/" (`s*` overlaps `src/x`)
                seg = pre.split("/", 1)[0] if ("/" in pre or not wild_card) else ""
                if seg:
                    buckets.setdefault(seg, set()).add(t)
                else:
                    wild.add(t)
        pairs = set()
        for group in buckets.values():
            g = sorted(group)
            pairs.update((a, b) for i, a in enumerate(g) for b in g[i + 1:])
        for w in wild:
            pairs.update((min(w, t), max(w, t)) for t in tasks if t != w)
        return sorted(pairs)

    def _parallel(self):
        for a, b in self._candidates(self.open_tasks()):
            if b in self.reach(a) or a in self.reach(b) or self._conflict(a, b):
                continue
            clash = self._paths_clash(a, b)
            if clash:
                self._p("error", "P", a, "%s and %s may run at the same time but both write %s: add a needs or split"
                        " the paths" % (a, b, clash))

    def waves(self, root=None):
        tasks = [t for t in self.open_tasks() if root is None or t == root or t.startswith(root + "/")]
        todo = set(tasks)
        done = {t for t in self.tasks if self.status(t) in DONE}
        waves, placed = [], set()
        while todo:
            avail = sorted(t for t in todo if all(d in done or d in placed for d in self.reach(t) if d in self.recs))
            if not avail:
                break  # a cycle, or a need that is dropped or outside root and not done
            wave = []
            for t in avail:
                if all(self.parallel_ok(t, w) for w in wave):
                    wave.append(t)
            waves.append(wave)
            placed.update(wave)
            todo -= set(wave)
        longest = {}
        for wave in waves:
            for t in wave:
                prev = [longest[d] for d in self.deps(t) if d in longest]
                best = max(prev, key=len) if prev else []
                longest[t] = best + [t]
        crit = max(longest.values(), key=len) if longest else []
        return {"waves": waves, "critical_path": crit, "width": max((len(w) for w in waves), default=0),
                "unplaced": sorted(todo)}

    # ---------------------------------------------------------------- ready, suspect, trace

    def ready(self, suspect="warn"):
        """Ready tasks; with suspect "refuse", a task whose requirement changed since its basis is not ready."""
        tasks, why = [], {}
        for t in self.open_tasks():
            st = self.status(t)
            if st != "planned":
                why[t] = "status %s" % st
                continue
            unmet = sorted(d for d in self.deps(t) if self.status(d) not in DONE)
            if unmet:
                why[t] = "needs not done: %s" % ", ".join(unmet[:5])
                continue
            if self.require_approved:
                req = self.t.requirement_of(t)
                if self.status(req) != "approved":
                    why[t] = "requirement %s is %s" % (req, self.status(req))
                    continue
                inactive = [a for a in self.t.ancestors(req) if self.status(a) != "active"]
                if inactive:
                    why[t] = "%s is %s" % (inactive[0], self.status(inactive[0]))
                    continue
            if suspect == "refuse" and self.is_suspect(t):
                why[t] = "suspect: the requirement changed since basis"
                continue
            tasks.append(t)
        return {"tasks": tasks, "why_not": why}

    def is_suspect(self, task):
        rec = self.recs.get(task) or {}
        req = self.recs.get(self.t.requirement_of(task))
        if req is None or not rec.get("basis"):
            return False
        return rec["basis"] != fingerprint(req)

    def _suspect(self):
        for t in self.tasks:
            if self.status(t) in CLOSED:
                continue
            req = self.recs.get(self.t.requirement_of(t))
            if req is None:
                continue
            if not self.recs[t].get("basis"):
                self._p("warning", "suspect", t, "no basis: run `wavecoord spec rebase-basis %s`" % t)
            elif self.is_suspect(t):
                self._p("warning", "suspect", t, "the requirement changed since basis %s (now %s): review the task,"
                                                 " then `wavecoord spec rebase-basis %s`" % (
                                                     self.recs[t]["basis"], fingerprint(req), t))

    def suspects(self):
        out = []
        for t in self.open_tasks():
            if self.is_suspect(t):
                req = self.t.requirement_of(t)
                out.append({"task": t, "requirement": req, "basis": self.recs[t]["basis"],
                            "current": fingerprint(self.recs[req])})
        return out

    def trace(self, nid):
        t = self.t
        rid = t.resolve(nid)
        if rid is None:
            return None
        rec = self.recs.get(rid) or {}
        sub = set(t.subtree(rid))
        produced = sorted({a["id"] for x in sub for a in (self.recs.get(x) or {}).get("outputs") or []})
        consumed = sorted({a["id"] for a in rec.get("inputs") or []})
        needed_by = sorted(x for x, rs in self.needs.items() if rid in rs)
        return {
            "id": rid, "kind": t.nodes[rid].kind, "status": self.status(rid), "ancestors": t.ancestors(rid),
            "children": t.children(rid), "needs": self.needs.get(rid, []),
            "waits_for": sorted(self.node_deps(rid) if t.nodes[rid].kind != "task" else self.deps(rid)),
            "needed_by": needed_by,
            "outputs": [{"id": a, "producer": self.producers.get(a, []), "consumers": self.consumers.get(a, [])}
                        for a in produced if a in {o["id"] for o in rec.get("outputs") or []} or
                        t.nodes[rid].kind == "task"],
            "inputs": [{"id": a, "producer": self.producers.get(a, [])} for a in consumed],
            "decisions": sorted(d for d, n in t.decisions.items()
                                if any(t.resolve(r) in ([rid] + t.ancestors(rid)) for r in (n.rec or {}).get(
                                    "affects") or [])),
        }

    def artifacts(self):
        ids = sorted(set(self.producers) | set(self.consumers))
        return [{"id": a, "producers": self.producers.get(a, []), "consumers": self.consumers.get(a, [])} for a in ids]
