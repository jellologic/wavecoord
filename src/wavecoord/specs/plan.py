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
counts as done before its status commit lands (S4).
"""

from wavecoord.plan import graph as g
from wavecoord.plan.adapters import PlanError

from .check import check
from .graph import Graph
from .tree import Tree

COORD_STATE = {"active": "in_progress", "review": "in_review", "done": "done"}
FLAT_STATE = {"planned": "todo", "draft": "blocked", "on_hold": "blocked", "in_progress": "active",
              "in_review": "review", "done": "done", "dropped": "dropped"}


def coord_id(tree_id):
    return tree_id.replace("/", ".")


def tree_id(cid):
    return cid.replace(".", "/")


class SpecTreePlan(object):
    kind = "specs"

    def __init__(self, files, label="specs", require_approved=True, suspect="warn"):
        self.files = files
        self.label = label
        self.require_approved = require_approved
        self.suspect = suspect
        self.tree = Tree(files)

    def _graph(self, states=None):
        override = {}
        for cid, st in (states or {}).items():
            if st in COORD_STATE and tree_id(cid) in self.tree.nodes:
                override[tree_id(cid)] = COORD_STATE[st]
        return Graph(self.tree, require_approved=self.require_approved, status_override=override)

    def shape(self, tid, gr=None):
        gr = gr or self._graph()
        n = self.tree.nodes[tid]
        rec = n.rec
        req_id = n.parent
        req = self.tree.nodes[req_id].rec or {}
        sec = rec.get("sections") or {}
        spec = {"id": coord_id(tid), "title": rec.get("title"), "phase": req_id,
                "needs": sorted(coord_id(d) for d in gr.deps(tid)),
                "paths": list(rec.get("paths") or []), "class": rec.get("class"), "size": rec.get("size"),
                "state": FLAT_STATE.get(gr.status(tid), "blocked"), "exec": dict(rec.get("exec") or {}),
                "sections": {k: sec[k] for k in ("context", "risks", "acceptance", "prompt", "notes") if k in sec}}
        if rec.get("shared_paths"):
            spec["shared_paths"] = list(rec["shared_paths"])
        rsec = req.get("sections") or {}
        spec["tree"] = {"id": tid, "requirement": req_id, "basis": rec.get("basis"),
                        "inputs": rec.get("inputs") or [], "outputs": rec.get("outputs") or [],
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
        tid = tree_id(cid)
        n = self.tree.nodes.get(tid) or self.tree.nodes.get(self.tree.resolve(tid) or "")
        if n is None or n.kind != "task":
            raise PlanError("no task %s in %s" % (cid, self.label))
        if n.rec is None:
            raise PlanError("task %s is invalid: %s" % (cid, "; ".join(self.tree.parse_errors.get(n.id, [])[:5])))
        return self.shape(n.id)

    def ready(self, states, live):
        gr = self._graph(states)
        r = gr.ready(self.suspect)
        tasks, why = [], {coord_id(t): w for t, w in r["why_not"].items()}
        for t in r["tasks"]:
            spec = self.shape(t, gr)
            block = g.reservation_blocks(spec, live)
            if block:
                why[spec["id"]] = block
                continue
            tasks.append(spec)
        return {"tasks": tasks, "why_not": why}

    def graph(self, states):
        gr = self._graph(states)
        specs = [self.shape(t, gr) for t in gr.tasks]
        nodes, edges = g.spec_graph(specs, states)
        return g.analyze(nodes, edges)

    def validate(self, spec=None):
        if spec is not None:
            return {"ok": False, "errors": ["the spec tree validates the whole tree, not one spec"]}
        probs = check(self.tree)
        errs = ["%s: %s %s" % (p.path, p.rule, p.msg) for p in probs if p.level == "error"]
        return {"ok": not errs, "errors": errs, "tasks": len(self._graph().tasks)}
