"""`wavecoord gate-check`: the fence merge check E5 (docs/design/edit-fence.md, section 3.5).

    wavecoord gate-check 42                          # a PR number (or URL): gh reads its body and head
    wavecoord gate-check origin/main..HEAD --pr-body body.md
    wavecoord gate-check --task api-auth --attempt 2 --lease api-auth@1792324800000 --range origin/main..HEAD

It reads the attempt's lease from the server and compares it with the `Lease:`
line of the PR body; it computes the net diff (merge-base..head, --no-renames)
itself with git; it checks every path with `source: "gate"`. No override has
any effect here. Exit codes: 0 pass, 1 violation (or a lease mismatch), 3 the
coordinator is unavailable or any other error. A gate must treat every exit
other than 0 as "do not merge".

A task PR (it has `Task:`, `Attempt:` and `Lease:` lines: the work of a worker
attempt) must not change the lead's config: the `.claude/wavecoord.json` keys
in CONFIG_KEYS and HOST_KEYS, the hook keys of the `.claude` settings files, or
a plugin `hooks/hooks.json`. Each such change is a violation of kind
`config_change` (edit-fence.md, section 8): it fails the check in the merge
modes advisory and enforce, and is only reported in mode off. Plan PRs and
lead PRs have no such lines, so this check does not apply to them.
"""

import json
import os
import re
import subprocess
import sys

from wavecoord import fence
from wavecoord.client import Client, Deadline

PASS_EXIT = 0
VIOLATION_EXIT = 1
UNAVAILABLE_EXIT = 3
BUDGET_S = 60.0

LEASE_RE = re.compile(r"^Lease:[ \t]*([A-Za-z0-9][A-Za-z0-9._-]{0,63})@([0-9]+)[ \t]*$", re.M)
TASK_RE = re.compile(r"^Task:[ \t]*([A-Za-z0-9][A-Za-z0-9._-]{0,63})[ \t]*$", re.M)
ATTEMPT_RE = re.compile(r"^Attempt:[ \t]*([0-9]+)[ \t]*$", re.M)


# The lead's config: a change to these is a lead decision in a plan or config PR, never in a task PR. It is
# every key that the lead or a host runs, or that decides where things run, connect or merge. WORKER_KEYS are
# the only other keys of config.schema.json (a test checks that each schema key is in one of the lists).
CONFIG_PATH = ".claude/wavecoord.json"
CONFIG_KEYS = ("fence", "merge_cmd", "check_cmd", "protected_paths_cmd", "providers", "auto_steer", "spec", "plan",
               "pr_cmd", "pr_state_cmd", "coord_url", "project", "log_branch", "default_provider", "auto_resolve",
               "branch_prefix")
HOST_KEYS = ("claude_flags", "env", "ssh", "repo_path", "prepare_cmd", "providers", "labels", "dispatch")
WORKER_KEYS = ("$schema", "wip_cap")
WORKER_HOST_KEYS = ("name", "slots")
SETTINGS_PATHS = (".claude/settings.json", ".claude/settings.local.json")
SETTINGS_KEYS = ("hooks", "disableAllHooks", "enabledPlugins", "extraKnownMarketplaces", "env")
HOOKS_FILE = "hooks/hooks.json"
_BAD = object()


class GateError(Exception):
    pass


