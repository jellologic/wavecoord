"""Plan adapters (docs/adapters.md): the built-in Markdown store and the command adapter.

Both give the same shapes:

    get(id)                  -> task spec (task-spec.schema.json)
    ready(states, live)      -> {"tasks": [spec], "why_not": {id: reason}}
    graph(states)            -> graph.analyze(...) output
    validate(spec=None)      -> {"ok": bool, "errors": [str]}

`states` maps task id -> effective state (from the coordinator), `live` is the
list of live reservations. The adapter never reads coordinator state itself.
"""

import json
import os
import subprocess

from wavecoord import schemaload

from . import graph as g
from .spec import SpecError, load_text, schema_errors

DEFAULT_DIR = "plan/tasks"
CMD_TIMEOUT_S = 120


class PlanError(Exception):
    """A plan problem the lead must see: a missing task, invalid adapter output, a bad command."""


# ----- sources of spec files ----------------------------------------------------------------------

class DirSource(object):
    """Spec files in a directory of a working tree."""

    def __init__(self, path):
        self.path = path
        self.label = path

    def names(self):
        try:
            return sorted(n for n in os.listdir(self.path) if n.endswith(".md") and not n.startswith("."))
        except OSError:
            return []

    def read(self, name):
        try:
            with open(os.path.join(self.path, name), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None


class GitSource(object):
    """Spec files in `<ref>:<dir>` of a repo (the default branch as fetched)."""

    def __init__(self, repo, ref, plan_dir):
        self.repo, self.ref, self.dir = repo, ref, plan_dir.strip("/")
        self.label = "%s:%s" % (ref, self.dir)

    def _git(self, args):
        p = subprocess.run(["git"] + args, cwd=self.repo, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=120)
        return p.returncode, p.stdout.decode("utf-8", "replace")

    def names(self):
        rc, out = self._git(["ls-tree", "--name-only", "%s:%s" % (self.ref, self.dir)])
        if rc != 0:
            return []
        return sorted(n for n in out.splitlines() if n.endswith(".md") and not n.startswith("."))

    def read(self, name):
        rc, out = self._git(["show", "%s:%s/%s" % (self.ref, self.dir, name)])
        return out if rc == 0 else None


# ----- the built-in store -------------------------------------------------------------------------

class BuiltinPlan(object):
    kind = "builtin"

    def __init__(self, source):
        self.source = source
        self._specs, self._errors = None, None

    def load(self):
        """-> (valid specs in plan order, {file: [errors]})."""
        if self._specs is None:
            specs, errors = [], {}
            for name in self.source.names():
                text = self.source.read(name)
                if text is None:
                    errors[name] = ["cannot read"]
                    continue
                try:
                    specs.append(load_text(text, name=None, expect_id=name[:-3]))
                except SpecError as e:
                    errors[name] = e.errors
            self._specs, self._errors = specs, errors
        return self._specs, self._errors

    def specs(self):
        return self.load()[0]

    def get(self, task_id):
        for s in self.specs():
            if s["id"] == task_id:
                return s
        errs = self.load()[1].get(task_id + ".md")
        if errs:
            raise PlanError("task %s is invalid: %s" % (task_id, "; ".join(errs[:5])))
        raise PlanError("no task %s in %s" % (task_id, self.source.label))

    def ready(self, states, live):
        tasks, why = g.ready(self.specs(), states, live)
        return {"tasks": tasks, "why_not": why}

    def graph(self, states):
        nodes, edges = g.spec_graph(self.specs(), states)
        return g.analyze(nodes, edges)

    def validate(self, spec=None):
        if spec is not None:
            errs = schema_errors(spec)
            return {"ok": not errs, "errors": errs}
        specs, errors = self.load()
        out = ["%s: %s" % (f, e) for f in sorted(errors) for e in errors[f]]
        nodes, edges = g.spec_graph(specs)
        for m in g.missing_needs(nodes, edges):
            out.append("%s.md: needs unknown task %s" % m)
        for c in g.cycles(nodes, edges):
            out.append("needs cycle: %s" % " -> ".join(c + [c[0]]))
        return {"ok": not out, "errors": out, "tasks": len(specs)}


# ----- the command adapter ------------------------------------------------------------------------

def run_json(cmd, payload, cwd, timeout=CMD_TIMEOUT_S):
    """Run an adapter command with JSON on stdin. -> parsed stdout. Raises PlanError."""
    argv = ["sh", "-c", cmd] if isinstance(cmd, str) else list(cmd)
    try:
        p = subprocess.run(argv, cwd=cwd, input=json.dumps(payload).encode("utf-8"), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PlanError("plan command %s: %s" % (argv[0], e))
    err = p.stderr.decode("utf-8", "replace").strip()
    if p.returncode != 0:
        raise PlanError("plan command %s exited %d: %s" % (argv[0], p.returncode, err[-800:] or "(no stderr)"))
    try:
        return json.loads(p.stdout.decode("utf-8"))
    except ValueError:
        raise PlanError("plan command %s did not print JSON: %r" % (argv[0], p.stdout[:200]))


def _shape(obj, ref, what):
    errs = schemaload.errors(obj, "plan-adapter", "#/definitions/" + ref)
    if errs:
        raise PlanError("invalid %s output: %s" % (what, "; ".join(errs[:5])))


def _spec(obj, what):
    errs = schema_errors(obj) if isinstance(obj, dict) else ["not an object"]
    if errs:
        raise PlanError("invalid task spec from %s: %s" % (what, "; ".join(errs[:5])))
    return obj


class CommandPlan(object):
    kind = "commands"

    def __init__(self, commands, cwd):
        self.commands = commands or {}
        self.cwd = cwd

    def _cmd(self, name):
        c = self.commands.get(name)
        if not c:
            raise PlanError("plan.commands.%s is not configured" % name)
        return c

    def get(self, task_id):
        out = run_json(self._cmd("get"), {"id": task_id}, self.cwd)
        spec = _spec(out, "get")
        if spec["id"] != task_id:
            raise PlanError("get %s returned task %s" % (task_id, spec["id"]))
        return spec

    def ready(self, states, live):
        out = run_json(self._cmd("ready"), {}, self.cwd)
        _shape(out, "ready_out", "ready")
        specs = [_spec(t, "ready") for t in out["tasks"]]
        # The command knows the plan; wavecoord adds what only the coordinator knows.
        tasks, why = [], {}
        for s in specs:
            st = g.effective(s["state"], states.get(s["id"]))
            if st != "todo":
                why[s["id"]] = "state %s" % st
                continue
            block = g.reservation_blocks(s, live)
            if block:
                why[s["id"]] = block
                continue
            tasks.append(s)
        return {"tasks": tasks, "why_not": why}

    def graph(self, states):
        out = run_json(self._cmd("graph"), {}, self.cwd)
        _shape(out, "graph_out", "graph")
        nodes = []
        for n in out["nodes"]:
            m = dict(n)
            m["spec_state"] = n["state"]
            m["state"] = g.effective(n["state"], states.get(n["id"]))
            nodes.append(m)
        return g.analyze(nodes, out["edges"])

    def validate(self, spec=None):
        payload = {"spec": spec} if spec is not None else {}
        out = run_json(self._cmd("validate"), payload, self.cwd)
        _shape(out, "validate_out", "validate")
        if spec is not None:
            errs = schema_errors(spec)
            if errs:
                return {"ok": False, "errors": errs + list(out.get("errors") or [])}
        return {"ok": bool(out["ok"]), "errors": list(out.get("errors") or [])}

    def edit(self, edits, branch=None):
        out = run_json(self._cmd("edit"), {"branch": branch, "edits": edits}, self.cwd, timeout=600)
        _shape(out, "edit_out", "edit")
        return out


def plan_dir(cfg_plan):
    return (cfg_plan or {}).get("dir") or DEFAULT_DIR


def builtin_from_tree(root, cfg_plan=None):
    """The built-in store on a working tree (the CLI, tests)."""
    return BuiltinPlan(DirSource(os.path.join(root, plan_dir(cfg_plan))))
