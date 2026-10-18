"""CLI commands of the lead tools: dispatch (and the dispatcher loop), collect, plan, refinery.

`dispatch`, `collect` and `refinery` call the running server with the lead
token (REST, docs/contract.md section 6). `plan` reads the built-in store of
the working tree offline, so a consumer can validate its specs in CI.
"""

import json
import os
import sys
import time

LEAD_TIMEOUT_S = 900.0


def _call(method, path, body=None):
    from wavecoord.corecli import _url, api
    try:
        status, out = api(_url(), method, path, body, timeout=LEAD_TIMEOUT_S)
    except OSError as e:
        raise SystemExit("wavecoord: the coordinator is unreachable: %s" % e)
    if status != 200:
        err = (out or {}).get("error") or {}
        raise SystemExit("wavecoord: %s %s: %s %s" % (method, path, err.get("code") or status, err.get("message") or ""))
    return out


def _show(out, as_json):
    out = {k: v for k, v in (out or {}).items() if k != "now"}
    if as_json:
        print(json.dumps(out, indent=1, sort_keys=True))
    return out


def _print_wave(out):
    for d in out.get("dispatched") or []:
        if d.get("dry_run"):
            print("would dispatch %s (%s)" % (d["task"], d.get("reason")))
        else:
            print("dispatched %s on %s (branch %s, provider %s)" % (d["id"], d["host"], d["branch"], d.get("provider")))
    for s in out.get("skipped") or []:
        print("skipped %s: %s" % (s["task"], s["why"]))


def _print_collect(out):
    for c in out.get("collected") or []:
        if c.get("error"):
            print("%s: error: %s" % (c["id"], c["error"]))
            continue
        extra = c.get("pr_url") or c.get("pr_error") or ""
        print("%s: %s [%s] %s" % (c["id"], c["state"], ",".join(c.get("actions") or []) or "-", extra))
    for p in out.get("parked") or []:
        print("%s-%s: %s outside the refinery (%s)%s" % (p["task"], p["attempt"], p.get("state") or "pr state error",
                                                         p["pr_url"], (": " + p["error"]) if p.get("error") else ""))


def cmd_dispatch(args):
    if args.loop:
        return dispatch_loop(args)
    if args.tasks and not args.wave and len(args.tasks) == 1:
        body = {"task": args.tasks[0], "host": args.host, "dry_run": args.dry_run}
        out = _show(_call("POST", "/api/dispatch", {k: v for k, v in body.items() if v is not None}), args.json)
        if not args.json:
            print("%s %s on %s (branch %s)" % ("would dispatch" if out.get("dry_run") else "dispatched",
                                               out.get("id") or out.get("task"), out.get("host") or out.get("hosts"),
                                               out.get("branch") or "-"))
        return 0
    out = _show(_call("POST", "/api/dispatch", {"wave": True, "tasks": args.tasks or None, "dry_run": args.dry_run}),
                args.json)
    if not args.json:
        _print_wave(out)
    return 0


def dispatch_loop(args):
    """The dispatcher loop: collect finished attempts (PRs, conflicts, resolvers, fallbacks), then
    dispatch the ready wave; repeat every --interval seconds."""
    tick = 0
    try:
        while True:
            tick += 1
            col = _call("POST", "/api/collect", {"dry_run": args.dry_run})
            wave = _call("POST", "/api/dispatch", {"wave": True, "dry_run": args.dry_run})
            if args.json:
                print(json.dumps({"tick": tick, "collect": _show(col, False), "wave": _show(wave, False)},
                                 sort_keys=True))
            else:
                _print_collect(col)
                _print_wave(wave)
            sys.stdout.flush()
            if args.ticks and tick >= args.ticks:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_collect(args):
    out = _show(_call("POST", "/api/collect", {"dry_run": args.dry_run, "attempt": args.attempt}), args.json)
    if not args.json:
        _print_collect(out)
        if out.get("pending"):
            print("%d attempts still running" % out["pending"])
    return 0


