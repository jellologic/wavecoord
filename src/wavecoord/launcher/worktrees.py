"""Host side: the worktree-admin lock, fetch with retries, the default branch, attempt worktrees."""

import os
import re
import time

from .common import Flock, LauncherError, PROTECTED_BRANCHES, git, git_run, wc_home, ensure_dir

REMOTE = "origin"
FETCH_RETRY_DELAYS_S = (2, 5)


class AdminLock(object):
    """Mutex for worktree add/remove/prune and fetch on one repo.

    `git worktree prune` running while another `git worktree add` is mid-flight
    corrupts the new entry, so every worktree mutation and every fetch of the
    repo goes through this lock. It lives in the git common dir, so every
    wavecoord home that uses the repo shares it; a second lock in the
    wavecoord home covers a repo whose common dir is read-only.
    """

    def __init__(self, repo):
        self.locks = []
        rc, out, _ = git_run(["rev-parse", "--git-common-dir"], repo, timeout=60)
        if rc == 0 and out:
            common = os.path.abspath(os.path.join(repo, out))
            if os.path.isdir(common) and os.access(common, os.W_OK):
                self.locks.append(Flock(os.path.join(common, "wavecoord-wt-admin.lock")))
        key = re.sub(r"[^A-Za-z0-9]+", "_", os.path.abspath(repo)).strip("_")[-80:]
        self.locks.append(Flock(os.path.join(ensure_dir(os.path.join(wc_home(), "locks")),
                                             "wt-admin-%s.lock" % key)))

    def __enter__(self):
        held = []
        try:
            for lk in self.locks:
                lk.__enter__()
                held.append(lk)
        except BaseException:
            for lk in reversed(held):
                lk.__exit__()
            raise
        return self

    def __exit__(self, *a):
        for lk in reversed(self.locks):
            lk.__exit__()


def fetch(repo, sleep=time.sleep):
    """`git fetch origin --prune` under the admin lock, with two retries.

    Other git users of the repo do not take wavecoord's lock: a ref lock held by
    them, or a transient network error, fails a fetch once. A `bad object`
    error is never transient and is not retried."""
    args = ["fetch", REMOTE, "--prune", "--quiet"]
    with AdminLock(repo):
        git_run(["worktree", "prune"], repo)
        rc, out, err = git_run(args, repo, timeout=600)
        for delay in FETCH_RETRY_DELAYS_S:
            if rc == 0 or "bad object" in (err or ""):
                break
            sleep(delay)
            rc, out, err = git_run(args, repo, timeout=600)
    if rc != 0:
        raise LauncherError("git %s failed in %s: %s" % (" ".join(args), repo, (err or out)[-800:]))


def default_branch(repo):
    """The remote's default branch name (origin/HEAD), else main or master."""
    rc, out, _ = git_run(["symbolic-ref", "--quiet", "--short", "refs/remotes/%s/HEAD" % REMOTE], repo)
    if rc == 0 and out.startswith(REMOTE + "/"):
        return out[len(REMOTE) + 1:]
    rc, out, _ = git_run(["ls-remote", "--symref", REMOTE, "HEAD"], repo, timeout=120)
    if rc == 0:
        m = re.search(r"^ref: refs/heads/(\S+)\s+HEAD", out, re.M)
        if m:
            git_run(["remote", "set-head", REMOTE, m.group(1)], repo)
            return m.group(1)
    for cand in ("main", "master"):
        if ref_exists(repo, "refs/remotes/%s/%s" % (REMOTE, cand)):
            return cand
    raise LauncherError("cannot find the default branch of %s in %s" % (REMOTE, repo))


def ref_exists(repo, ref):
    return git_run(["show-ref", "--verify", "--quiet", ref], repo)[0] == 0


def worktree_of_branch(repo, branch):
    out = git(["worktree", "list", "--porcelain"], repo)
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == "branch refs/heads/" + branch:
            return path
    return None


def check_branch(branch, default, log_branch=None):
    """Refuse a protected, default, log or malformed branch name."""
    if not branch or branch.startswith("-") or branch in PROTECTED_BRANCHES or branch == default \
            or (log_branch and branch == log_branch):
        raise LauncherError("refusing branch %r (protected, default or invalid)" % branch)
    rc, _, _ = git_run(["check-ref-format", "--branch", branch], "/")
    if rc != 0:
        raise LauncherError("invalid branch name %r" % branch)


