"""`wavecoord fence offline|online|status` and `wavecoord gate-check` (docs/design/edit-fence.md).

`fence offline` is for the human, in a terminal, while the coordinator is
down. It refuses to run inside a Claude Code Bash tool (CLAUDECODE is set),
refuses while the coordinator answers (decision D7), and caps the TTL at 4 h.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from wavecoord import fence, gatecheck
from wavecoord.client import Client, Deadline, resolve_coord_url

HEALTH_TIMEOUT_S = 2.0


def parse_duration(text):
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([smh]?)\s*$", str(text))
    if not m:
        raise ValueError("a duration is <n>s, <n>m or <n>h")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def coord_answers(env, cwd):
    url = resolve_coord_url(env, cwd)
    if not url:
        return None, False
    try:
        with urllib.request.urlopen(url + "/api/health", timeout=HEALTH_TIMEOUT_S) as r:
            return url, r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return url, False


def offline(seconds_text, reason, env=None, cwd=None, out=sys.stdout, err=sys.stderr):
    env = os.environ if env is None else env
    if (env.get("CLAUDECODE") or "").strip():
        err.write("wavecoord fence offline: refused: this runs in a Claude Code session (CLAUDECODE is set). "
                  "The offline override is for the human only; run it in your own terminal.\n")
        return 2
    try:
        seconds = parse_duration(seconds_text)
    except ValueError as e:
        err.write("wavecoord fence offline: %s\n" % e)
        return 2
    if seconds <= 0:
        err.write("wavecoord fence offline: the duration must be more than 0\n")
        return 2
    if not reason or not reason.strip():
        err.write("wavecoord fence offline: --reason is required\n")
        return 2
    url, up = coord_answers(env, cwd or os.getcwd())
    if up:
        err.write("wavecoord fence offline: refused: the coordinator at %s answers. The override is only for a "
                  "coordinator outage.\n" % url)
        return 1
    if seconds > fence.MAX_OVERRIDE_S:
        err.write("wavecoord fence offline: the maximum is 4 h; the override lasts 4 h\n")
    rec = fence.write_override(env, seconds, reason.strip())
    out.write("wavecoord fence: offline override until %s (%s). Edit and commit checks that cannot reach the "
              "coordinator now pass and are logged; push and merge checks still need the coordinator.\n"
              % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rec["until"])), fence.override_path(env)))
    return 0


def online(env=None, cwd=None, out=sys.stdout, err=sys.stderr):
    env = os.environ if env is None else env
    removed = fence.remove_override(env)
    out.write("wavecoord fence: offline override %s\n" % ("removed" if removed else "was not set"))
    left = len(fence.journal_entries(env))
    if left:
        agent = (env.get("WAVECOORD_AGENT_ID") or "").strip() or "human"
        client = Client.from_env(agent=agent, deadline=Deadline(20), env=env, cwd=cwd or os.getcwd())
        if client is not None:
            left = fence.journal_flush(client, env, agent)
        out.write("wavecoord fence: %d logged override write(s) not sent yet\n" % left if left else
                  "wavecoord fence: the override log is sent to the coordinator\n")
    return 0


def status(env=None, cwd=None, out=sys.stdout):
    env = os.environ if env is None else env
    cwd = cwd or os.getcwd()
    ovr = fence.read_override(env)
    url, up = coord_answers(env, cwd)
    d = {"modes": fence.modes(env, cwd), "override": ovr, "override_path": fence.override_path(env),
         "journal": len(fence.journal_entries(env)), "coord_url": url, "coord_answers": up,
         "env_offline_ignored": bool((env.get("WAVECOORD_FENCE_OFFLINE") or "").strip())}
    out.write(json.dumps(d, indent=1, sort_keys=True) + "\n")
    return 0


def cmd_fence(args):
    if args.fence_cmd == "offline":
        return offline(args.duration, args.reason)
    if args.fence_cmd == "online":
        return online()
    return status()


def cmd_gate_check(args):
    return gatecheck.run(args.target, args.task, args.attempt, args.range, args.pr_body, args.lease, args.repo,
                         as_json=args.json)


def register(sub):
    sp = sub.add_parser("fence", help="the edit fence: offline override (human only) and status")
    fs = sp.add_subparsers(dest="fence_cmd", metavar="<offline|online|status>")
    fs.required = True
    p = fs.add_parser("offline", help="human only: let edit and commit checks pass while the coordinator is down")
    p.add_argument("--for", dest="duration", required=True, help="duration: <n>s, <n>m or <n>h (max 4h)")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_fence)
    p = fs.add_parser("online", help="remove the offline override and send its log")
    p.set_defaults(func=cmd_fence)
    p = fs.add_parser("status", help="fence modes, override and log")
    p.set_defaults(func=cmd_fence)

    sp = sub.add_parser("gate-check", help="the fence merge check for a PR or a range (exit 0 pass, 1 fail, 3 error)")
    sp.add_argument("target", nargs="?", help="a PR number or URL, or a range BASE..HEAD")
    sp.add_argument("--task")
    sp.add_argument("--attempt", type=int)
    sp.add_argument("--range", help="BASE..HEAD (net diff from the merge base)")
    sp.add_argument("--pr-body", help="a file with the PR body (Task:, Attempt:, Lease: lines)")
    sp.add_argument("--lease", help="<task>@<epoch>, when there is no PR body")
    sp.add_argument("--repo", help="the git repo (default: the current directory)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_gate_check)