def _show(repo, rev, path):
    """The text of `rev:path`, or None when it does not exist there (or git fails: then every key looks changed)."""
    try:
        p = subprocess.run(["git", "show", "%s:%s" % (rev, path)], cwd=repo, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.decode("utf-8", "replace") if p.returncode == 0 else None


def _load(text):
    if text is None:
        return {}
    try:
        d = json.loads(text)
    except ValueError:
        return _BAD
    return d if isinstance(d, dict) else _BAD


def _hosts(cfg):
    out = {}
    hosts = cfg.get("hosts")
    for h in hosts if isinstance(hosts, list) else []:
        if isinstance(h, dict) and isinstance(h.get("name"), str):
            out[h["name"]] = h
    return out


def _key_changes(path, old, new, keys, hosts=False):
    if new is _BAD:
        return [{"kind": "config_change", "path": path, "key": "%s (the new file does not parse)" % path}]
    old = {} if old is _BAD else old
    out = [{"kind": "config_change", "path": path, "key": k} for k in keys if old.get(k) != new.get(k)]
    if hosts:
        oh, nh = _hosts(old), _hosts(new)
        for name in sorted(set(oh) | set(nh)):
            for k in HOST_KEYS:
                if (oh.get(name) or {}).get(k) != (nh.get(name) or {}).get(k):
                    out.append({"kind": "config_change", "path": path, "key": "hosts[%s].%s" % (name, k)})
    return out


def is_hooks_file(path):
    return path == HOOKS_FILE or path.endswith("/" + HOOKS_FILE)


def merge_base(repo, base, head):
    rc = subprocess.run(["git", "merge-base", base, head], cwd=repo, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
    if rc.returncode != 0 or not rc.stdout.strip():
        raise GateError("git merge-base %s %s failed: %s" % (base, head, rc.stderr.strip()[-300:]))
    return rc.stdout.strip()


def config_changes(repo, base, head, paths):
    """The lead-config changes of the net diff base...head (`paths` is that diff). -> [violation]."""
    mb = merge_base(repo, base, head)
    out = []
    if CONFIG_PATH in paths:
        out += _key_changes(CONFIG_PATH, _load(_show(repo, mb, CONFIG_PATH)), _load(_show(repo, head, CONFIG_PATH)),
                            CONFIG_KEYS, hosts=True)
    for sp in SETTINGS_PATHS:
        if sp in paths:
            out += _key_changes(sp, _load(_show(repo, mb, sp)), _load(_show(repo, head, sp)), SETTINGS_KEYS)
    out += [{"kind": "config_change", "path": p, "key": "the plugin hooks (%s)" % p}
            for p in paths if is_hooks_file(p)]
    return out


def spec_dir_at(repo, rev):
    """The spec tree's folder in the config at `rev` (default specs), normalized (specs/source.py, norm_dir)."""
    from wavecoord.specs.source import norm_dir
    cfg = _load(_show(repo, rev, CONFIG_PATH))
    spec = cfg.get("spec") if isinstance(cfg, dict) else None
    return norm_dir(spec.get("dir") if isinstance(spec, dict) else None)


def spec_tree_used(repo, rev, d):
    """Does the repo at `rev` use the spec tree in folder `d`? As the lead picks its plan (lead/base.py, plan):
    plan.adapter "specs", or no plan adapter and a file under `d`. When in doubt (git fails, the config does
    not parse) yes."""
    cfg = _load(_show(repo, rev, CONFIG_PATH))
    if cfg is _BAD:
        return True
    plan = cfg.get("plan")
    adapter = plan.get("adapter") if isinstance(plan, dict) else None
    if adapter is not None:
        return adapter == "specs"
    try:
        p = subprocess.run(["git", "ls-tree", "-r", "-z", "--name-only", "--full-tree", rev, "--", d + "/"], cwd=repo,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return True
    if p.returncode != 0:
        return True
    for path in p.stdout.decode("utf-8", "replace").split("\0"):
        rel = path[len(d) + 1:] if path.startswith(d + "/") else ""
        if rel and not any(seg.startswith(".") for seg in rel.split("/")):
            return True
    return False


def spec_dirs(repo, revs):
    """The spec tree folders in use at any of `revs`."""
    out = []
    for rev in revs:
        d = spec_dir_at(repo, rev)
        if d not in out and spec_tree_used(repo, rev, d):
            out.append(d)
    return out


def spec_changes(repo, base, head, paths):
    """The spec tree files that a task PR changes (edit-fence.md, section 8.1): requirements and plans are lead
    decisions, and status belongs to the coordinator. The folders come from the config on the base side and at
    the merge base (a tree that moved on the base side is still the tree), only when the repo uses the spec tree
    there. Paths compare case-insensitively (false hits are fine here, misses are not)."""
    if not paths:
        return []
    dirs = spec_dirs(repo, [base, merge_base(repo, base, head)])
    return [{"kind": "spec_change", "path": p, "key": "the spec tree (%s)" % p} for p in paths
            if any(under_dir(p, d) for d in dirs)]


def under_dir(path, d):
    """Is `path` under folder `d`? Case-insensitive: SPECS/x lands in specs/ on a case-insensitive checkout."""
    return path.lower().startswith(d.lower() + "/")


def base_merge_mode(repo, base):
    """The fence `merge` mode of the config on the base side (the default branch): the authority (section 8)."""
    from wavecoord import fencecore
    cfg = _load(_show(repo, base, CONFIG_PATH))
    return fencecore.parse_modes(cfg.get("fence") if isinstance(cfg, dict) else None)["merge"]


CONFIG_ADVICE = ("a task PR may not change the lead's config (fence modes, gate commands, providers, claude_flags,"
                 " host env, hooks) or the spec tree; the lead makes this change in a plan or config PR"
                 " (spec_edit for the spec tree)")


def parse_body(text):
    """-> {task, attempt, lease_task, epoch, error} from a PR body (missing keys are None). `error` names a body
    that has more than one Lease: line; gate-check refuses it after the config and spec checks."""
    out = {"task": None, "attempt": None, "lease_task": None, "epoch": None, "error": None}
    m = LEASE_RE.findall(text or "")
    if len(m) == 1:
        out["lease_task"], out["epoch"] = m[0][0], int(m[0][1])
    elif len(m) > 1:
        out["error"] = "the PR body has %d Lease: lines; it must have one" % len(m)
    t = TASK_RE.findall(text or "")
    out["task"] = t[0] if len(t) == 1 else None
    a = ATTEMPT_RE.findall(text or "")
    out["attempt"] = int(a[0]) if len(a) == 1 else None
    return out


def _run(argv, cwd):
    try:
        p = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        raise GateError("%s: %s" % (argv[0], e))
    if p.returncode != 0:
        raise GateError("%s failed: %s" % (" ".join(argv[:3]), (p.stderr or p.stdout).strip()[-300:]))
    return p.stdout


def pr_info(pr, repo):
    out = _run(["gh", "pr", "view", str(pr), "--json", "body,baseRefName,headRefOid"], repo)
    d = json.loads(out)
    try:
        _run(["git", "fetch", "-q", "origin", d["baseRefName"], d["headRefOid"]], repo)
    except GateError:
        pass  # the objects may be here already
    return d["body"] or "", "origin/%s...%s" % (d["baseRefName"], d["headRefOid"])


def lease_ok(lease_rec, task, epoch):
    """Is Lease: task@epoch the attempt's live lease (or an older lease of it, resolver lineage)?"""
    if not lease_rec or lease_rec.get("task") != task or lease_rec.get("state") == "released":
        return False
    return epoch in (lease_rec.get("epoch"), lease_rec.get("from_epoch"))


def set_lease_line(text, task, epoch):
    """The PR body with its `Lease:` line set to task@epoch (added after Attempt:/Task: when missing)."""
    line = "Lease: %s@%d" % (task, int(epoch))
    if LEASE_RE.search(text or ""):
        return LEASE_RE.sub(line, text, count=1)
    lines = (text or "").split("\n")
    at = None
    for i, l in enumerate(lines):
        if ATTEMPT_RE.match(l) or TASK_RE.match(l):
            at = i
    if at is None:
        return line + "\n" + (text or "")
    lines.insert(at + 1, line)
    return "\n".join(lines)


def update_pr_lease(pr, task, epoch, repo, body_file):
    """Set the Lease: line of PR `pr` to task@epoch (gh pr view + gh pr edit). -> None or the error."""
    try:
        out = _run(["gh", "pr", "view", str(pr), "--json", "body"], repo)
        body = json.loads(out).get("body") or ""
        new = set_lease_line(body, task, epoch)
        if new == body:
            return None
        with open(body_file, "w", encoding="utf-8") as f:
            f.write(new)
        _run(["gh", "pr", "edit", str(pr), "--body-file", body_file], repo)
        return None
    except (GateError, OSError, ValueError) as e:
        return str(e)


def net_range(rng):
    """A..B or A...B -> A...B (the net diff from the merge base)."""
    if "..." in rng:
        return rng
    if ".." in rng:
        a, b = rng.split("..", 1)
        return "%s...%s" % (a, b or "HEAD")
    return "%s...HEAD" % rng


def coord_checker(coord, ctx):
    """check_fn for a coordinator in this process (the lead's refinery): the same check, no REST."""
    from wavecoord.core import ApiError

    def check(body):
        try:
            r = coord.check(ctx, body)
        except ApiError as e:
            return fence.CheckResult(fail="unavailable" if e.status >= 500 else
                                     ("bad_request" if e.status == 400 else "malformed"), detail=e.message)
        return fence.CheckResult(r["violations"], r.get("seq"), lease=r.get("lease"))
    return check


def run(target=None, task=None, attempt=None, rng=None, pr_body=None, lease=None, repo=None, env=None,
        out=sys.stdout, err=sys.stderr, as_json=False, check_fn=None, body_text=None, merge_mode=None):
    env = os.environ if env is None else env
    repo = os.path.abspath(repo or os.getcwd())
    res = {"verdict": "error", "exit": UNAVAILABLE_EXIT, "lease_ok": None, "task": None, "attempt": None, "epoch": None,
           "paths": 0, "violations": [], "seq": None, "detail": None, "config_changes": [], "merge_mode": merge_mode,
           "config_checked": False}

    def done(code, verdict, detail=None):
        res.update(exit=code, verdict=verdict, detail=detail)
        if as_json:
            out.write(json.dumps(res, sort_keys=True) + "\n")
        else:
            err.write("wavecoord gate-check: %s%s\n" % (verdict, (": " + detail) if detail else ""))
            for v in res["violations"][:50]:
                err.write("  %s\n" % fence.violation_line(v))
        return code

    try:
        if target and (str(target).isdigit() or "/pull/" in str(target)):
            body_text, pr_rng = pr_info(target, repo)
            rng = rng or pr_rng
        elif target:
            rng = rng or target
        if pr_body:
            with open(pr_body, encoding="utf-8") as f:
                body_text = f.read()
        info = parse_body(body_text) if body_text is not None else {"task": None, "attempt": None,
                                                                     "lease_task": None, "epoch": None, "error": None}
        # 1. the config and spec checks: they do not depend on the Lease: line, so they run first, and a PR body
        #    with no Lease: line or with two of them cannot skip them (edit-fence.md, section 8)
        nr = net_range(rng or "origin/HEAD...HEAD")
        paths = fence.diff_paths(repo, nr)
        if paths is None:
            raise GateError("git diff %s failed" % nr)
        res["paths"] = len(paths)
        base, head = nr.split("...", 1)
        cfg = (config_changes(repo, base, head or "HEAD", paths) + spec_changes(repo, base, head or "HEAD", paths)
               if paths else [])
        if cfg and merge_mode is None:
            merge_mode = base_merge_mode(repo, base)
        res.update(config_changes=cfg, merge_mode=merge_mode, config_checked=True)
        cfg_block = bool(cfg) and merge_mode != "off"
        if cfg and not cfg_block:
            err.write("wavecoord gate-check: note: %d config change(s), not refused because the fence merge mode is"
                      " off: %s\n" % (len(cfg), ", ".join(v["key"] for v in cfg)))
        if cfg_block:
            res["violations"] = list(cfg)
        # 2. the Lease: line; a missing or ambiguous one is never a pass (lease_ok false)
        if info.get("error") and not lease:
            res["lease_ok"] = False
            raise GateError(info["error"])
        if lease:
            m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,63})@([0-9]+)$", lease)
            if not m:
                raise GateError("--lease must be <task>@<epoch>")
            if info["epoch"] is not None and (info["lease_task"], info["epoch"]) != (m.group(1), int(m.group(2))):
                res["lease_ok"] = False
                return done(VIOLATION_EXIT, "fail", "--lease %s does not match the PR body's Lease: %s@%s"
                            % (lease, info["lease_task"], info["epoch"]))
            info["lease_task"], info["epoch"] = m.group(1), int(m.group(2))
        task = task or info["task"] or info["lease_task"]
        attempt = int(attempt) if attempt else info["attempt"]
        res.update(task=task, attempt=attempt, epoch=info["epoch"])
        if info["epoch"] is None:
            res["lease_ok"] = False
            return done(VIOLATION_EXIT, "fail", "no Lease: <task>@<epoch> line (PR body, --pr-body or --lease)")
        if info["lease_task"] != task:
            res["lease_ok"] = False
            return done(VIOLATION_EXIT, "fail", "the Lease: line names task %s, not %s" % (info["lease_task"], task))
        if not attempt:
            raise GateError("no attempt number (--attempt or an Attempt: line)")
        client = None if check_fn else Client.from_env(agent=None, deadline=Deadline(BUDGET_S), env=env, cwd=repo)
        if client is None and check_fn is None:
            return done(UNAVAILABLE_EXIT, "unavailable", "no coordinator URL")
        if not paths:
            return done(PASS_EXIT, "pass", "the diff is empty")
        r = fence.check_paths(client, None, paths, task, info["epoch"], source="gate", extra={"attempt": attempt},
                              fold_case=fence.case_insensitive(repo), check_fn=check_fn)
        if not r.ok:
            code = UNAVAILABLE_EXIT
            return done(code, "unavailable" if r.fail == "unavailable" else "error",
                        fence.describe(r.fail, "the merge", detail=r.detail))
        res.update(violations=r.violations, seq=r.seq)
        lease_rec = r.lease
        res["lease_ok"] = lease_ok(lease_rec, task, info["epoch"])
        if lease_rec is None and not r.violations:
            return done(VIOLATION_EXIT, "fail", "attempt %s/%s has no lease on the server" % (task, attempt))
        if lease_rec is not None and lease_rec.get("task") == task and lease_rec.get("from_epoch") == info["epoch"]:
            err.write("wavecoord gate-check: Lease: %s@%s is an earlier lease of the same attempt; the current"
                      " lease is %s@%s (resolver lineage)\n" % (task, info["epoch"], task, lease_rec.get("epoch")))
        elif lease_rec is not None and (lease_rec.get("task"), lease_rec.get("epoch")) != (task, info["epoch"]):
            return done(VIOLATION_EXIT, "fail", "the PR body says Lease: %s@%s, the server has %s@%s"
                        % (task, info["epoch"], lease_rec.get("task"), lease_rec.get("epoch")))
        if cfg_block:
            res["violations"] = list(r.violations) + cfg
        if r.violations:
            return done(VIOLATION_EXIT, "fail", "%d violation(s) in %d path(s)" % (len(res["violations"]), len(paths)))
        if cfg_block:
            return done(VIOLATION_EXIT, "fail", "%d config change(s): %s; %s" % (
                len(cfg), ", ".join(v["key"] for v in cfg), CONFIG_ADVICE))
        return done(PASS_EXIT, "pass", "%d path(s) checked at seq %s" % (len(paths), r.seq))
    except (GateError, OSError, ValueError, KeyError) as e:
        return done(UNAVAILABLE_EXIT, "error", str(e))
    except Exception as e:  # noqa: BLE001 - never pass on a bug
        return done(UNAVAILABLE_EXIT, "error", "%s: %s" % (type(e).__name__, e))