def worktree_path(repo, attempt_id):
    return os.path.normpath(os.path.join(repo, "..", "wt", attempt_id))


def prepare_worktree(repo, branch, attempt_id, cont=False, log_branch=None):
    """Fetch, then create the attempt worktree. -> (worktree, base_sha, default_branch).

    New attempt: branch `branch` from origin/<default>; the branch must not exist.
    --continue: the existing branch (local, else from origin), fast-forwarded to origin."""
    if not os.path.isdir(repo):
        raise LauncherError("repo_path %s does not exist" % repo)
    fetch(repo)
    default = default_branch(repo)
    check_branch(branch, default, log_branch)
    wt = worktree_path(repo, attempt_id)
    ensure_dir(os.path.dirname(wt))
    with AdminLock(repo):
        co = worktree_of_branch(repo, branch)
        if cont and co and os.path.realpath(co) == os.path.realpath(wt):
            # --continue on the host of the earlier run: reuse its worktree as it is
            base_sha = git(["rev-parse", "HEAD"], wt)
            return wt, base_sha, default
        if os.path.exists(wt):
            raise LauncherError("worktree %s already exists" % wt)
        if co:
            raise LauncherError("branch %s is already checked out at %s" % (branch, co))
        local = ref_exists(repo, "refs/heads/" + branch)
        remote = ref_exists(repo, "refs/remotes/%s/%s" % (REMOTE, branch))
        if cont:
            if local:
                git(["worktree", "add", "--quiet", wt, branch], repo)
                if remote:
                    rc, _, err = git_run(["merge", "--ff-only", "--quiet", "%s/%s" % (REMOTE, branch)], wt)
                    if rc != 0:
                        raise LauncherError("local branch %s diverged from %s/%s: %s"
                                            % (branch, REMOTE, branch, err[-300:]))
            elif remote:
                git(["worktree", "add", "--quiet", "--no-track", "-b", branch, wt,
                     "%s/%s" % (REMOTE, branch)], repo)
            else:
                raise LauncherError("--continue: branch %s exists neither locally nor on %s" % (branch, REMOTE))
            base_sha = git(["rev-parse", "HEAD"], wt)
        else:
            if local or remote:
                raise LauncherError("branch %s already exists (%s); use --continue or a new attempt"
                                    % (branch, "on %s" % REMOTE if remote else "locally"))
            base_sha = git(["rev-parse", "--verify", "%s/%s^{commit}" % (REMOTE, default)], repo)
            git(["worktree", "add", "--quiet", "--no-track", "-b", branch, wt, base_sha], repo)
    return wt, base_sha, default


def remove_worktree(repo, wt, branch=None):
    with AdminLock(repo):
        rc, _, err = git_run(["worktree", "remove", "--force", wt], repo)
        if rc != 0 and os.path.exists(wt):
            raise LauncherError("git worktree remove %s: %s" % (wt, err[-300:]))
        git_run(["worktree", "prune"], repo)
        if branch:
            git_run(["branch", "-D", branch], repo)


def unpushed_commits(wt):
    """Commits on HEAD that no remote-tracking ref has. None when unknown."""
    rc, out, _ = git_run(["rev-list", "HEAD", "--not", "--remotes"], wt)
    if rc != 0:
        return None
    return [l for l in out.splitlines() if l.strip()]


def dirty_count(wt):
    rc, out, _ = git_run(["status", "--porcelain"], wt)
    if rc != 0:
        return None
    return len([l for l in out.splitlines() if l.strip()])


def merge_tree_conflicts(wt, base_ref, head="HEAD"):
    """Files that conflict when `head` merges into `base_ref` ([] = clean).

    `git merge-tree --write-tree` (git >= 2.38) merges in memory and touches no worktree."""
    rc, out, err = git_run(["merge-tree", "--write-tree", "--name-only", "--no-messages", base_ref, head], wt)
    if rc == 0:
        return []
    if rc == 1:
        return [l for l in out.splitlines()[1:] if l.strip()] or ["(unknown files)"]
    raise LauncherError("git merge-tree failed (git >= 2.38 is needed): %s" % err[-300:])
