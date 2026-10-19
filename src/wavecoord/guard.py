"""Git guards: `wavecoord guard install` and `wavecoord guard check`. Stdlib only.

`install` points the worktree's own hooks path (per-worktree git config) at a
directory with `pre-commit`, `pre-merge-commit` and `pre-push` scripts. Each
script first runs the hook of the same name that was active before (the chain),
then `wavecoord guard check`. The check is the fence point E2 (pre-commit, and
pre-merge-commit for a merge that makes a commit) and a second check after E3
(pre-push), docs/design/edit-fence.md. Git runs no commit hook for a
cherry-pick, a rebase, `git am` or a fast-forward merge: E3 and E5 check those
commits (they diff the net change of the branch). Advisory by
default; in enforce mode (`--enforce`, or the fence mode that the launcher
recorded at install) it fails closed: a violation and every failure refuse.

`install` also writes `<git dir>/wavecoord-hooks/identity.json` with the
agent, task and epoch from the launcher env. The env of a `git commit` that a
worker starts with Bash is not a reliable source, so `check` reads the file.
"""

import json
import os
import shlex
import stat
import subprocess
import sys

from wavecoord import fence
from wavecoord.client import Client, Deadline

HOOK_NAMES = ("pre-commit", "pre-merge-commit", "pre-push")
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


def _script(name, chain_dir, cmd, enforce=False):
    q = " ".join(shlex.quote(w) for w in cmd)
    chain = shlex.quote(os.path.join(chain_dir, name)) if chain_dir else "''"
    need = _needed_path(cmd)
    # Advisory: when the wavecoord build is gone (for example after a plugin
    # update), run only the chained hook. Enforce: refuse.
    skip = 'have=1; [ -e %s ] || have=0\n' % shlex.quote(need) if need else 'have=1\n'
    gone = ('[ "$have" = 1 ] || { echo "wavecoord guard: the wavecoord build is missing; refused (fence'
            ' enforce)" >&2; exit 1; }\n') if enforce else '[ "$have" = 1 ] || exit 0\n'
    if name != "pre-push":  # pre-commit, pre-merge-commit: the staged check
        body = (
            'chain=%s\n'
            'if [ -n "$chain" ] && [ -x "$chain" ]; then "$chain" "$@" || exit $?; fi\n'
            '%s'
            '%s'
            'exec %s guard check --staged $WAVECOORD_GUARD_FLAGS\n' % (chain, skip, gone, q))
    else:
        body = (
            'chain=%s\n'
            'input=$(cat)\n'
            'if [ -n "$chain" ] && [ -x "$chain" ]; then\n'
            '  printf "%%s\\n" "$input" | "$chain" "$@" || exit $?\n'
            'fi\n'
            '%s'
            '%s'
            'printf "%%s\\n" "$input" | exec %s guard check --push - $WAVECOORD_GUARD_FLAGS\n' % (chain, skip, gone, q))
    return "#!/bin/sh\n%s\n%s" % (MARK, body)


IDENTITY_NAME = "identity.json"


def write_identity(hooks_dir, env):
    """identity.json from the launcher env, or nothing when the env names no agent."""
    p = os.path.join(hooks_dir, IDENTITY_NAME)
    agent = (env.get("WAVECOORD_AGENT_ID") or "").strip()
    if not agent:
        try:
            os.remove(p)
        except OSError:
            pass
        return None
    ident = {"agent": agent, "task": (env.get("WAVECOORD_TASK") or "").strip() or None,
             "epoch": fence._epoch_from_env(env), "lease_id": _int_or_none(env.get("WAVECOORD_LEASE_ID")),
             "attempt": (env.get("WAVECOORD_ATTEMPT") or "").strip() or None,
             "worktree": (env.get("WAVECOORD_WORKTREE") or "").strip() or None,
             "fence": fence.launcher_modes(env)}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(ident, f, sort_keys=True)
    return ident