def cmd_refinery(args):
    if args.action == "status":
        out = _show(_call("GET", "/api/refinery"), args.json)
        if not args.json:
            for i in out.get("queue") or []:
                print("%-24s #%-6s %-8s unblocks=%-3s %s" % ("%s/%s" % (i["task"], i["attempt"]), i["pr"], i["state"],
                                                            i["unblocks"], i.get("reason") or ""))
        return 0
    out = _show(_call("POST", "/api/refinery/merge", {"pr": args.pr, "approved_protected": args.approved_protected}),
                args.json)
    if not args.json:
        for r in out.get("results") or []:
            print("%s/%s #%s: %s %s" % (r["task"], r["attempt"], r["pr"], r["state"], r.get("reason") or ""))
    return 0 if not out.get("blocked") else 1


def cmd_plan(args):
    from wavecoord.launcher.config import find_repo_root
    from wavecoord.plan.adapters import PlanError, builtin_from_tree
    root = os.path.abspath(args.repo) if args.repo else find_repo_root(os.getcwd())
    cfg_plan = {"dir": args.dir} if args.dir else None
    if not args.dir:
        from wavecoord.core import load_consumer_config
        cfg_plan = load_consumer_config(root).get("plan")
    plan = builtin_from_tree(root, cfg_plan)
    try:
        if args.action == "validate":
            out = plan.validate()
            if args.json:
                print(json.dumps(out, indent=1, sort_keys=True))
            else:
                for e in out["errors"]:
                    print(e)
                print("ok: %d tasks" % out["tasks"] if out["ok"] else "invalid: %d errors" % len(out["errors"]))
            return 0 if out["ok"] else 1
        if args.action == "get":
            if not args.id:
                raise SystemExit("wavecoord plan get: name the task")
            print(json.dumps(plan.get(args.id), indent=1, sort_keys=True))
            return 0
        if args.action == "ready":
            out = plan.ready({}, [])
            if args.json:
                print(json.dumps(out, indent=1, sort_keys=True))
            else:
                for t in out["tasks"]:
                    print(t["id"])
            return 0
        out = plan.graph({})
        if args.json:
            print(json.dumps(out, indent=1, sort_keys=True))
        else:
            print("critical path: %s" % " -> ".join(out["critical_path"]))
            for h in out["funnel_heads"]:
                print("funnel head: %s (unblocks %d)" % (h["id"], h["unblocks"]))
        return 0
    except PlanError as e:
        sys.stderr.write("wavecoord plan: %s\n" % e)
        return 1


def register(sub):
    sp = sub.add_parser("dispatch", help="dispatch tasks (one task, the listed ones, or the ready wave)")
    sp.add_argument("tasks", nargs="*", help="task ids (default: the ready wave)")
    sp.add_argument("--host", help="host for a single task")
    sp.add_argument("--wave", action="store_true", help="treat the listed tasks as a wave")
    sp.add_argument("--loop", action="store_true", help="run the dispatcher loop: collect, then dispatch the wave")
    sp.add_argument("--interval", type=float, default=30.0, help="seconds between loop ticks (default 30)")
    sp.add_argument("--ticks", type=int, default=0, help="stop the loop after N ticks (default: run until Ctrl-C)")
    sp.add_argument("--dry-run", action="store_true", help="show what would happen; change nothing")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_dispatch)

    sp = sub.add_parser("collect", help="collect finished attempts: reports, client-side PRs, conflicts, fallbacks")
    sp.add_argument("--attempt", help="only this attempt id (<task>-<n>)")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_collect)

    sp = sub.add_parser("refinery", help="the merge queue: status, or merge PRs in order")
    sp.add_argument("action", choices=["status", "merge"])
    sp.add_argument("pr", nargs="?", help="merge: one PR (number, URL, branch or <task>/<n>); default: the queue")
    sp.add_argument("--approved-protected", action="store_true",
                    help="the user approved the protected paths of this PR in the lead session")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_refinery)

    sp = sub.add_parser("plan", help="read the built-in plan store of the working tree (offline)")
    sp.add_argument("action", choices=["validate", "ready", "get", "graph"])
    sp.add_argument("id", nargs="?")
    sp.add_argument("--repo", help="the consumer repo (default: found from the cwd)")
    sp.add_argument("--dir", help="the plan directory (default: plan.dir of the config, else plan/tasks)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_plan)
    return sub
