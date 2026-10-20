"""The wavecoord command line. Stdlib only, Python >= 3.9.

Implemented: `version`, `status`, `hook`, `guard`, the core commands (serve, log, revert,
rebuild, events, tokens; see corecli.py), `channel` (see docs/feedback.md), the launcher
(launch, hosts, attempts, gc; see docs/launcher.md) and the lead tools (dispatch, collect,
refinery, plan; see leadcli.py), `install-server` (installsrv.py), and the setup commands
`doctor` (doctor.py) and `init` (initrepo.py).
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from wavecoord import __version__, corecli

NOT_IMPLEMENTED_EXIT = 2
STATUS_TIMEOUT_S = 3.0
CONFIG_REL_PATH = os.path.join(".claude", "wavecoord.json")

# Planned hook events (docs/hooks.md). Unknown events are accepted too, so a
# newer Claude Code event never breaks a session.
HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolBatch",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "StopFailure",
    "Notification",
    "PreCompact",
    "SessionEnd",
)

STUBS = {
}


def _find_repo_config(start):
    """Return the parsed .claude/wavecoord.json above `start`, or None."""
    d = os.path.abspath(start)
    while True:
        p = os.path.join(d, CONFIG_REL_PATH)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else None
            except (OSError, ValueError):
                return None
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def resolve_coord_url(env=None, cwd=None):
    """The coordinator URL, or None when not configured.

    Order: WAVECOORD_URL, then the plugin option CLAUDE_PLUGIN_OPTION_COORD_URL,
    then coord_url in .claude/wavecoord.json. The plugin default value counts
    as configured only when it comes through the plugin option.
    """
    env = os.environ if env is None else env
    for key in ("WAVECOORD_URL", "CLAUDE_PLUGIN_OPTION_COORD_URL"):
        v = env.get(key, "").strip()
        if v:
            return v.rstrip("/")
    cfg = _find_repo_config(cwd or os.getcwd())
    if cfg and isinstance(cfg.get("coord_url"), str) and cfg["coord_url"].strip():
        return cfg["coord_url"].strip().rstrip("/")
    return None


def cmd_version(_args):
    print("wavecoord " + __version__)
    return 0


def cmd_status(args):
    url = (args.url or "").strip().rstrip("/") or resolve_coord_url()
    if not url:
        print("not configured")
        return 0
    health = url + "/api/health"
    try:
        with urllib.request.urlopen(health, timeout=args.timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        print("unreachable: no coordinator answers at %s (%s)" % (url, getattr(e, "reason", e)))
        return 1
    ver = str(body.get("version") or "")
    print("ok: %s version=%s api=%s" % (url, ver[len("wavecoord "):] if ver.startswith("wavecoord ") else ver,
                                         body.get("api")))
    return 0


def cmd_hook(args):
    """Run one Claude Code hook event (wavecoord.hooks). Exits 0, or 2 for a fence deny backstop."""
    try:
        from wavecoord import hooks
        return hooks.main(args.event)
    except Exception as e:
        if args.event == "PreToolUse" and os.environ.get("WAVECOORD_TASK") and \
                "enforce" in os.environ.get("WAVECOORD_FENCE", ""):
            sys.stderr.write("wavecoord fence: the hook failed (%s); the edit is refused (fail closed)\n"
                             % type(e).__name__)
            return 2
        return 0


def cmd_guard(args):
    from wavecoord import guard
    if args.guard_cmd == "install":
        return guard.install(args.worktree)
    if args.staged:
        return guard.check("staged", enforce=args.enforce)
    return guard.check("push", args.push, enforce=args.enforce)


def cmd_channel(args):
    from wavecoord import channel
    return channel.main(args)


def cmd_inbox_relay(args):
    from wavecoord import relay
    return relay.main()


def _stub(name):
    def run(_args):
        sys.stderr.write("wavecoord %s: not implemented in P0\n" % name)
        return NOT_IMPLEMENTED_EXIT

    return run


def build_parser():
    p = argparse.ArgumentParser(prog="wavecoord", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    sp = sub.add_parser("version", help="print the version")
    sp.set_defaults(func=cmd_version)

    sp = sub.add_parser("status", help="show whether the coordinator is configured and reachable")
    sp.add_argument("--timeout", type=float, default=STATUS_TIMEOUT_S)
    sp.add_argument("--url", help="coordinator URL; empty means the usual lookup")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("hook", help="Claude Code hook entry point (reads JSON on stdin)")
    sp.add_argument("event", help="hook event name, for example SessionStart")
    sp.set_defaults(func=cmd_hook)

    sp = sub.add_parser("guard", help="git pre-commit and pre-push guards (advisory)")
    gsub = sp.add_subparsers(dest="guard_cmd", metavar="<install|check>")
    gsub.required = True
    gp = gsub.add_parser("install", help="install the guards in a worktree")
    gp.add_argument("--worktree", help="worktree directory (default: the current directory)")
    gp.set_defaults(func=cmd_guard)
    gp = gsub.add_parser("check", help="check changed paths with the coordinator")
    mode = gp.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="check the staged paths (pre-commit)")
    mode.add_argument("--push", metavar="RANGE", help="check a revision range, or - for pre-push stdin")
    gp.add_argument("--enforce", action="store_true", help="fail closed: refuse on a violation or any failure")
    gp.set_defaults(func=cmd_guard)

    sp = sub.add_parser("channel", help="stdio MCP channel that pushes lead events into the session")
    sp.set_defaults(func=cmd_channel)

    sp = sub.add_parser("inbox-relay", help="internal: push a worker session's messages into its Claude Code"
                                            " inbox socket (started by the SessionStart hook)")
    sp.set_defaults(func=cmd_inbox_relay)

    corecli.register(sub)
    from wavecoord import fencecli
    fencecli.register(sub)
    from wavecoord.launcher.cli import register as register_launcher
    register_launcher(sub)
    from wavecoord.leadcli import register as register_lead
    register_lead(sub)
    from wavecoord.specs.cli import register as register_spec
    register_spec(sub)
    from wavecoord.installsrv import register as register_install
    register_install(sub)
    from wavecoord import doctor, initrepo
    doctor.register(sub)
    initrepo.register(sub)

    for name, helptext in STUBS.items():
        sp = sub.add_parser(name, help=helptext + " (not implemented in P0)")
        sp.set_defaults(func=_stub(name), accepts_extra=True)
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["_remote"]:  # host side of the launcher; JSON on stdin and stdout
        from wavecoord.launcher.hostside import main as remote_main
        return remote_main(argv[1:])
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    if extra and not getattr(args, "accepts_extra", False):
        parser.error("unrecognized arguments: %s" % " ".join(extra))
    return args.func(args)
