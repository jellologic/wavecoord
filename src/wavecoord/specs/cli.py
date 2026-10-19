"""`wavecoord spec ...`: read, check and format the spec tree (docs/specs.md).

Offline: it reads the working tree (or a git ref with --ref) and never needs
the coordinator. The writes (new, set, mv, ...) come in S2.
"""

import json
import os
import sys

from wavecoord.plan.spec import SpecError

from . import check as checkmod
from . import doc, query, source
from .fingerprint import fingerprint
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
    files = source.read_ref(root, args.ref, d) if args.ref else source.read_tree(root, d)
    return root, d, Tree(files)


def _print(obj):
    print(json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False))


def cmd(args):
    try:
        return _run(args)
    except (source.SourceError, query.QueryError) as e:
        sys.stderr.write("wavecoord spec: %s\n" % e)
        return 2


def _run(args):
    root, d, tree = load(args)
    a = args.action
    if a == "tree":
        if args.id and args.id not in tree.nodes:
            raise query.QueryError("no node %s" % args.id)
        data, lines = query.tree_view(tree, args.id)
        if args.json:
            _print(data)
        else:
            print("\n".join(lines) if lines else "(no nodes under %s/)" % d)
        return 0
    if a == "show":
        if not args.id:
            raise query.QueryError("show needs a node or decision id")
        out = query.record(tree, args.id)
        if out is None:
            raise query.QueryError("no record %s" % args.id)
        if args.json:
            _print(out)
        else:
            n = tree.nodes.get(out["id"]) or tree.decisions.get(out["id"])
            sys.stdout.write(n.text)
        return 0
    if a == "query":
        hits = query.query(tree, args.id or "")
        if args.json:
            _print([query.summary(n) for n in hits])
        else:
            for n in hits:
                print("%-11s %-10s %s" % (n.kind, n.status or "?", n.id))
        return 0
    if a == "fp":
        n = tree.nodes.get(tree.resolve(args.id or "") or "")
        if n is None or n.kind != "requirement" or n.rec is None:
            raise query.QueryError("fp needs a requirement that parses")
        print(fingerprint(n.rec))
        return 0
    if a in ("check", "lint"):
        probs = checkmod.check(tree) if a == "check" else checkmod.lint(tree)
        if args.json:
            _print({"ok": not [p for p in probs if p.level == "error"], "problems": [p.to_json() for p in probs],
                    "records": len(tree.nodes) + len(tree.decisions)})
        else:
            for p in probs:
                print(p)
            n_err = len([p for p in probs if p.level == "error"])
            print("%s: %d records, %d errors, %d warnings" % ("ok" if not n_err else "invalid",
                                                              len(tree.nodes) + len(tree.decisions), n_err,
                                                              len(probs) - n_err))
        return 1 if [p for p in probs if p.level == "error"] else 0
    if a == "fmt":
        if args.ref:
            raise query.QueryError("fmt works on the working tree, not on --ref")
        return _fmt(root, d, tree, args.check, args.json)
    raise query.QueryError("unknown action %s" % a)


def _fmt(root, d, tree, check_only, as_json):
    changed, failed = [], []
    nodes = list(tree.nodes.values()) + list(tree.decisions.values())
    for n in sorted(nodes, key=lambda x: x.path):
        try:
            canon = doc.fmt(n.text, n.kind)
        except SpecError as e:
            failed.append({"path": n.path, "errors": e.errors})
            continue
        if canon != n.text:
            changed.append(n.path)
            if not check_only:
                p = os.path.join(root, d, n.path)
                with open(p, "w", encoding="utf-8", newline="\n") as f:
                    f.write(canon)
    if as_json:
        _print({"changed": changed, "failed": failed, "check": check_only})
    else:
        for p in changed:
            print("%s %s/%s" % ("would format" if check_only else "formatted", d, p))
        for f in failed:
            print("cannot format %s/%s: %s" % (d, f["path"], "; ".join(f["errors"][:3])))
    if failed or (check_only and changed):
        return 1
    return 0


def register(sub):
    sp = sub.add_parser("spec", help="read, check and format the spec tree (docs/specs.md)")
    sp.add_argument("action", choices=["tree", "show", "query", "check", "lint", "fmt", "fp"])
    sp.add_argument("id", nargs="?", help="a node id (tree, show, fp) or a query expression (query)")
    sp.add_argument("--repo", help="the consumer repo (default: found from the cwd)")
    sp.add_argument("--dir", help="the spec directory (default: spec.dir of the config, else specs)")
    sp.add_argument("--ref", help="read a git ref (for example origin/main) instead of the working tree")
    sp.add_argument("--check", action="store_true", help="fmt: change nothing; exit 1 when a file is not canonical")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd)
    return sub
