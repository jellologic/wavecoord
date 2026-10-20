#!/usr/bin/env python3
"""The course forge: a local stand-in for GitHub, with the `gh` commands wavecoord and the lessons use.

The course demos have a bare git repo as `origin` instead of a GitHub repo. This script answers as `gh`
does for the pull requests of that origin, so the default `pr_cmd`, `merge_cmd` and `pr_state_cmd` work
unchanged and the lessons show the same commands you type with GitHub. `demos/bin/gh` runs it.

  gh --version | gh auth status
  gh pr create --head B --base main --title T (--body-file F | --body TEXT)
  gh pr view <n|url|branch> [--json fields]     JSON as gh prints it, or a short summary
  gh pr edit <n|url|branch> --body-file F
  gh pr merge <n|url|branch> [--merge|--squash] [--match-head-commit SHA] [--delete-branch]
  gh pr list [--state open|merged|closed|all]
  gh pr diff <n|url|branch>
  gh pr close <n|url|branch>

The pull requests are kept in `<origin>.forge.json` next to the bare origin ($COURSE_FORGE_STATE overrides
it). A merge clones origin into a temp dir, merges the head with `git merge --no-ff` and pushes the base.
Stdlib only, Python >= 3.9.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

VERSION = "gh version 2.0.0 (course forge: a local stand-in for GitHub)"
URL = "http://forge.localhost/%s/pull/%d"     # .localhost never leaves this machine


class ForgeError(Exception):
    pass


def git(args, cwd, check=True):
    p = subprocess.run(["git"] + list(args), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True)
    if check and p.returncode != 0:
        raise ForgeError("git %s: %s" % (" ".join(args), (p.stderr or p.stdout).strip()))
    return p.stdout.strip()


def origin_of(cwd):
    url = git(["remote", "get-url", "origin"], cwd, check=False)
    if not url:
        raise ForgeError("this repo has no origin remote")
    if url.startswith("file://"):
        url = url[len("file://"):]
    if not os.path.isdir(url):
        raise ForgeError("origin %s is not a local repo: the course forge serves local demos only" % url)
    return os.path.abspath(url)


def state_path(origin):
    return os.environ.get("COURSE_FORGE_STATE") or origin.rstrip("/") + ".forge.json"


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"prs": []}


def save(path, st):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, path)


def opt(args, name, default=None):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default


def repo_name(origin):
    base = os.path.basename(origin.rstrip("/"))
    return base[:-4] if base.endswith(".git") else base


def find(st, ref):
    """A PR number, a PR URL, or a head branch (its latest PR) -> the PR record."""
    ref = str(ref).rstrip("/")
    n = None
    if ref.isdigit():
        n = int(ref)
    elif "/pull/" in ref:
        n = int(ref.rsplit("/", 1)[-1])
    else:
        hits = [p["number"] for p in st["prs"] if p["head"] == ref]
        n = hits[-1] if hits else None
    if n is None or not 1 <= n <= len(st["prs"]):
        raise ForgeError("no pull requests found for %s" % ref)
    return st["prs"][n - 1]


def head_sha(origin, branch):
    out = git(["ls-remote", origin, "refs/heads/" + branch], origin, check=False).split()
    return out[0] if out else ""


def view_json(origin, pr):
    sha = pr.get("merged_head") or (head_sha(origin, pr["head"]) if pr["state"] == "open" else pr.get("sha", ""))
    return {"number": pr["number"], "title": pr["title"], "body": pr["body"], "baseRefName": pr["base"],
            "headRefName": pr["head"], "headRefOid": sha, "state": pr["state"].upper(), "url": pr["url"],
            "mergedAt": pr.get("merged_at")}


def changed_files(origin, pr):
    base = pr.get("merge_base") or pr["base"]
    head = pr.get("merged_head") or pr["head"]
    return git(["diff", "--stat", "%s...%s" % (base, head)], origin, check=False)


def cmd_create(st, origin, args):
    head, base, title = opt(args, "--head"), opt(args, "--base", "main"), opt(args, "--title", "")
    if not head:
        raise ForgeError("pr create needs --head")
    if not head_sha(origin, head):
        raise ForgeError("branch %s is not on origin: push it first" % head)
    body = opt(args, "--body", "")
    if opt(args, "--body-file"):
        with open(opt(args, "--body-file")) as f:
            body = f.read()
    dup = [p for p in st["prs"] if p["head"] == head and p["base"] == base and p["state"] == "open"]
    if dup:
        sys.stderr.write('a pull request for branch "%s" into branch "%s" already exists:\n%s\n'
                         % (head, base, dup[-1]["url"]))
        return 1
    n = len(st["prs"]) + 1
    url = URL % (repo_name(origin), n)
    st["prs"].append({"number": n, "head": head, "base": base, "title": title, "body": body, "state": "open",
                      "url": url, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    print(url)
    return 0


def cmd_view(st, origin, args):
    pr = find(st, args[0])
    data = view_json(origin, pr)
    if "--json" in args:
        fields = [f for f in (opt(args, "--json") or "").split(",") if f]
        print(json.dumps({k: v for k, v in data.items() if not fields or k in fields}))
        return 0
    print("%s #%d" % (pr["title"], pr["number"]))
    print("%s: %s into %s" % (data["state"], pr["head"], pr["base"]))
    print(pr["url"])
    print("")
    print(pr["body"].strip() or "(no description)")
    stat = changed_files(origin, pr)
    if stat:
        print("")
        print(stat)
    return 0


def cmd_edit(st, origin, args):
    pr = find(st, args[0])
    if opt(args, "--body-file"):
        with open(opt(args, "--body-file")) as f:
            pr["body"] = f.read()
    elif opt(args, "--body") is not None:
        pr["body"] = opt(args, "--body")
    if opt(args, "--title"):
        pr["title"] = opt(args, "--title")
    print(pr["url"])
    return 0


def cmd_merge(st, origin, args):
    pr = find(st, args[0])
    if pr["state"] != "open":
        raise ForgeError("pull request #%d is %s" % (pr["number"], pr["state"]))
    sha = head_sha(origin, pr["head"])
    if not sha:
        raise ForgeError("branch %s is gone from origin" % pr["head"])
    want = opt(args, "--match-head-commit")
    if want and want != sha:
        raise ForgeError("head of %s is %s, not %s" % (pr["head"], sha[:12], want[:12]))
    tmp = tempfile.mkdtemp(prefix="course-forge-")
    try:
        git(["clone", "-q", "-b", pr["base"], origin, tmp], origin)
        for k, v in (("user.name", "course forge"), ("user.email", "forge@example.invalid")):
            git(["config", k, v], tmp)
        base_before = git(["rev-parse", "HEAD"], tmp)
        git(["fetch", "-q", "origin", pr["head"]], tmp)
        if "--squash" in args:
            p = subprocess.run(["git", "merge", "--squash", "-q", "FETCH_HEAD"], cwd=tmp, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, universal_newlines=True)
            if p.returncode == 0:
                git(["commit", "-q", "-m", "%s (#%d)" % (pr["title"], pr["number"])], tmp)
        else:
            p = subprocess.run(["git", "merge", "--no-ff", "-q", "-m", "Merge pull request #%d from %s\n\n%s"
                                % (pr["number"], pr["head"], pr["title"]), "FETCH_HEAD"], cwd=tmp,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if p.returncode != 0:
            raise ForgeError("pull request #%d has a merge conflict with %s" % (pr["number"], pr["base"]))
        git(["push", "-q", "origin", "HEAD:" + pr["base"]], tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    pr.update(state="merged", merged_head=sha, merge_base=base_before,
              merged_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    if "--delete-branch" in args:
        git(["push", "-q", origin, "--delete", pr["head"]], origin, check=False)
    print("Merged pull request #%d (%s)" % (pr["number"], pr["title"]))
    return 0


def cmd_list(st, origin, args):
    want = opt(args, "--state", "open")
    rows = [p for p in st["prs"] if want == "all" or p["state"] == want]
    if not rows:
        print("no %spull requests" % ("" if want == "all" else want + " "))
        return 0
    for p in reversed(rows):
        print("#%-3d %-50s %-28s %s" % (p["number"], p["title"][:50], p["head"][:28], p["state"].upper()))
    return 0


def cmd_diff(st, origin, args):
    pr = find(st, args[0])
    base = pr.get("merge_base") or pr["base"]
    head = pr.get("merged_head") or pr["head"]
    sys.stdout.write(git(["diff", "%s...%s" % (base, head)], origin, check=False) + "\n")
    return 0


def cmd_close(st, origin, args):
    pr = find(st, args[0])
    if pr["state"] == "open":
        pr["state"] = "closed"
    print("Closed pull request #%d (%s)" % (pr["number"], pr["title"]))
    return 0


PR_CMDS = {"create": cmd_create, "view": cmd_view, "edit": cmd_edit, "merge": cmd_merge, "list": cmd_list,
           "diff": cmd_diff, "close": cmd_close}


def main(argv, cwd=None):
    cwd = cwd or os.getcwd()
    if argv[:1] == ["--version"]:
        print(VERSION)
        return 0
    if argv[:2] == ["auth", "status"]:
        print("Logged in to the course forge, a local stand-in for GitHub")
        return 0
    if len(argv) < 2 or argv[0] != "pr" or argv[1] not in PR_CMDS:
        sys.stderr.write("course forge: unsupported: gh %s\n" % " ".join(argv))
        return 2
    if argv[1] not in ("create", "list") and len(argv) < 3:
        sys.stderr.write("course forge: gh pr %s needs a PR number, URL or branch\n" % argv[1])
        return 2
    try:
        origin = origin_of(cwd)
        path = state_path(origin)
        st = load(path)
        rc = PR_CMDS[argv[1]](st, origin, argv[2:])
        save(path, st)
        return rc
    except ForgeError as e:
        sys.stderr.write("%s\n" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
