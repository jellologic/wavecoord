"""The spec tree as the lead's plan store (docs/design/spec-layer.md, section 9.4).

`SpecTreePlan` has the interface of the flat stores in plan/adapters.py (get,
ready, graph, validate, specs), so dispatch, the dispatcher loop, plan_ready and
plan_get work on it unchanged. Each task of the tree is handed out in the shape
of task-spec.schema.json, with:

- `id`: the coordinator's task id, the tree id with "/" replaced by "."
  (`auth/sessions/token-rotation/rotate-impl` -> `auth.sessions.token-rotation.rotate-impl`;
  names have no dots, so the mapping goes both ways). Leases, attempts, branches
  and the PR's `Task:` line use it; `check` keeps it within 64 characters.
- `state`: the record's status in the flat store's words (planned -> todo, ...).
- `needs`: the expanded needs (section 5), as coordinator ids.
- `tree`: the tree id, the requirement (statement, scenarios, verify), the task's
  inputs and outputs and `basis`, for the worker prompt.

Readiness is the tree's (section 5): the coordinator's states (a live attempt,
a parked lease, a merge) override the status in the files, so a merged task
counts as done before its status commit lands (S4). A coordinator id reaches
its task through the task's aliases too, so a moved or migrated task keeps its
state. A task left in_progress or in_review in the files with nothing live is
planned again. A task is not ready while a task it conflicts with
(`conflicts_with`) runs, and `clash` tells dispatch_wave which ready tasks
cannot start together (predicate P).
"""

from wavecoord.plan import graph as g
from wavecoord.plan.adapters import PlanError

from .check import check
from .graph import Graph
from .tree import Tree

COORD_STATE = {"active": "in_progress", "review": "in_review", "done": "done"}
STRENGTH = {"review": 1, "active": 2, "done": 3}
FLAT_STATE = {"planned": "todo", "draft": "blocked", "on_hold": "blocked", "in_progress": "active",
              "in_review": "review", "done": "done", "dropped": "dropped"}


def coord_id(tree_id):
    return tree_id.replace("/", ".")


def tree_id(cid):
    return cid.replace(".", "/")


def task_of(tree, cid):
    """A coordinator task id -> the tree's task it names (directly or through an alias), or None."""
    for tid in (tree.resolve(tree_id(cid)), tree.aliases.get(cid)):  # an old tree id, or a flat plan's id
        n = tree.nodes.get(tid) if tid else None
        if n is not None and n.kind == "task":
            return tid
    return None


def task_states(tree, states):
    """{coordinator id: state} -> {tree task id: the strongest state of its id and its aliases}."""
    out = {}
    for cid, st in (states or {}).items():
        tid = task_of(tree, cid) if st in STRENGTH else None
        if tid and STRENGTH[st] > STRENGTH.get(out.get(tid), 0):
            out[tid] = st
    return out


