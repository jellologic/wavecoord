"""Git guards: `wavecoord guard install` and `wavecoord guard check`. Stdlib only.

`install` points the worktree's own hooks path (per-worktree git config) at a
directory with `pre-commit` and `pre-push` scripts. Each script first runs the
hook of the same name that was active before (the chain), then
`wavecoord guard check`. In P2 the check is advisory: it prints a warning and
exits 0. `--enforce` makes a reported violation fail the commit or push; the
fail-closed rules for an unreachable coordinator come with the fence in P6.
"""

import os
import shlex
import stat
import subprocess
import sys

from wavecoord.client import Client, Deadline, Unreachable

HOOK_NAMES = ("pre-commit", "pre-push")
HOOKS_DIRNAME = "wavecoord-hooks"
MARK = "# wavecoord guard (generated; do not edit)"
CHECK_BUDGET_S = 5.0
ZERO_SHA = "0" * 40


def _git(cwd, *args, check=False):
    p = subprocess.run(["git"] + list(args), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True)
    if check and p.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args), p.stderr.strip()))
    return p.stdout.strip() if p.returncode == 0 else None


def self_command():
    """The shell words that run this wavecoord build."""
    exe = sys.executable or "python3"
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and not argv0.endswith(".py"):
        return [exe, argv0]  # the zipapp
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return ["env", "PYTHONPATH=" + src, exe, "-m", "wavecoord"]


def _needed_path(cmd):
    """The file or directory the command needs (the zipapp, or the source dir)."""
    for w in cmd:
        if w.startswith("PYTHONPATH="):
            return w[len("PYTHONPATH="):]
    return cmd[-1] if len(cmd) > 1 else None


def _script(name, chain_dir, cmd):
    q = " ".join(shlex.quote(w) for w in cmd)
    chain = shlex.quote(os.path.join(chain_dir, name)) if chain_dir else "''"
    need = _needed_path(cmd)
    # Advisory: when the wavecoord build is gone (for example after a plugin
    # update), run only the chained hook.
    skip = 'have=1; [ -e %s ] || have=0\n' % shlex.quote(need) if need else 'have=1\n'
    if name == "pre-commit":
        body = (
            'chain=%s\n'
            'if [ -n "$chain" ] && [ -x "$chain" ]; then "$chain" "$@" || exit $?; fi\n'
            '%s'
            '[ "$have" = 1 ] || exit 0\n'
            'exec %s guard check --staged $WAVECOORD_GUARD_FLAGS\n' % (chain, skip, q))
    else:
        body = (
            'chain=%s\n'
            'input=$(cat)\n'
            'if [ -n "$chain" ] && [ -x "$chain" ]; then\n'
            '  printf "%%s\\n" "$input" | "$chain" "$@" || exit $?\n'
            'fi\n'
            '%s'
            '[ "$have" = 1 ] || exit 0\n'
            'printf "%%s\\n" "$input" | exec %s guard check --push - $WAVECOORD_GUARD_FLAGS\n' % (chain, skip, q))
    return "#!/bin/sh\n%s\n%s" % (MARK, body)