def _int_or_none(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def read_identity(top):
    """The identity that `guard install` recorded for the worktree at `top`, or None."""
    gd = _git(top, "rev-parse", "--absolute-git-dir")
    if not gd:
        return None
    try:
        with open(os.path.join(gd, HOOKS_DIRNAME, IDENTITY_NAME), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get("agent") else None
    except (OSError, ValueError):
        return None


def install(worktree=None, cmd=None, out=sys.stdout, env=None):
    env = os.environ if env is None else env
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
    ident = write_identity(hooks_dir, env)
    m = fence.modes(dict(env, WAVECOORD_FENCE=json.dumps((ident or {}).get("fence"))) if (ident or {}).get("fence")
                    else env, top)
    for name in HOOK_NAMES:
        p = os.path.join(hooks_dir, name)
        point = "push" if name == "pre-push" else "commit"
        with open(p, "w", encoding="utf-8") as f:
            f.write(_script(name, chain_dir, cmd, enforce=m[point] == "enforce"))
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
    """The staged paths, renames split into a delete and an add (--no-renames). None on a git error."""
    return fence.diff_paths(cwd, "--cached")


def _has_commit(cwd, sha):
    return _git(cwd, "cat-file", "-e", sha + "^{commit}") is not None


def push_paths(cwd, spec, stdin):
    """Changed paths for a push. `spec` is a range (a..b) or "-" for the pre-push stdin. None when git cannot
    compute them (the caller refuses in enforce mode)."""
    cmds = []
    if spec and spec != "-":
        cmds.append(["diff", "--name-only"] + fence.DIFF_RENAME_FLAGS + ["-z", spec])
    else:
        for ln in stdin.read().splitlines():
            parts = ln.split()
            if len(parts) != 4 or parts[1] == ZERO_SHA:
                continue  # malformed, or a branch delete
            local_sha, remote_sha = parts[1], parts[3]
            if remote_sha == ZERO_SHA or not _has_commit(cwd, remote_sha):
                # A new remote branch, or a remote head this repo does not have (a forced push over a remote
                # branch that moved since the last fetch): the commits that no remote-tracking ref has. -m with
                # --first-parent: a merge commit lists what it brings in (git log shows no paths for it otherwise).
                cmds.append(["log", "-m", "--first-parent", "--name-only"] + fence.DIFF_RENAME_FLAGS +
                            ["-z", "--format=", local_sha, "--not", "--remotes"])
            else:
                cmds.append(["diff", "--name-only"] + fence.DIFF_RENAME_FLAGS + ["-z", remote_sha, local_sha])
    paths = set()
    for c in cmds:
        out = _git(cwd, *(fence.DIFF_GIT_CONFIG + c))
        if out is None:
            return None  # a git error is never "no paths"
        paths.update(p for p in out.replace("\n", "\0").split("\0") if p)
    return sorted(paths)


def check(mode, spec=None, enforce=False, cwd=None, env=None, stdin=sys.stdin, err=sys.stderr):
    """E2 (staged) or the pre-push guard. -> exit code: 0 pass, 1 refuse."""
    env = os.environ if env is None else env
    cwd = cwd or os.getcwd()
    what = "commit" if mode == "staged" else "push"
    top = _git(cwd, "rev-parse", "--show-toplevel") or cwd
    ident = read_identity(top)
    menv = dict(env, WAVECOORD_FENCE=json.dumps(ident["fence"])) if (ident or {}).get("fence") else env
    m = fence.modes(menv, top)
    enforce = enforce or m["commit" if mode == "staged" else "push"] == "enforce"
    if m["commit" if mode == "staged" else "push"] == "off" and not enforce:
        return 0

    def refuse(why):
        if enforce:
            err.write("wavecoord guard: %s refused (fence enforce): %s\n" % (what, why))
            return 1
        err.write("wavecoord guard: %s; check skipped (advisory)\n" % why)
        return 0

    try:
        paths = staged_paths(top) if mode == "staged" else push_paths(top, spec, stdin)
    except Exception as e:  # noqa: BLE001
        return refuse("cannot read the changed paths (%s)" % e)
    if paths is None:
        return refuse("cannot read the changed paths (git diff failed)")
    if not paths:
        return 0
    if ident:
        agent, task, epoch = ident.get("agent"), ident.get("task"), ident.get("epoch")
    else:
        agent = env.get("WAVECOORD_AGENT_ID", "").strip() or None
        task = env.get("WAVECOORD_TASK", "").strip() or None
        epoch = fence._epoch_from_env(env)
    client = Client.from_env(agent=agent, deadline=Deadline(CHECK_BUDGET_S), env=env, cwd=top)
    if client is None:
        if ident and enforce:
            return refuse("no coordinator URL is configured")
        return 0  # not configured
    try:
        r = fence.check_paths(client, agent, paths, task, epoch, source="guard",
                              fold_case=fence.case_insensitive(os.path.realpath(top)),
                              extra={"branch": _git(top, "rev-parse", "--abbrev-ref", "HEAD")}
                              if _git(top, "rev-parse", "--abbrev-ref", "HEAD") else None)
    except Exception as e:  # noqa: BLE001
        return refuse("check failed (%s)" % e)
    if not r.ok:
        if r.fail == "unavailable" and task:
            ovr = fence.override_active(env, top)
            if ovr is not None and mode == "staged":
                for p in paths:
                    fence.journal_append(env, ovr, agent, task, epoch, p, "commit")
                err.write("wavecoord guard: coordinator unavailable; the commit goes on under the offline override"
                          " (%s) and is logged\n" % ovr.get("reason"))
                return 0
        if r.fail == "unavailable":
            return refuse("coordinator unreachable (%s)" % (r.detail or "no answer"))
        return refuse(fence.describe(r.fail, "the %s" % what, detail=r.detail))
    violations = r.violations
    if not violations:
        return 0
    err.write("wavecoord guard: this %s touches paths outside your lease or inside another lease:\n" % what)
    for v in violations[:50]:
        err.write("  %s\n" % fence.violation_line(v))
    if any(v.get("kind") == "outside_lease" for v in violations) and ident and ident.get("lease_id"):
        err.write("wavecoord guard: checked as agent %s, lease #%s, epoch %s (recorded at `guard install`)\n"
                  % (agent, ident.get("lease_id"), epoch))
    if enforce:
        err.write("wavecoord guard: %s refused (--enforce)\n" % what)
        return 1
    err.write("wavecoord guard: advisory only; the %s goes on\n" % what)
    return 0