class SpecTreePlan(object):
    kind = "specs"

    def __init__(self, files, label="specs", require_approved=True, suspect="warn"):
        self.files = files
        self.label = label
        self.require_approved = require_approved
        self.suspect = suspect
        self.tree = Tree(files)

    def _graph(self, states=None):
        live = task_states(self.tree, states)
        override = {t: COORD_STATE[st] for t, st in live.items()}
        for tid, n in self.tree.nodes.items():
            # in_progress or in_review in the files, nothing live: the attempt ended (the status
            # commit that says so may not have landed)
            if n.kind == "task" and tid not in live and (n.rec or {}).get("status") in ("in_progress", "in_review"):
                override[tid] = "planned"
        return Graph(self.tree, require_approved=self.require_approved, status_override=override)

    def shape(self, tid, gr=None):
        gr = gr or self._graph()
        n = self.tree.nodes[tid]
        rec = n.rec
        req_id = n.parent
        req = (self.tree.nodes[req_id].rec if req_id in self.tree.nodes else None) or {}
        sec = rec.get("sections") or {}
        spec = {"id": coord_id(tid), "title": rec.get("title"), "phase": req_id,
                "needs": sorted(coord_id(d) for d in gr.deps(tid)),
                "paths": list(rec.get("paths") or []), "class": rec.get("class"), "size": rec.get("size"),
                "state": FLAT_STATE.get(gr.status(tid), "blocked"), "exec": dict(rec.get("exec") or {}),
                "sections": {k: sec[k] for k in ("context", "risks", "acceptance", "prompt", "notes") if k in sec}}
        if rec.get("shared_paths"):
            spec["shared_paths"] = list(rec["shared_paths"])
        rsec = req.get("sections") or {}
        inputs = []
        for a in rec.get("inputs") or []:
            src = self.tree.resolve(a.get("from") or "")
            made = [p for p in gr.producers.get(a.get("id"), []) if src and (p == src or p.startswith(src + "/"))]
            inputs.append(dict(a, producers=made) if made else dict(a))
        spec["tree"] = {"id": tid, "requirement": req_id, "basis": rec.get("basis"),
                        "aliases": sorted(coord_id(x) for x in rec.get("aliases") or []),
                        "inputs": inputs, "outputs": rec.get("outputs") or [],
                        "statement": rsec.get("statement"), "scenarios": rsec.get("scenarios") or [],
                        "verify": rsec.get("verify") or [], "requirement_title": req.get("title")}
        return spec

    def specs(self):
        gr = self._graph()
        return [self.shape(t, gr) for t in gr.tasks]

    def load(self):
        return self.specs(), {n.path: e for n, e in ((self.tree.nodes.get(i) or self.tree.decisions.get(i), e)
                                                      for i, e in self.tree.parse_errors.items())}

    def get(self, cid):
        # the task the id names (directly, by an old tree id or by a flat id), before any non-task node that the
        # same text spells (a flat id `auth.sessions` and the feature auth/sessions)
        tid = task_of(self.tree, cid)
        n = self.tree.nodes.get(tid) if tid else (self.tree.nodes.get(tree_id(cid)) or
                                                   self.tree.nodes.get(self.tree.resolve(tree_id(cid)) or ""))
        if n is None or n.kind != "task":
            raise PlanError("no task %s in %s" % (cid, self.label))
        if n.rec is None:
            raise PlanError("task %s is invalid: %s" % (cid, "; ".join(self.tree.parse_errors.get(n.id, [])[:5])))
        return self.shape(n.id)

    def ids(self, spec):
        """The coordinator ids that name this task: its own and its aliases'."""
        return [spec["id"]] + [a for a in (spec.get("tree") or {}).get("aliases") or [] if a != spec["id"]]

    def ready(self, states, live):
        gr = self._graph(states)
        self._gr = gr
        r = gr.ready(self.suspect)
        tasks, why = [], {coord_id(t): w for t, w in r["why_not"].items()}
        running = [t for t, st in task_states(self.tree, states).items() if st in ("active", "review")]
        for t in r["tasks"]:
            req = self.tree.nodes.get(self.tree.requirement_of(t))
            if req is None or req.rec is None:
                why[coord_id(t)] = "requirement %s has no valid %s" % (self.tree.requirement_of(t), "_node.md")
                continue
            hit = next((u for u in running if u != t and gr._conflict(t, u)), None)
            if hit:
                why[coord_id(t)] = "conflicts with %s, which is %s" % (coord_id(hit), gr.status(hit))
                continue
            spec = self.shape(t, gr)
            block = g.reservation_blocks(spec, live)
            if block:
                why[spec["id"]] = block
                continue
            tasks.append(spec)
        return {"tasks": tasks, "why_not": why}

    def clash(self, a, b):
        """Why the ready tasks a and b (shaped specs) cannot start together (predicate P), or None."""
        gr = getattr(self, "_gr", None) or self._graph()
        ta, tb = (self.tree.resolve(tree_id(s["id"])) for s in (a, b))
        if ta in gr.recs and tb in gr.recs and not gr.parallel_ok(ta, tb):
            return "conflicts with %s (predicate P)" % b["id"] if gr._conflict(ta, tb) else \
                "may not run with %s (predicate P)" % b["id"]
        return None

    def graph(self, states):
        gr = self._graph(states)
        specs = [self.shape(t, gr) for t in gr.tasks]
        nodes, edges = g.spec_graph(specs, {coord_id(t): st for t, st in task_states(self.tree, states).items()})
        return g.analyze(nodes, edges)

    def validate(self, spec=None):
        if spec is not None:
            return {"ok": False, "errors": ["the spec tree validates the whole tree, not one spec"]}
        probs = check(self.tree)
        errs = ["%s: %s %s" % (p.path, p.rule, p.msg) for p in probs if p.level == "error"]
        return {"ok": not errs, "errors": errs, "tasks": len(self._graph().tasks)}
