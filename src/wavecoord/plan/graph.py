"""The `needs` graph: effective states, ready, cycles, critical path and funnel heads.

Pure functions over plain dicts, shared by the built-in store and the command
adapter. A node is {id, state, phase, size?, ...}; an edge {from, to} means
`from` needs `to`.
"""

from wavecoord.globs import globs_overlap

SIZE_WEIGHT = {"XS": 1, "S": 2, "M": 3, "L": 5, "XL": 8}
CLOSED = ("done", "dropped")


def effective(spec_state, override):
    """The state to use: a closed spec state wins; else the coordinator's view, else the spec."""
    if spec_state in CLOSED or not override:
        return spec_state
    return override


def needs_map(nodes, edges):
    ids = [n["id"] for n in nodes]
    needs = {i: [] for i in ids}
    for e in edges:
        if e["from"] in needs and e["to"] not in needs[e["from"]]:
            needs[e["from"]].append(e["to"])
    return needs


def missing_needs(nodes, edges):
    ids = set(n["id"] for n in nodes)
    return sorted(set((e["from"], e["to"]) for e in edges if e["to"] not in ids))


def cycles(nodes, edges):
    """Each cycle once, as a list of ids, in a stable order."""
    needs = needs_map(nodes, edges)
    color, stack, out, seen = {}, [], [], set()

    def visit(n):
        color[n] = 1
        stack.append(n)
        for m in needs.get(n, []):
            if m not in needs:
                continue
            if color.get(m) == 1:
                cyc = stack[stack.index(m):]
                key = tuple(sorted(cyc))
                if key not in seen:
                    seen.add(key)
                    out.append(list(cyc))
            elif color.get(m) is None:
                visit(m)
        stack.pop()
        color[n] = 2

    for n in sorted(needs):
        if color.get(n) is None:
            visit(n)
    return out


def dependents(nodes, edges):
    """id -> set of ids that need it, transitively."""
    needs = needs_map(nodes, edges)
    direct = {i: set() for i in needs}
    for a, ns in needs.items():
        for b in ns:
            if b in direct:
                direct[b].add(a)
    out = {}
    for i in needs:
        seen, todo = set(), list(direct[i])
        while todo:
            x = todo.pop()
            if x in seen or x == i:
                continue
            seen.add(x)
            todo.extend(direct.get(x, ()))
        out[i] = seen
    return out


def analyze(nodes, edges):
    """Add `unblocks` to each node and return {nodes, edges, critical_path, funnel_heads, cycles, missing}.

    unblocks: open tasks that wait on this one, transitively.
    critical_path: the longest chain of open tasks by size weight (XS 1 .. XL 8; 1 when unknown),
    from the task to do first to the last one.
    funnel_heads: open tasks whose needs are all closed and that unblock other open tasks,
    most unblocking first. Dispatch and merge them first."""
    by = {n["id"]: n for n in nodes}
    needs = needs_map(nodes, edges)
    deps = dependents(nodes, edges)
    cyc = cycles(nodes, edges)
    in_cycle = set(i for c in cyc for i in c)
    open_ = set(i for i, n in by.items() if n.get("state") not in CLOSED)
    out_nodes = []
    for n in nodes:
        m = dict(n)
        m["unblocks"] = len([d for d in deps[n["id"]] if d in open_])
        out_nodes.append(m)
    # longest weighted path over open, acyclic nodes
    best = {}

    def longest(i, depth=0):
        if i in best:
            return best[i]
        if depth > len(by) or i in in_cycle:
            return (0, [])
        w = SIZE_WEIGHT.get(by[i].get("size"), 1)
        sub = [longest(j, depth + 1) for j in sorted(needs.get(i, [])) if j in open_ and j in by]
        top = max(sub, key=lambda x: x[0]) if sub else (0, [])
        best[i] = (top[0] + w, top[1] + [i])
        return best[i]

    crit = (0, [])
    for i in sorted(open_):
        c = longest(i)
        if c[0] > crit[0]:
            crit = c
    heads = []
    for m in out_nodes:
        if m["id"] in open_ and m["unblocks"] > 0 and \
                all(by.get(j, {}).get("state") in CLOSED for j in needs.get(m["id"], [])):
            heads.append({"id": m["id"], "unblocks": m["unblocks"], "state": m.get("state")})
    heads.sort(key=lambda h: (-h["unblocks"], h["id"]))
    return {"nodes": out_nodes, "edges": [dict(e) for e in edges], "critical_path": crit[1],
            "critical_weight": crit[0], "funnel_heads": heads, "cycles": cyc,
            "missing": [{"from": a, "to": b} for a, b in missing_needs(nodes, edges)]}


def spec_graph(specs, states=None):
    """Specs -> (nodes, edges). `states` maps id -> effective state (else the spec state)."""
    states = states or {}
    nodes, edges = [], []
    for s in specs:
        nodes.append({"id": s["id"], "title": s.get("title"), "phase": s["phase"], "size": s.get("size"),
                      "class": s.get("class"), "spec_state": s["state"],
                      "state": effective(s["state"], states.get(s["id"]))})
        for n in s.get("needs") or []:
            edges.append({"from": s["id"], "to": n})
    return nodes, edges


def reservation_blocks(spec, live):
    """Why `spec` overlaps live reservations of other leases, or None.

    Its paths (exclusive) overlap any live reservation, or its shared_paths overlap
    an exclusive one."""
    mine = [(g, "exclusive") for g in spec.get("paths") or []] + \
           [(g, "shared") for g in spec.get("shared_paths") or []]
    for g, mode in mine:
        for r in live:
            if r.get("task") == spec["id"]:
                continue
            if "exclusive" in (mode, r["mode"]) and globs_overlap(g, r["glob"]):
                return "%s overlaps %s (%s, task %s)" % (g, r["glob"], r["mode"], r.get("task"))
    return None


def specs_overlap(a, b):
    """Would two tasks conflict if both ran now (the same rule as reservations)?"""
    live = [{"glob": g, "mode": "exclusive", "task": b["id"]} for g in b.get("paths") or []] + \
           [{"glob": g, "mode": "shared", "task": b["id"]} for g in b.get("shared_paths") or []]
    return reservation_blocks(a, live)


def ready(specs, states, live):
    """Ready specs in plan order: effective state todo, every need done, no overlap with `live`.

    -> (ready specs, {id: why not} for the todo specs that are not ready)."""
    by = {s["id"]: s for s in specs}
    st = lambda i: effective(by[i]["state"], states.get(i)) if i in by else None  # noqa: E731
    out, why = [], {}
    for s in specs:
        if st(s["id"]) != "todo":
            continue
        unmet = [n for n in s.get("needs") or [] if st(n) != "done"]
        if unmet:
            why[s["id"]] = "needs not done: %s" % ", ".join(unmet)
            continue
        block = reservation_blocks(s, live)
        if block:
            why[s["id"]] = block
            continue
        out.append(s)
    return out, why
