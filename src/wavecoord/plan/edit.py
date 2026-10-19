"""Anchored plan edits (`plan_edit`), committed on a `plan/<n>` branch and opened as a PR.

An edit names a file and an anchor (an exact substring). The anchor must occur
exactly once in the file, or `occurrence` picks the n-th occurrence. A missing
or ambiguous anchor fails the whole call: the edits are applied in memory
first, every changed spec is parsed and validated, and only then is anything
written. The work happens in a fresh detached worktree; the only ref that is
pushed is `refs/heads/plan/<n>`. The default branch is never touched.
"""

import os
import re
import shutil
import subprocess
import tempfile

from wavecoord import schemaload

from .spec import SpecError, load_text

PLAN_BRANCH_RE = re.compile(r"^plan/([A-Za-z0-9._-]{1,64})$")


class EditError(ValueError):
    pass


def edit_file(e, plan_dir):
    """The repo-relative file of one edit."""
    if e.get("path"):
        rel = e["path"]
    elif e.get("id"):
        rel = e["id"] + ".md"
    else:
        raise EditError("an edit needs id or path")
    return "%s/%s" % (plan_dir.strip("/"), rel)


def _find(text, anchor, occurrence, where):
    starts, i = [], text.find(anchor)
    while i >= 0:
        starts.append(i)
        i = text.find(anchor, i + 1)
    if not starts:
        raise EditError("%s: anchor not found: %r" % (where, anchor[:120]))
    if occurrence is None:
        if len(starts) > 1:
            raise EditError("%s: anchor is ambiguous (%d occurrences; give occurrence): %r"
                            % (where, len(starts), anchor[:120]))
        return starts[0]
    if occurrence > len(starts):
        raise EditError("%s: occurrence %d of the anchor asked, only %d found: %r"
                        % (where, occurrence, len(starts), anchor[:120]))
    return starts[occurrence - 1]


def apply_edits(read, edits, plan_dir):
    """Apply `edits` in order, in memory. `read(path)` gives the current text or None.

    -> {path: new text}. Raises EditError (nothing changed) on any problem."""
    errs = schemaload.errors({"edits": edits}, "plan-adapter", "#/definitions/edit_in")
    if errs:
        raise EditError("bad edits: " + "; ".join(errs[:5]))
    files = {}
    for k, e in enumerate(edits):
        path = edit_file(e, plan_dir)
        where = "edit %d (%s)" % (k + 1, path)
        if path not in files:
            files[path] = read(path)
        cur = files[path]
        op = e["op"]
        if op == "create":
            if cur is not None:
                raise EditError("%s: the file exists; use an anchored edit" % where)
            if "anchor" in e:
                raise EditError("%s: create takes no anchor" % where)
            files[path] = e.get("text") or ""
            continue
        if cur is None:
            raise EditError("%s: no such file" % where)
        if "anchor" not in e:
            raise EditError("%s: %s needs an anchor" % (where, op))
        if op != "delete" and "text" not in e:
            raise EditError("%s: %s needs text" % (where, op))
        a = e["anchor"]
        i = _find(cur, a, e.get("occurrence"), where)
        text = e.get("text", "")
        if op == "replace":
            cur = cur[:i] + text + cur[i + len(a):]
        elif op == "insert_before":
            cur = cur[:i] + text + cur[i:]
        elif op == "insert_after":
            cur = cur[:i + len(a)] + text + cur[i + len(a):]
        elif op == "delete":
            cur = cur[:i] + cur[i + len(a):]
        files[path] = cur
    # every resulting spec must still parse and validate
    problems = []
    for path, text in sorted(files.items()):
        base = os.path.basename(path)
        try:
            load_text(text, name=None, expect_id=base[:-3])
        except SpecError as ex:
            problems += ["%s: %s" % (path, m) for m in ex.errors]
    if problems:
        raise EditError("the edited specs are invalid: " + "; ".join(problems[:8]))
    return files


def next_plan_branch(repo, git_run):
    """plan/<n>, n one more than any plan/<number> branch on origin or local."""
    nums = set()
    rc, out, _ = git_run(["ls-remote", "--heads", "origin", "refs/heads/plan/*"], repo, timeout=120)
    rc2, out2, _ = git_run(["for-each-ref", "--format=%(refname)", "refs/heads/plan/", "refs/remotes/origin/plan/"],
                           repo, timeout=60)
    for line in (out if rc == 0 else "").splitlines() + (out2 if rc2 == 0 else "").splitlines():
        m = re.search(r"refs/(?:heads|remotes/origin)/plan/(\d+)$", line.strip())
        if m:
            nums.add(int(m.group(1)))
    return "plan/%d" % ((max(nums) + 1) if nums else 1)


def plan_branch(repo, branch=None):
    """-> (branch, new?, base sha, default branch): a new plan/<n> from the default branch, or an existing one."""
    from wavecoord.launcher.common import git, git_run
    from wavecoord.launcher.worktrees import REMOTE, default_branch, fetch, ref_exists

    fetch(repo)
    default = default_branch(repo)
    new = branch is None
    if new:
        branch = next_plan_branch(repo, git_run)
        base_ref = "%s/%s" % (REMOTE, default)
    else:
        if not PLAN_BRANCH_RE.match(branch or ""):
            raise EditError("a plan branch is plan/<name>, not %r" % branch)
        base_ref = "%s/%s" % (REMOTE, branch)
        if not ref_exists(repo, "refs/remotes/" + base_ref):
            raise EditError("branch %s is not on %s" % (branch, REMOTE))
    if branch in (default, "main", "master") or not PLAN_BRANCH_RE.match(branch):
        raise EditError("refusing to write branch %r" % branch)
    return branch, new, git(["rev-parse", "--verify", base_ref + "^{commit}"], repo), default


