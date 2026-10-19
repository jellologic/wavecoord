"""Random spec trees and random ops for the spec tree's property tests (docs/design/spec-layer.md, section 10).

    files = build(random.Random(seed))            # a valid tree: {path: text}
    ops = branch_ops(r, files, own, prefix, n)    # random ops on the requirements in `own` only

A built tree is valid by construction: each requirement promises one interface
that its first task produces; later tasks may consume it (and need that task);
a requirement may consume an earlier requirement's interface (and need it);
every task writes only its own file. Ops that the op API refuses are skipped.
"""

import random

import _paths  # noqa: F401
from wavecoord.specs.ops import OpsError, apply_ops

EXEC = {"model": "sonnet", "effort": "medium", "max_turns": 30, "budget_usd": 1, "timeout_min": 20}
SEEDS = list(range(12))


def _task(nid, path, outputs, inputs=(), needs=()):
    fields = {"title": "Task " + nid.rsplit("/", 1)[1], "paths": [path], "class": "B", "size": "S",
              "exec": dict(EXEC), "status": "planned", "outputs": list(outputs)}
    if inputs:
        fields["inputs"] = list(inputs)
    if needs:
        fields["needs"] = list(needs)
    return {"op": "create", "id": nid, "fields": fields,
            "sections": {"Context": "Context of %s." % nid, "Prompt": "Do %s." % nid}}


def build_ops(r, caps=2, feats=2, reqs=2, max_tasks=3):
    ops, done_reqs = [], []
    for c in range(caps):
        cap = "cap%d" % c
        ops.append({"op": "create", "id": cap, "fields": {"title": "Capability %d" % c, "status": "active"},
                    "sections": {"Intent": "Intent of %s." % cap}})
        for f in range(feats):
            feat = "%s/feat%d" % (cap, f)
            ops.append({"op": "create", "id": feat, "fields": {"title": "Feature", "status": "active"},
                        "sections": {"Intent": "Intent of %s." % feat}})
            for q in range(reqs):
                req = "%s/req%d" % (feat, q)
                api = "x.%s.%s.%s" % (cap, "feat%d" % f, "req%d" % q)
                fields = {"title": "Requirement %s" % req, "status": "approved",
                          "outputs": [{"id": api, "kind": "interface", "contract": "api of %s" % req}]}
                if done_reqs and r.random() < 0.5:
                    src, src_api = r.choice(done_reqs)
                    fields["inputs"] = [{"id": src_api, "kind": "interface", "from": src}]
                    fields["needs"] = [src]
                ops.append({"op": "create", "id": req, "fields": fields, "sections": {
                    "Statement": "When event %s occurs, the service shall answer it." % req.replace("/", "-"),
                    "Verify": ["python -m unittest tests.test_%s" % req.replace("/", "_")]}})
                base = "src/%s" % req
                ops.append(_task(req + "/t0", base + "/t0.py",
                                 [{"id": api, "kind": "interface", "contract": "api of %s" % req},
                                  {"id": base + "/t0.py", "kind": "file"}]))
                for k in range(1, r.randint(1, max_tasks)):
                    tid = "%s/t%d" % (req, k)
                    use = r.random() < 0.5
                    ops.append(_task(tid, "%s/t%d.py" % (base, k), [{"id": "%s/t%d.py" % (base, k), "kind": "file"}],
                                     [{"id": api, "kind": "interface", "from": req + "/t0"}] if use else (),
                                     [req + "/t0"] if use else ()))
                done_reqs.append((req, api))
    return ops


def build(r, **kw):
    changes, _, _ = apply_ops({}, build_ops(r, **kw))
    return {k: v for k, v in changes.items() if v is not None}


def requirements(files):
    return sorted(p[:-len("/_node.md")] for p in files if p.count("/") == 3 and p.endswith("/_node.md"))


def tasks_of(files, req):
    return sorted("%s/%s" % (req, p.rsplit("/", 1)[1][:-3]) for p in files if p.startswith(req + "/tasks/"))


def referenced(files, nid):
    """True when any record names nid (needs or input from)."""
    needle = nid
    for p, text in files.items():
        if needle in text and not p.startswith(nid.rsplit("/", 1)[0] + "/tasks/" + nid.rsplit("/", 1)[1]):
            return True
    return False


def branch_ops(r, files, own, prefix, n):
    """Apply n random ops on the requirements in `own` (and their tasks). -> (files, applied ops)."""
    applied = []
    k = 0
    for _ in range(n * 3):
        if len(applied) >= n:
            break
        req = r.choice(own)
        tasks = tasks_of(files, req)
        kind = r.choice(["title", "statement", "tag", "status", "new_task", "rebase", "scenario", "mv", "task_tag"])
        k += 1
        if kind == "title":
            op = {"op": "set", "id": req, "key": "title", "value": "%s title %d" % (prefix, k)}
        elif kind == "statement":
            op = {"op": "set_section", "id": req, "section": "Statement",
                  "value": "When %s event %d occurs, the service shall answer it." % (prefix, k)}
        elif kind == "tag":
            op = {"op": "add", "id": req, "key": "tags", "value": "%s-%d" % (prefix, k)}
        elif kind == "task_tag" and tasks:
            op = {"op": "add", "id": r.choice(tasks), "key": "tags", "value": "%s-t%d" % (prefix, k)}
        elif kind == "status" and tasks:
            op = {"op": "set_status", "id": r.choice(tasks), "status": r.choice(["planned", "on_hold", "draft"])}
        elif kind == "new_task":
            nid = "%s/%s-n%d" % (req, prefix, k)
            path = "src/new/%s/%d.py" % (prefix, k)
            op = _task(nid, path, [{"id": path, "kind": "file"}])
        elif kind == "rebase" and tasks:
            op = {"op": "rebase_basis", "task": r.choice(tasks)}
        elif kind == "scenario":
            op = {"op": "set_scenario", "id": req, "name": "%s case %d" % (prefix, k),
                  "steps": [["Given", "state %d" % k], ["Then", "result %d" % k]]}
        elif kind == "mv" and tasks and len(own) > 1:
            leaf = [t for t in tasks if not t.endswith("/t0") and not referenced(files, t)]
            if not leaf:
                continue
            t = r.choice(leaf)
            dest = r.choice([q for q in own if q != req])
            op = {"op": "mv", "from": t, "to": "%s/%s" % (dest, t.rsplit("/", 1)[1])}
        else:
            continue
        try:
            changes, _, _ = apply_ops(files, [op])
        except OpsError:
            continue
        files = dict(files)
        for p, text in changes.items():
            if text is None:
                files.pop(p, None)
            else:
                files[p] = text
        applied.append(op)
    return files, applied
