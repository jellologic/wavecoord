"""`wavecoord spec ...`: read, check, format and change the spec tree (docs/specs.md).

Offline: it reads the working tree (or a git ref with --ref) and never needs the
coordinator. Every write goes through the op API (ops.py), so a change that
would leave the tree invalid writes nothing; --dry-run prints the diff instead.
"""

import difflib
import json
import os
import sys

from wavecoord.plan.spec import SpecError

from . import check as checkmod
from . import doc, migrate, query, source
from .fingerprint import fingerprint
from .graph import Graph
from .ops import OpsError, apply_ops
from .tree import Tree

DEFAULT_DIR = "specs"


def spec_dir(root, override=None):
    if override:
        return override.strip("/")
    from wavecoord.core import load_consumer_config
    cfg = load_consumer_config(root).get("spec") or {}
    return str(cfg.get("dir") or DEFAULT_DIR).strip("/")


def _root(args):
    if args.repo:
        return os.path.abspath(args.repo)
    from wavecoord.launcher.config import find_repo_root
    return find_repo_root(os.getcwd())


def load(args):
    root = _root(args)
    d = spec_dir(root, args.dir)
    files = source.read_ref(root, args.ref, d) if getattr(args, "ref", None) else source.read_tree(root, d)
    return root, d, files


def _print(obj):
    print(json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False))


class CliError(ValueError):
    pass


def cmd(args):
    try:
        return args.run(args)
    except (source.SourceError, query.QueryError, CliError) as e:
        sys.stderr.write("wavecoord spec: %s\n" % e)
        return 2
    except OpsError as e:
        for m in e.errors:
            sys.stderr.write("wavecoord spec: %s\n" % m)
        return 1


# ------------------------------------------------------------------ reads

def r_tree(args):
    _, d, files = load(args)
    tree = Tree(files)
    if args.id and args.id not in tree.nodes:
        raise CliError("no node %s" % args.id)
    data, lines = query.tree_view(tree, args.id)
    if args.json:
        _print(data)
    else:
        print("\n".join(lines) if lines else "(no nodes under %s/)" % d)
    return 0


def r_show(args):
    _, _, files = load(args)
    tree = Tree(files)
    out = query.record(tree, args.id)
    if out is None:
        raise CliError("no record %s" % args.id)
    if args.json:
        _print(out)
    else:
        n = tree.nodes.get(out["id"]) or tree.decisions.get(out["id"])
        sys.stdout.write(n.text)
    return 0


def r_query(args):
    _, _, files = load(args)
    hits = query.query(Tree(files), args.expr or "")
    if args.json:
        _print([query.summary(n) for n in hits])
    else:
        for n in hits:
            print("%-11s %-11s %s" % (n.kind, n.status or "?", n.id))
    return 0


def r_fp(args):
    _, _, files = load(args)
    tree = Tree(files)
    n = tree.nodes.get(tree.resolve(args.id) or "")
    if n is None or n.kind != "requirement" or n.rec is None:
        raise CliError("fp needs a requirement that parses")
    print(fingerprint(n.rec))
    return 0


def _report(probs, n_records, as_json):
    errs = [p for p in probs if p.level == "error"]
    if as_json:
        _print({"ok": not errs, "problems": [p.to_json() for p in probs], "records": n_records})
    else:
        for p in probs:
            print(p)
        print("%s: %d records, %d errors, %d warnings" % ("ok" if not errs else "invalid", n_records, len(errs),
                                                          len(probs) - len(errs)))
    return 1 if errs else 0


def r_check(args):
    _, _, files = load(args)
    tree = Tree(files)
    probs = checkmod.check(tree) if args.action == "check" else checkmod.lint(tree)
    return _report(probs, len(tree.nodes) + len(tree.decisions), args.json)


def r_waves(args):
    _, _, files = load(args)
    g = Graph(Tree(files))
    out = g.waves(args.id)
    if args.json:
        _print(out)
        return 0
    for i, w in enumerate(out["waves"]):
        print("wave %d (%d):  %s" % (i + 1, len(w), ", ".join(w)))
    if out["unplaced"]:
        print("cannot place:  %s" % ", ".join(out["unplaced"]))
    print("critical path: %s (%d)" % (" -> ".join(out["critical_path"]) or "-", len(out["critical_path"])))
    print("max width: %d" % out["width"])
    return 0