def install(worktree=None, cmd=None, out=sys.stdout):
    wt = os.path.abspath(worktree or os.getcwd())
    top = _git(wt, "rev-parse", "--show-toplevel", check=True)
    git_dir = _git(top, "rev-parse", "--absolute-git-dir", check=True)
    common = _git(top, "rev-parse", "--git-common-dir", check=True)
    common = common if os.path.isabs(common) else os.path.normpath(os.path.join(top, common))
    hooks_dir = os.path.join(git_dir, HOOKS_DIRNAME)

    prev = _git(top, "config", "--get", "core.hooksPath")
    if prev:
        prev = prev if os.path.isabs(prev) else os.path.normpath(os.path.join(top, prev))
    chain_dir = prev or os.path.join(common, "hooks")
    if os.path.realpath(chain_dir) == os.path.realpath(hooks_dir):
        # Re-install: keep the chain recorded in the existing scripts.
        chain_dir = _read_chain(hooks_dir) or os.path.join(common, "hooks")

    os.makedirs(hooks_dir, exist_ok=True)
    cmd = cmd or self_command()
    for name in HOOK_NAMES:
        p = os.path.join(hooks_dir, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(_script(name, chain_dir, cmd))
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    with open(os.path.join(hooks_dir, "chain"), "w", encoding="utf-8") as f:
        f.write(chain_dir + "\n")

    _git(top, "config", "extensions.worktreeConfig", "true", check=True)
    _git(top, "config", "--worktree", "core.hooksPath", hooks_dir, check=True)
    out.write("wavecoord guard: installed %s (chain: %s)\n" % (hooks_dir, chain_dir))
    return 0


def _read_chain(hooks_dir):
    try:
        with open(os.path.join(hooks_dir, "chain"), encoding="utf-8") as f:
            v = f.read().strip()
        return v or None
    except OSError:
        return None


def staged_paths(cwd):
    out = _git(cwd, "diff", "--cached", "--name-only", "-z")
    return [p for p in (out or "").split("\0") if p]


def push_paths(cwd, spec, stdin):
    """Changed paths for a push. `spec` is a range (a..b) or "-" for the pre-push stdin."""
    cmds = []
    if spec and spec != "-":
        cmds.append(["diff", "--name-only", "-z", spec])
    else:
        for ln in stdin.read().splitlines():
            parts = ln.split()
            if len(parts) != 4 or parts[1] == ZERO_SHA:
                continue  # malformed, or a branch delete
            local_sha, remote_sha = parts[1], parts[3]
            if remote_sha == ZERO_SHA:
                # A new remote branch: the commits that no remote-tracking ref has.
                cmds.append(["log", "--name-only", "-z", "--format=", local_sha, "--not", "--remotes"])
            else:
                cmds.append(["diff", "--name-only", "-z", remote_sha, local_sha])
    paths = set()
    for c in cmds:
        out = _git(cwd, *c)
        paths.update(p for p in (out or "").replace("\n", "\0").split("\0") if p)
    return sorted(paths)


def check(mode, spec=None, enforce=False, cwd=None, env=None, stdin=sys.stdin, err=sys.stderr):
    env = os.environ if env is None else env
    cwd = cwd or os.getcwd()
    try:
        top = _git(cwd, "rev-parse", "--show-toplevel") or cwd
        paths = staged_paths(top) if mode == "staged" else push_paths(top, spec, stdin)
    except Exception:
        return 0
    if not paths:
        return 0
    agent = env.get("WAVECOORD_AGENT_ID", "").strip() or None
    client = Client.from_env(agent=agent, deadline=Deadline(CHECK_BUDGET_S), env=env, cwd=top)
    if client is None:
        return 0  # not configured
    body = {"agent": agent, "paths": paths[:1000], "source": "guard"}
    task = env.get("WAVECOORD_TASK", "").strip()
    if task:
        body["task"] = task
    branch = _git(top, "rev-parse", "--abbrev-ref", "HEAD")
    if branch:
        body["branch"] = branch
    try:
        status, resp = client.post("/api/check", body)
    except Unreachable as e:
        err.write("wavecoord guard: coordinator unreachable (%s); check skipped (advisory)\n" % e)
        return 0
    except Exception:
        return 0
    if status != 200 or not isinstance(resp, dict):
        err.write("wavecoord guard: check failed (HTTP %s); skipped (advisory)\n" % status)
        return 0
    violations = [v for v in resp.get("violations") or [] if isinstance(v, dict)]
    if not violations:
        return 0
    what = "commit" if mode == "staged" else "push"
    err.write("wavecoord guard: this %s touches paths outside your lease or inside another lease:\n" % what)
    for v in violations[:50]:
        err.write("  %s: %s%s\n" % (v.get("path"), v.get("kind"),
                                     " (task %s, %s)" % (v.get("task"), v.get("glob")) if v.get("task") else ""))
    if enforce:
        err.write("wavecoord guard: %s refused (--enforce)\n" % what)
        return 1
    err.write("wavecoord guard: advisory only; the %s goes on\n" % what)
    return 0
