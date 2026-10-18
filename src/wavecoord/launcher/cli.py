"""CLI commands of the launcher: launch, hosts, attempts and gc.

The host-side entry point `wavecoord _remote <cmd>` is dispatched in
wavecoord.cli.main before argparse (see launcher/hostside.py)."""

import json
import sys

from .common import FINAL, LauncherError


def _launcher(args):
    from .client import Launcher
    from .config import load_config
    cfg = load_config(getattr(args, "config", None))
    return Launcher(cfg, coord_url=getattr(args, "coord_url", None) or None,
                    plugin_dir=getattr(args, "plugin_dir", None))


def _print(obj, as_json):
    if as_json:
        print(json.dumps(obj, indent=1, sort_keys=True))
        return
    if isinstance(obj, dict):
        for k in sorted(obj):
            print("%s: %s" % (k, obj[k]))


def _err(e):
    sys.stderr.write("wavecoord: error: %s\n" % e)
    return 1


def cmd_launch(args):
    from .client import load_spec, prompt_from_spec
    try:
        L = _launcher(args)
        opts = {"task": args.task, "host": args.host, "model": args.model, "effort": args.effort,
                "max_turns": args.max_turns, "max_budget_usd": args.max_budget_usd,
                "fallback_model": args.fallback_model, "timeout_min": args.timeout_min,
                "continue": args.cont, "wait": args.wait, "attempt": args.attempt,
                "lease_id": args.lease_id, "lease_epoch": args.lease_epoch, "no_claim": args.no_claim,
                "claude_flags": args.claude_flag or None, "provider": args.provider,
                "provider_fallback": [x for x in (args.provider_fallback or "").split(",") if x] or None}
        prompt = None
        if args.spec:
            spec = load_spec(args.spec)
            opts["task"] = opts["task"] or spec["id"]
            ex = spec.get("exec") or {}
            for src, dst in (("model", "model"), ("effort", "effort"), ("max_turns", "max_turns"),
                             ("budget_usd", "max_budget_usd"), ("fallback_model", "fallback_model"),
                             ("timeout_min", "timeout_min"), ("provider", "provider"),
                             ("provider_fallback", "provider_fallback")):
                if opts.get(dst) is None and ex.get(src) is not None:
                    opts[dst] = ex[src]
            opts["reservations"] = [{"glob": g, "mode": "exclusive"} for g in spec.get("paths") or []] + \
                                   [{"glob": g, "mode": "shared"} for g in spec.get("shared_paths") or []]
            prompt = prompt_from_spec(spec)
        if args.prompt_file:
            if args.prompt_file == "-":
                prompt = sys.stdin.read()
            else:
                with open(args.prompt_file, encoding="utf-8") as f:
                    prompt = f.read()
        if not opts["task"]:
            raise LauncherError("--task (or --spec) is required")
        if prompt is None or not prompt.strip():
            raise LauncherError("--prompt-file (or --spec with a prompt) is required")
        opts["prompt"] = prompt
        rec = L.launch(opts)
        if not args.wait:
            if args.json:
                _print(rec, True)
            else:
                print("launched %s on %s (branch %s, provider %s, lease %s)" % (
                rec["id"], rec["host"], rec["branch"], rec.get("provider"), rec.get("lease_epoch") or "none"))
            return 0
        if not args.json:
            sys.stderr.write("wavecoord: launched %s on %s; waiting\n" % (rec["id"], rec["host"]))
        res = L.wait(rec["id"])
        if res.get("pr_ready"):
            res["pr_url"], res["pr_error"] = L.open_pr(res)  # the PR is opened here, on the client
            _record_end(L, res)
        _print(res, True) if args.json else _print_result(res)
        return 0 if res.get("state") == "reported" else 1
    except (LauncherError, OSError) as e:
        return _err(e)


def _record_end(L, res):
    """Tell the coordinator the PR (best effort; needs a lead token). `wavecoord collect` does it too."""
    from . import coord as coordmod
    if not L.coord_url:
        return
    body = {k: res.get(k) for k in ("state", "pr_url", "pr_error") if res.get(k) is not None}
    try:
        coordmod.Coord(L.coord_url, agent="lead").request(
            "POST", "/api/attempts/%s/%s/end" % (res["task"], res["attempt"]), body,
            idem="end:%s:%s" % (res.get("id"), res.get("pr_url")))
    except LauncherError:
        pass


def _print_result(r):
    print("%s on %s: %s%s" % (r.get("id"), r.get("host"), r.get("state"),
                              (" (%s)" % r["note"]) if r.get("note") else ""))
    for k in ("branch", "provider", "report_status", "pushed", "pr_url", "pr_error", "conflicts", "turns", "cost_usd",
              "cost_basis", "throttle", "next_provider", "session_id", "worktree"):
        if r.get(k) not in (None, "", []):
            print("  %s: %s" % (k, r[k]))


def cmd_hosts(args):
    from .client import why_ineligible
    try:
        L = _launcher(args)
        probes = L.probe([args.host] if args.host else None)
    except LauncherError as e:
        return _err(e)
    if args.json:
        _print(probes, True)
        return 0
    print("%-12s %-6s %-8s %-8s %-10s %-24s %-24s %s" % ("HOST", "SLOTS", "MEM_GIB", "DISK_GIB", "AUTH", "CLAUDE",
                                                        "PROVIDERS", "STATUS"))
    for n in sorted(probes):
        p = probes[n]
        c = p.get("claude") or {}
        mem = p.get("mem_avail_gib")
        disk = p.get("disk_free_gib")
        provs = ",".join(k + ("" if v.get("present") else "(missing)") for k, v in sorted((p.get("providers") or {}).items()))
        print("%-12s %-6s %-8s %-8s %-10s %-24s %-24s %s" % (
            n, "%s/%s" % (p.get("active", "?"), p.get("slots", "?")), "%.1f" % mem if mem is not None else "?",
            "%.0f" % disk if disk is not None else "?", c.get("auth", "?"), (c.get("version") or "-")[:24],
            provs or "-", why_ineligible(p, L.cfg.default_provider) or "ok"))
    return 0