def r_ready(args):
    root, _, files = load(args)
    from wavecoord.core import load_consumer_config
    sc = load_consumer_config(root).get("spec") or {}
    out = Graph(Tree(files), require_approved=sc.get("require_approved", True) is not False).ready(
        sc.get("suspect", "warn"))
    if args.json:
        _print(out)
    else:
        for t in out["tasks"]:
            print("ready      %s" % t)
        for t, why in sorted(out["why_not"].items()):
            print("not ready  %s: %s" % (t, why))
    return 0


def r_trace(args):
    _, _, files = load(args)
    out = Graph(Tree(files)).trace(args.id)
    if out is None:
        raise CliError("no node %s" % args.id)
    _print(out)
    return 0


def r_artifacts(args):
    _, _, files = load(args)
    out = Graph(Tree(files)).artifacts()
    if args.json:
        _print(out)
    else:
        for a in out:
            print("%s  produced by %s  consumed by %s" % (a["id"], ", ".join(a["producers"]) or "-",
                                                          ", ".join(a["consumers"]) or "-"))
    return 0


def r_suspect(args):
    _, _, files = load(args)
    out = Graph(Tree(files)).suspects()
    if args.json:
        _print(out)
    else:
        for s in out:
            print("%s: %s changed (basis %s, now %s)" % (s["task"], s["requirement"], s["basis"], s["current"]))
    return 1 if out else 0


def r_fmt(args):
    if args.ref:
        raise CliError("fmt works on the working tree, not on --ref")
    root, d, files = load(args)
    tree = Tree(files)
    changed, failed = [], []
    for n in sorted(list(tree.nodes.values()) + list(tree.decisions.values()), key=lambda x: x.path):
        try:
            canon = doc.fmt(n.text, n.kind)
        except SpecError as e:
            failed.append({"path": n.path, "errors": e.errors})
            continue
        if canon != n.text:
            changed.append(n.path)
            if not args.check:
                _write(os.path.join(root, d, n.path), canon)
    if args.json:
        _print({"changed": changed, "failed": failed, "check": args.check})
    else:
        for p in changed:
            print("%s %s/%s" % ("would format" if args.check else "formatted", d, p))
        for f in failed:
            print("cannot format %s/%s: %s" % (d, f["path"], "; ".join(f["errors"][:3])))
    return 1 if failed or (args.check and changed) else 0


# ------------------------------------------------------------------ writes

def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _prune(base, rel):
    d = os.path.dirname(os.path.join(base, rel))
    while os.path.abspath(d) != os.path.abspath(base) and os.path.isdir(d) and not os.listdir(d):
        os.rmdir(d)
        d = os.path.dirname(d)


def run_ops(args, ops):
    """Apply ops to the working tree, or print the diff (--dry-run)."""
    if getattr(args, "ref", None):
        raise CliError("writes change the working tree, not --ref")
    root, d, files = load(args)
    changes, moves, problems = apply_ops(files, ops)
    base = os.path.join(root, d)
    if args.dry_run:
        for rel in sorted(changes):
            old = files.get(rel, "")
            new = changes[rel] or ""
            sys.stdout.writelines(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                                       "a/%s/%s" % (d, rel) if rel in files else "/dev/null",
                                                       "b/%s/%s" % (d, rel) if changes[rel] is not None else "/dev/null"))
        return 0
    for rel, text in sorted(changes.items()):
        if text is not None:
            _write(os.path.join(base, rel), text)
    for rel, text in sorted(changes.items()):
        if text is None and os.path.exists(os.path.join(base, rel)):
            os.remove(os.path.join(base, rel))
            _prune(base, rel)
    warnings = [p for p in problems if p.level == "warning"]
    if args.json:
        _print({"changed": sorted(r for r, t in changes.items() if t is not None),
                "deleted": sorted(r for r, t in changes.items() if t is None), "moves": moves,
                "warnings": [p.to_json() for p in warnings]})
    else:
        for old, new in moves:
            print("moved   %s/%s -> %s/%s" % (d, old, d, new))
        for rel in sorted(changes):
            if not any(rel == n for _, n in moves):
                print("%s %s/%s" % ("deleted" if changes[rel] is None else "wrote  ", d, rel))
        for p in warnings:
            print(p)
    return 0