def commit_files(repo, base_sha, branch, files, msg, body, wt_root=None, moves=(), author=None):
    """Write `files` ({path: text, or None to delete}) on top of base_sha in a fresh worktree, `git mv` each
    (old, new) of `moves` first, commit once (as `author`, "Name <email>", when given) and push
    refs/heads/<branch> without force. -> the commit sha."""
    from wavecoord.launcher.common import git, git_run
    from wavecoord.launcher.worktrees import REMOTE

    root = wt_root or tempfile.gettempdir()
    os.makedirs(root, exist_ok=True)
    wt = tempfile.mkdtemp(prefix="wavecoord-plan-", dir=root)
    os.rmdir(wt)
    git(["worktree", "add", "--quiet", "--detach", wt, base_sha], repo)
    try:
        moved_from = set()
        for old, new in moves:
            os.makedirs(os.path.dirname(os.path.join(wt, new)), exist_ok=True)
            git(["mv", "--", old, new], wt)
            moved_from.add(old)
        for path, text in sorted(files.items()):
            full = os.path.join(wt, path)
            if text is None:
                if path not in moved_from and os.path.exists(full):
                    git(["rm", "--quiet", "--", path], wt)
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            git(["add", "--", path], wt)
        rc, out, _ = git_run(["diff", "--cached", "--quiet"], wt)
        if rc == 0:
            raise EditError("the edits change nothing")
        git(["commit", "--quiet", "-m", msg, "-m", body] + (["--author", author] if author else []), wt)
        commit = git(["rev-parse", "HEAD"], wt)
        rc, _, err = git_run(["push", "--quiet", REMOTE, "HEAD:refs/heads/" + branch], wt, timeout=600)
        if rc != 0:
            raise EditError("push of %s failed: %s" % (branch, err[-400:]))
    finally:
        git_run(["worktree", "remove", "--force", wt], repo)
        shutil.rmtree(wt, ignore_errors=True)
        git_run(["worktree", "prune"], repo)
    return commit


def read_at(repo, sha):
    def read(path):
        p = subprocess.run(["git", "cat-file", "-p", "%s:%s" % (sha, path)], cwd=repo,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        return p.stdout.decode("utf-8") if p.returncode == 0 else None
    return read


def plan_edit(repo, plan_dir, edits, branch=None, pr=None, title=None, body=None, wt_root=None):
    """Apply anchored edits on a plan branch, commit, push, and open a PR on a new branch.

    `pr(branch, base, title, body)` opens the PR on the client side and returns
    (url, error). -> {branch, commit, pr, pr_error, files, base}."""
    branch, new, base_sha, default = plan_branch(repo, branch)
    files = apply_edits(read_at(repo, base_sha), edits, plan_dir)
    ids = sorted(set(os.path.basename(p)[:-3] for p in files))
    msg = title or "plan: edit %s" % ", ".join(ids)
    commit = commit_files(repo, base_sha, branch, files, msg, body or "Anchored plan edit by wavecoord plan_edit.",
                          wt_root)
    url, perr = None, None
    if new and pr is not None:
        pr_body = (body or "Anchored plan edit by wavecoord plan_edit.") + \
            "\n\nFiles:\n" + "\n".join("- `%s`" % p for p in sorted(files)) + "\n"
        url, perr = pr(branch, default, msg[:100], pr_body)
    return {"branch": branch, "commit": commit, "pr": url, "pr_error": perr, "files": sorted(files),
            "base": default, "new_branch": new}


def spec_edit(repo, spec_dir, ops, branch=None, pr=None, title=None, body=None, wt_root=None):
    """Apply spec tree ops (specs/ops.py) on a plan branch, commit (moves as `git mv`), push, and open a PR on a
    new branch. The ops read the tree as it is on that branch, so several calls with one branch build one PR."""
    from wavecoord.specs import source
    from wavecoord.specs.ops import OpsError, apply_ops

    branch, new, base_sha, default = plan_branch(repo, branch)
    d = source.norm_dir(spec_dir)
    try:
        changes, moves, problems = apply_ops(source.read_ref(repo, base_sha, d), ops)
    except OpsError as e:
        raise EditError("; ".join(e.errors[:8]))
    files = {"%s/%s" % (d, p): t for p, t in changes.items()}
    mv = [("%s/%s" % (d, a), "%s/%s" % (d, b)) for a, b in moves]
    msg = title or "spec: %s" % ", ".join(sorted({op.get("id") or op.get("from") or op.get("task") or op.get("new")
                                                  for op in ops}))[:90]
    warnings = ["%s %s: %s" % (p.rule, p.path, p.msg) for p in problems if p.level == "warning"]
    text = body or "Spec tree change by wavecoord spec_edit."
    commit = commit_files(repo, base_sha, branch, files, msg, text, wt_root, moves=mv)
    url, perr = None, None
    if new and pr is not None:
        pr_body = text + "\n\nFiles:\n" + "\n".join("- `%s`%s" % (p, " (deleted)" if t is None else "")
                                                    for p, t in sorted(files.items()))
        if mv:
            pr_body += "\n\nMoves:\n" + "\n".join("- `%s` -> `%s`" % m for m in mv)
        pr_body += "\n\n`wavecoord spec check`: no errors%s\n" % (
            "; warnings:\n" + "\n".join("- " + w for w in warnings[:30]) if warnings else "")
        url, perr = pr(branch, default, msg[:100], pr_body)
    return {"branch": branch, "commit": commit, "pr": url, "pr_error": perr, "files": sorted(files),
            "moves": mv, "warnings": warnings, "base": default, "new_branch": new}