def cmd_attempts(args):
    try:
        L = _launcher(args)
        if args.action == "ls":
            rows = [r for n, rs in sorted(L.ls([args.host] if args.host else None).items()) for r in rs]
            if args.active:
                rows = [r for r in rows if r["state"] not in FINAL]
            if args.json:
                _print(rows, True)
                return 0
            print("%-28s %-10s %-10s %-8s %-8s %s" % ("ATTEMPT", "HOST", "STATE", "TURNS", "COST", "PR / NOTE"))
            for r in rows:
                print("%-28s %-10s %-10s %-8s %-8s %s" % (r["id"], r["host"], r["state"], r.get("turns") or "-",
                                                         r.get("cost_usd") if r.get("cost_usd") is not None else "-",
                                                         r.get("pr_url") or r.get("note") or ""))
            return 0
        if not args.id:
            raise LauncherError("attempts %s needs an attempt id" % args.action)
        if args.action == "result":
            r = L.result(args.id)
            _print(r, True) if args.json else _print_result(r)
            return 0 if r.get("ok") else 1
        if args.action == "stop":
            r = L.stop(args.id)
            _print(r, True) if args.json else print("%s: %s" % (args.id, r.get("state") or r.get("error")))
            return 0 if r.get("ok") else 1
        if args.action == "logs":
            return L.logs(args.id, follow=args.follow, tail=args.tail, raw=args.raw)
    except LauncherError as e:
        return _err(e)
    return 2


def cmd_gc(args):
    try:
        L = _launcher(args)
        out = L.gc(dry_run=args.dry_run, names=[args.host] if args.host else None)
    except LauncherError as e:
        return _err(e)
    if args.json:
        _print(out, True)
        return 0
    for n in sorted(out):
        o = out[n]
        if not o.get("ok"):
            print("%s: %s" % (n, o.get("error")))
            continue
        for r in o["removed"]:
            print("%s: %s %s" % (n, "would remove" if args.dry_run else "removed", r["worktree"]))
        for r in o["kept"]:
            print("%s: kept %s (%s)" % (n, r["worktree"], r["why"]))
    return 0


def _common(sp):
    sp.add_argument("--config", help="path to .claude/wavecoord.json (default: found from the cwd)")
    sp.add_argument("--json", action="store_true", help="print JSON")


def register(sub):
    """Add the launcher commands to the top-level subparsers."""
    sp = sub.add_parser("launch", help="launch a worker attempt on a host")
    _common(sp)
    sp.add_argument("--task", help="task id")
    sp.add_argument("--prompt-file", help="the worker prompt ('-' for stdin)")
    sp.add_argument("--spec", help="a task spec as JSON (schemas/task-spec.schema.json): id, exec, paths, prompt")
    sp.add_argument("--host", help="host name from hosts[] (default: placement)")
    sp.add_argument("--model")
    sp.add_argument("--effort")
    sp.add_argument("--max-turns", type=int)
    sp.add_argument("--max-budget-usd", type=float)
    sp.add_argument("--fallback-model")
    sp.add_argument("--timeout-min", type=float, help="stop the worker after this time (default 120)")
    sp.add_argument("--attempt", type=int, help="attempt number (default: the next free one)")
    sp.add_argument("--continue", dest="cont", action="store_true",
                    help="run again on the latest (or --attempt) attempt's branch; resumes its session on the same host")
    sp.add_argument("--wait", action="store_true", help="queue while all hosts are full, then wait for the result")
    sp.add_argument("--lease-id", type=int, help="lease given by the lead (skips the claim)")
    sp.add_argument("--lease-epoch", type=int, help="fencing epoch of that lease")
    sp.add_argument("--no-claim", action="store_true", help="do not claim a lease")
    sp.add_argument("--claude-flag", action="append", help="extra flag for claude (repeat)")
    sp.add_argument("--provider", help="provider profile (default: default_provider, else anthropic); docs/providers.md")
    sp.add_argument("--provider-fallback", help="comma-separated profiles to try next after a rate limit (recorded for the dispatcher)")
    sp.add_argument("--coord-url", help="coordinator URL (default: WAVECOORD_URL, plugin option, config)")
    sp.add_argument("--plugin-dir", help="plugin dir to pin on hosts (default: this wavecoord's plugin)")
    sp.set_defaults(func=cmd_launch)

    sp = sub.add_parser("hosts", help="probe the configured hosts")
    _common(sp)
    sp.add_argument("--host")
    sp.set_defaults(func=cmd_hosts)

    sp = sub.add_parser("attempts", help="list and manage launched attempts")
    _common(sp)
    sp.add_argument("action", choices=["ls", "logs", "stop", "result"])
    sp.add_argument("id", nargs="?", help="attempt id, <task>-<n>")
    sp.add_argument("--host")
    sp.add_argument("--active", action="store_true", help="ls: only attempts in flight")
    sp.add_argument("--follow", "-f", action="store_true", help="logs: follow until the attempt ends")
    sp.add_argument("--tail", type=int, help="logs: last N lines of each log")
    sp.add_argument("--raw", action="store_true", help="logs: raw stream-json")
    sp.set_defaults(func=cmd_attempts)

    sp = sub.add_parser("gc", help="remove finished worktrees that have no unpushed commits")
    _common(sp)
    sp.add_argument("--host")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_gc)

    return sub