def _value(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def _text_arg(v):
    return sys.stdin.read() if v == "-" else v


def w_new(args):
    fields = {}
    if args.title:
        fields["title"] = args.title
    for kv in args.set or []:
        k, _, v = kv.partition("=")
        fields[k] = _value(v)
    sections = {}
    for kv in args.section or []:
        k, _, v = kv.partition("=")
        sections[k] = _text_arg(v)
    op = {"op": "create", "id": args.id, "fields": fields, "sections": sections}
    if args.kind:
        op["kind"] = args.kind
    return run_ops(args, [op])


def w_simple(args):
    a = args.action
    if a == "set":
        ops = [{"op": "set", "id": args.id, "key": args.key, "value": _value(args.value)}]
    elif a == "status":
        ops = [{"op": "set_status", "id": args.id, "status": args.value}]
    elif a == "section":
        v = _text_arg(args.value)
        ops = [{"op": "set_section", "id": args.id, "section": args.key, "value": v}]
    elif a in ("add", "rm"):
        ops = [{"op": "add" if a == "add" else "remove", "id": args.id, "key": args.key, "value": args.value}]
    elif a == "artifact":
        ops = [{"op": "set_artifact", "id": args.id, "list": args.key, "artifact": json.loads(args.value)}]
    elif a == "artifact-rm":
        ops = [{"op": "remove_artifact", "id": args.id, "list": args.key, "artifact": args.value}]
    elif a == "scenario":
        steps = [s.split(" ", 1) for s in args.steps]
        ops = [{"op": "set_scenario", "id": args.id, "name": args.key, "steps": steps}]
    elif a == "mv":
        ops = [{"op": "mv", "from": args.id, "to": args.key}]
    elif a == "rebase-basis":
        ops = [{"op": "rebase_basis", "task": args.id}]
    elif a == "supersede":
        ops = [{"op": "supersede", "old": args.id, "new": args.key}]
    elif a == "delete":
        ops = [{"op": "delete", "id": args.id, "force": bool(args.force)}]
    else:
        raise CliError("unknown action %s" % a)
    return run_ops(args, ops)


def w_apply(args):
    data = json.loads(_text_arg(args.file) if args.file == "-" else open(args.file, encoding="utf-8").read())
    ops = data["ops"] if isinstance(data, dict) else data
    return run_ops(args, ops)


def r_status_sync(args):
    from wavecoord.leadcli import _call
    out = _call("POST", "/api/spec/status")
    out.pop("now", None)
    if args.json:
        _print(out)
    elif out.get("skipped"):
        print("skipped: %s" % out["skipped"])
    elif out.get("blocked"):
        print("blocked: %s" % out.get("reason"))
        return 1
    else:
        for c in out.get("changes") or []:
            print("%s: %s -> %s" % (c["task"], c["from"], c["to"]))
        print("%d change(s)%s" % (len(out.get("changes") or []), (" in %s (%s)" % (out["sha"][:10], out["via"]))
                                  if out.get("sha") else ""))
    return 0


def w_migrate(args):
    from wavecoord.core import load_consumer_config
    from wavecoord.plan.adapters import builtin_from_tree, plan_dir
    root = _root(args)
    cfg_plan = load_consumer_config(root).get("plan")
    plan = builtin_from_tree(root, cfg_plan)
    specs, errors = plan.load()
    if errors:
        raise CliError("plan/tasks has invalid specs: %s" % "; ".join("%s: %s" % (k, v[0]) for k, v in errors.items()))
    if not specs:
        raise CliError("no task specs in %s" % plan_dir(cfg_plan))
    ops, ids = migrate.plan_ops(specs, args.into)
    rc = run_ops(args, ops)
    if rc == 0 and not args.dry_run:
        pdir = os.path.join(root, plan_dir(cfg_plan))
        if not args.keep:
            for old in ids:
                p = os.path.join(pdir, old + ".md")
                if os.path.exists(p):
                    os.remove(p)
        if not args.json:
            for old, new in sorted(ids.items()):
                print("%s -> %s" % (old, new))
    return rc


# ------------------------------------------------------------------ parser

def register(sub):
    sp = sub.add_parser("spec", help="read, check, format and change the spec tree (docs/specs.md)")
    acts = sp.add_subparsers(dest="action", metavar="<action>")
    acts.required = True

    def add(name, run, help_, **flags):
        p = acts.add_parser(name, help=help_)
        p.add_argument("--repo", help="the consumer repo (default: found from the cwd)")
        p.add_argument("--dir", help="the spec directory (default: spec.dir of the config, else specs)")
        p.add_argument("--ref", help="read a git ref (for example origin/main) instead of the working tree")
        p.add_argument("--json", action="store_true")
        if flags.get("write"):
            p.add_argument("--dry-run", action="store_true", help="print the diff; change nothing")
        p.set_defaults(func=cmd, run=run)
        return p

    p = add("tree", r_tree, "the tree with status and progress")
    p.add_argument("id", nargs="?")
    p = add("show", r_show, "one record (text, or --json)")
    p.add_argument("id")
    p = add("query", r_query, "records that match: key=value key!=value key~glob key !key under=<id>")
    p.add_argument("expr", nargs="?")
    add("check", r_check, "every rule; exit 1 on an error")
    add("lint", r_check, "style warnings")
    p = add("fmt", r_fmt, "write every record in canonical form")
    p.add_argument("--check", action="store_true", help="change nothing; exit 1 when a file is not canonical")
    p = add("fp", r_fp, "the fingerprint of a requirement")
    p.add_argument("id")
    p = add("waves", r_waves, "what can run at the same time")
    p.add_argument("id", nargs="?", help="only the tasks under this node")
    add("ready", r_ready, "ready tasks, and why the others are not")
    p = add("trace", r_trace, "ancestors, children, needs, producers, consumers, decisions")
    p.add_argument("id")
    add("artifacts", r_artifacts, "every artifact, its producers and consumers")
    add("suspect", r_suspect, "tasks whose requirement changed since their basis; exit 1 when any")

    p = add("new", w_new, "create a record", write=True)
    p.add_argument("id")
    p.add_argument("--kind", choices=["capability", "feature", "requirement", "task", "decision"])
    p.add_argument("--title")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="a field; VALUE is JSON or text")
    p.add_argument("--section", action="append", metavar="NAME=TEXT", help="a section; TEXT - reads stdin")
    for name, help_, args_ in (("set", "set a field (title, basis, class, size, exec.<key>)", ("key", "value")),
                               ("status", "set the status", ("value",)),
                               ("section", "set a section (TEXT - reads stdin)", ("key", "value")),
                               ("add", "add an item to a list", ("key", "value")),
                               ("rm", "remove an item from a list", ("key", "value")),
                               ("artifact", "add or replace an input or output (JSON)", ("key", "value")),
                               ("artifact-rm", "remove an input or output by id", ("key", "value")),
                               ("mv", "move or rename a node (keeps the old id as an alias)", ("key",)),
                               ("rebase-basis", "record that a task still fits its requirement", ()),
                               ("supersede", "the second requirement supersedes the first", ("key",)),
                               ("delete", "delete a record (refused while referenced)", ())):
        p = add(name, w_simple, help_, write=True)
        p.add_argument("id")
        for a in args_:
            p.add_argument(a)
        if name == "delete":
            p.add_argument("--force", action="store_true")
    p = add("scenario", w_simple, "add or replace a scenario: NAME 'Given ...' 'When ...' 'Then ...'", write=True)
    p.add_argument("id")
    p.add_argument("key", metavar="name")
    p.add_argument("steps", nargs="+")
    p = add("apply", w_apply, "apply ops from a JSON file (schemas/spec-ops.schema.json); - reads stdin", write=True)
    p.add_argument("file")
    add("status-sync", r_status_sync, "ask the coordinator to write every task's status into the tree now")
    p = add("migrate-plan", w_migrate, "move plan/tasks into the spec tree", write=True)
    p.add_argument("--into", default="legacy", help="the capability to create (default legacy)")
    p.add_argument("--keep", action="store_true", help="keep the old plan/tasks files")
    return sub
