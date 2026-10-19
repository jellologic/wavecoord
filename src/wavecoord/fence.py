"""The client side of the edit fence (docs/design/edit-fence.md). Stdlib only.

- `modes`: the fence mode of each point, from the launcher (`WAVECOORD_FENCE`)
  and the worktree config; the worktree can only make it stricter.
- `classify`: path normalization and the path table of section 3.6.
- `server_check`: one `POST /api/check`, with every failure mapped to a kind.
- the offline override (section 4.4): a file that only the human writes, a
  journal of the writes it let through, and the flush of that journal.
- `pretool`: the E1 decision for the `PreToolUse` hook.
- `push_check`: the E3 check that the launcher runs before it pushes.

Every rule is in its own function, because the seeded-defect tests patch them
one at a time (tests/test_fence_defects.py).
"""

import json
import os
import socket
import subprocess
import time

from wavecoord import fencecore, schemaload
from wavecoord.client import Client, Deadline, Unreachable, find_repo_config, idem_key

# The flags of every fence diff (E2, E3, E5, the Stop hook, the refinery). --no-renames: without it, the rename
# of a foreign file shows only the new name. --ignore-submodules=none: a submodule (gitlink) change is a path
# too, whatever `submodule.<name>.ignore` in .gitmodules or `diff.ignoreSubmodules` in the git config says.
DIFF_RENAME_FLAGS = ["--no-renames", "--ignore-submodules=none"]
# The git config of the same diffs (it goes before the subcommand).
DIFF_GIT_CONFIG = ["-c", "diff.ignoreSubmodules=none"]
OVERRIDE_NAME = "fence-override.json"
JOURNAL_NAME = "fence-override.jsonl"
MAX_OVERRIDE_S = 4 * 3600
JOURNAL_SEND_MAX = 200
RETRY_DELAY_S = 0.2
E1_BUDGET_S = 2.5
E1_TIMEOUT_S = 2.0
GIT_TIMEOUT_S = 2.0
FAIL_KINDS = ("unavailable", "auth", "bad_request", "malformed")
CLIENT_KINDS = ("foreign_worktree", "protected_local") + FAIL_KINDS
NO_WAY_AROUND = (" Do not write this path in another way (Bash). Send a message to the lead if you need it,"
                 " or wait and retry.")


def _git(cwd, *args):
    try:
        p = subprocess.run(["git"] + list(args), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, universal_newlines=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, None
    return p.returncode, p.stdout


# ----------------------------------------------------------------- modes

def launcher_modes(env):
    """The modes that the launcher passed (WAVECOORD_FENCE: a JSON object or a mode word), or None."""
    raw = (env.get("WAVECOORD_FENCE") or "").strip()
    if not raw:
        return None
    try:
        val = json.loads(raw) if raw.startswith("{") else raw
    except ValueError:
        val = "enforce"  # a broken value never makes the fence weaker
    return fencecore.parse_modes(val)


def worktree_modes(cwd):
    try:
        cfg = find_repo_config(cwd) if cwd else None
    except OSError:
        cfg = None
    if not cfg or "fence" not in cfg:
        return None
    return fencecore.parse_modes(cfg.get("fence"))


def modes(env, cwd):
    """The effective modes: the stricter of the launcher's modes and the worktree config.

    The worktree copy of .claude/wavecoord.json is writable by the agent, so it
    can only make the fence stricter. With no launcher modes (a lead or other
    session), the repo config is the source.
    """
    lm, wm = launcher_modes(env), worktree_modes(cwd)
    if lm is None:
        return wm or dict(fencecore.DEFAULT_MODES)
    return fencecore.stricter(lm, wm) if wm else lm


# ----------------------------------------------------------------- paths

class Target(object):
    """A classified edit target. kind: check, outside, protected_local, foreign_worktree."""

    def __init__(self, kind, abs_path=None, top=None, rel=None, own=False):
        self.kind = kind
        self.abs = abs_path
        self.top = top
        self.rel = rel
        self.own = own

    def __repr__(self):
        return "Target(%s, %r, top=%r)" % (self.kind, self.rel, self.top)


def resolve_target(path, cwd):
    """Step 1: absolute from the hook's cwd, then symlinks and `..` resolved with realpath."""
    p = path if os.path.isabs(path) else os.path.join(cwd or os.getcwd(), path)
    return os.path.realpath(p)


def find_top(abs_path):
    """Step 2: the innermost directory at or above abs_path that holds a `.git` entry, or None."""
    d = abs_path if os.path.isdir(abs_path) else os.path.dirname(abs_path)
    while True:
        if os.path.lexists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def common_dir(top):
    """The git common dir of the worktree at `top` (realpath), read from `.git` without a git call."""
    g = os.path.join(top, ".git")
    try:
        if os.path.isdir(g):
            return os.path.realpath(g)
        with open(g, encoding="utf-8") as f:
            line = f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not line.startswith("gitdir:"):
        return None
    gd = line[len("gitdir:"):].strip()
    gd = gd if os.path.isabs(gd) else os.path.join(top, gd)
    try:
        with open(os.path.join(gd, "commondir"), encoding="utf-8") as f:
            cd = f.read().strip()
        return os.path.realpath(cd if os.path.isabs(cd) else os.path.join(gd, cd))
    except OSError:
        return os.path.realpath(gd)


def same_repo(top, own_top):
    """Is the worktree at `top` a worktree (or the main checkout) of the same repo as `own_top`?"""
    a, b = common_dir(top), common_dir(own_top)
    return a is not None and a == b


def _under(p, d):
    return p == d or p.startswith(d.rstrip(os.sep) + os.sep)


_FOLD_CACHE = {}


def folds_case(path):
    """Does the file system at `path` (its deepest existing folder) fold case? A probe: the same name in the
    other case names the same file. Cached per folder."""
    d = path
    while d and not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent
    if d in _FOLD_CACHE:
        return _FOLD_CACHE[d]
    v = False
    probe = d
    while probe and os.path.dirname(probe) != probe:  # the nearest component with a cased letter
        base = os.path.basename(probe)
        if base.swapcase() != base:
            other = os.path.join(os.path.dirname(probe), base.swapcase())
            try:
                v = os.path.samefile(probe, other)
            except OSError:
                v = False
            break
        probe = os.path.dirname(probe)
    _FOLD_CACHE[d] = v
    return v


def under_local(p, d):
    """Is `p` under the protected folder `d`? On a file system that folds case, `~/.CLAUDE/x` is under
    `~/.claude` (realpath keeps the case as typed), so the compare is case-insensitive there."""
    if _under(p, d):
        return True
    return folds_case(d) and _under(os.path.normcase(p).casefold(), os.path.normcase(d).casefold())


def protected_dirs(env):
    """The local paths that a worker never writes with an edit tool (section 3.6)."""
    home = os.path.expanduser("~")
    out = [os.path.join(home, ".claude"), os.path.join(home, ".wavecoord")]
    for k in ("WAVECOORD_HOME", "CLAUDE_PLUGIN_ROOT", "CLAUDE_CONFIG_DIR", "WAVECOORD_STATE_DIR",
              "CLAUDE_PLUGIN_DATA"):
        v = (env.get(k) or "").strip()
        if v:
            out.append(os.path.expanduser(v))
    return [os.path.realpath(d) for d in out]


def own_worktree(env, cwd):
    wt = (env.get("WAVECOORD_WORKTREE") or "").strip()
    if wt and os.path.isdir(wt):
        return os.path.realpath(wt)
    c = os.path.realpath(cwd or os.getcwd())
    return find_top(c)


_CI_CACHE = {}


def case_insensitive(top):
    """Does the file system at `top` fold case? (cached; a fact that does not change in a session)"""
    if top in _CI_CACHE:
        return _CI_CACHE[top]
    probe = os.path.join(top, ".GIT")
    v = os.path.lexists(os.path.join(top, ".git")) and os.path.lexists(probe)
    _CI_CACHE[top] = v
    return v


def canon_case(top, rel):
    """Step 4: rebuild each existing component of `rel` with its case on disk; keep a new leaf as given."""
    parts = rel.split("/")
    d, out = top, []
    for i, name in enumerate(parts):
        exact = os.path.join(d, name)
        try:
            names = os.listdir(d)
        except OSError:
            out.extend(parts[i:])
            break
        if name in names:
            out.append(name)
            d = exact
            continue
        folded = [n for n in names if n.casefold() == name.casefold()]
        if not folded:
            out.extend(parts[i:])
            break
        out.append(sorted(folded)[0])
        d = os.path.join(d, out[-1])
    return "/".join(out)


def is_ignored(top, rel):
    """A path that git ignores in the worktree at `top` (one git call)."""
    rc, _ = _git(top, "check-ignore", "-q", "--", rel)
    return rc == 0


def outer_top(top, own):
    """The worktree whose paths name a target in the repo at `top`. A nested repo of another git repo below the
    own worktree (a submodule, a vendored clone) is part of the own worktree: the outer repo sees a change in
    it as its path below the own worktree (a submodule change is its gitlink path), so the path is checked from
    there, never from the nested repo's root. A nested worktree of the same repo (a subagent's) keeps its top."""
    if own is not None and top != own and _under(top, own) and not same_repo(top, own):
        return own
    return top


def classify(path, cwd, env, worker):
    """The path table of section 3.6 for one edit target."""
    if not isinstance(path, str) or not path or "\0" in path:
        return Target("bad")
    abs_path = resolve_target(path, cwd)
    own = own_worktree(env, cwd)
    if worker:
        for d in protected_dirs(env):
            if under_local(abs_path, d) and not (own and under_local(own, d) and under_local(abs_path, own)):
                return Target("protected_local", abs_path)
        if own:
            cd = common_dir(own)
            if cd and under_local(abs_path, cd):
                return Target("protected_local", abs_path)
    top = find_top(abs_path)
    if top is None:
        return Target("outside", abs_path)
    top = outer_top(top, own)
    rel =os.path.relpath(abs_path, top).replace(os.sep, "/")
    if rel == ".":
        return Target("outside", abs_path, top)  # the worktree directory itself is not a file
    if any(seg.lower() == ".git" for seg in rel.split("/")):  # `.GIT` too: the same folder on a folding disk
        return Target("protected_local" if worker else "outside", abs_path, top, rel)
    if worker:
        if own is None:
            return Target("outside", abs_path, top, rel)
        if _under(top, own):
            kind, mine = "check", True
        elif same_repo(top, own):
            return Target("foreign_worktree", abs_path, top, rel)
        else:
            return Target("outside", abs_path, top, rel)
    else:
        if own is not None and not (_under(top, own) or same_repo(top, own)):
            return Target("outside", abs_path, top, rel)
        kind, mine = "check", True
    if case_insensitive(top):
        rel = canon_case(top, rel)
    return Target(kind, abs_path, top, rel, own=mine)


# ----------------------------------------------------------------- the server call

def parse_check_response(status, resp):
    """-> (violations, seq) for a valid 200 answer, else None (malformed)."""
    if status != 200 or not isinstance(resp, dict):
        return None
    if schemaload.errors(resp, "api", "#/definitions/check_response"):
        return None
    return list(resp["violations"]), resp.get("seq")


def failure_kind(status):
    if status in (401, 403, 421):
        return "auth"
    if status == 400:
        return "bad_request"
    if status is None or status >= 500 or status in (408, 429):
        return "unavailable"
    return "malformed"


class CheckResult(object):
    """ok: violations is the server's list (maybe empty). Otherwise `fail` is a FAIL_KINDS kind."""

    def __init__(self, violations=None, seq=None, fail=None, detail=None, lease=None):
        self.violations = violations or []
        self.seq = seq
        self.fail = fail
        self.detail = detail
        self.lease = lease

    @property
    def ok(self):
        return self.fail is None

    def kinds(self):
        return sorted(set(v.get("kind") for v in self.violations))


def server_check(client, body, retry=True):
    """One POST /api/check. Never raises; never cached (rule F4)."""
    for attempt in (0, 1):
        try:
            status, resp = client.post("/api/check", body)
        except Unreachable as e:
            if retry and attempt == 0 and client.deadline.remaining() > RETRY_DELAY_S + 0.3:
                time.sleep(RETRY_DELAY_S)
                continue
            return CheckResult(fail="unavailable", detail=str(e))
        except Exception as e:  # noqa: BLE001 - any client bug is a failure, never an allow
            return CheckResult(fail="malformed", detail="%s: %s" % (type(e).__name__, e))
        if status != 200:
            msg = ((resp or {}).get("error") or {}).get("message") if isinstance(resp, dict) else None
            return CheckResult(fail=failure_kind(status), detail="HTTP %s %s" % (status, msg or ""))
        parsed = parse_check_response(status, resp)
        if parsed is None:
            return CheckResult(fail="malformed", detail="the answer does not match check_response")
        return CheckResult(parsed[0], parsed[1], lease=resp.get("lease") if isinstance(resp, dict) else None)
    return CheckResult(fail="unavailable")


# ----------------------------------------------------------------- the offline override

def wc_home(env):
    h = (env.get("WAVECOORD_HOME") or "").strip()
    return os.path.abspath(os.path.expanduser(h)) if h else os.path.join(os.path.expanduser("~"), ".wavecoord")


def override_path(env):
    return os.path.join(wc_home(env), OVERRIDE_NAME)


def journal_path(env):
    return os.path.join(wc_home(env), "state", JOURNAL_NAME)


def read_override(env, now=None):
    """The override record when it is valid now, else None. Only the file counts (never env or config)."""
    p = override_path(env)
    try:
        st = os.stat(p)
        if st.st_mode & 0o077:
            return None  # not mode 600: not written by `wavecoord fence offline`
        with open(p, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    try:
        until, created = float(rec["until"]), float(rec["created"])
    except (KeyError, TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    if not (created <= now + 60 and now < until and until - created <= MAX_OVERRIDE_S + 1):
        return None
    if not isinstance(rec.get("nonce"), str) or not rec["nonce"]:
        return None
    return rec


def override_active(env, cwd=None):
    """The valid override record, or None (the local clock is used only here, rule F3)."""
    return read_override(env)


def write_override(env, seconds, reason, now=None):
    seconds = min(float(seconds), MAX_OVERRIDE_S)
    now = time.time() if now is None else now
    rec = {"until": now + seconds, "created": now, "reason": reason[:500], "host": socket.gethostname(),
           "nonce": os.urandom(8).hex()}
    p = override_path(env)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".%d.tmp" % os.getpid()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(rec, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return rec


def remove_override(env):
    try:
        os.remove(override_path(env))
        return True
    except OSError:
        return False


def abandoned():
    """True in a hook handler that ran past its budget: its process would be gone, so it writes nothing."""
    import threading
    return bool(getattr(threading.current_thread(), "wavecoord_abandoned", False))


def journal_append(env, ovr, agent, task, epoch, path, point):
    if abandoned():
        raise OSError("the hook budget ended")
    rec = {"ts": time.time(), "n": time.time_ns(), "agent": agent, "task": task, "epoch": epoch, "path": path,
           "point": point, "nonce": ovr.get("nonce"), "created": ovr.get("created"), "until": ovr.get("until"),
           "reason": ovr.get("reason")}
    p = journal_path(env)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(rec, separators=(",", ":")) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return rec


def journal_entries(env):
    try:
        with open(journal_path(env), encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def acked_entries(notice, batch):
    """The `n` of each entry of `batch` that the logged notice holds: only these leave the journal.

    A retry has the same idempotency key (nonce and first n) but can carry more entries than the first send.
    When the first send was logged and its answer was lost, the server returns the first notice, and the new
    entries are not in it (0.1.3).
    """
    detail = notice.get("detail")
    acked = detail.get("entries") if isinstance(detail, dict) else None
    if not isinstance(acked, list):
        return set()
    return set(r.get("n") for r in batch) & set(r.get("n") for r in acked if isinstance(r, dict))


def journal_flush(client, env, agent):
    """Send the journal to the server as fence_override notices. -> entries left (0 when empty).

    An entry leaves the journal only when the server acknowledged it. A send that removes some entries but not
    all (a deduplicated retry, or a journal larger than one request) is followed by the next one, with a new
    idempotency key, so one reachable call sends the whole journal. It stops when a send makes no progress (the
    server is unreachable, the client's deadline ended, or the answer is not an acknowledgement).
    """
    left = len(journal_entries(env))
    while left:
        now = _journal_flush_once(client, env, agent)
        if now >= left:
            return now  # no progress: unreachable, or not acknowledged
        left = now
    return 0


def journal_body_max():
    """The largest notify body a flush sends: half the server's body limit, so a batch never gets a 413."""
    from wavecoord import const
    return int(const.get("REQ_MAX_BYTES")) // 2


def _journal_body(entries, batch, agent):
    first = batch[0]
    detail = {"created": first.get("created"), "until": first.get("until"), "reason": first.get("reason"),
              "nonce": first.get("nonce"), "count": len(entries), "entries": batch}
    text = "fence override: %d write(s) went through without a check (reason: %s)" % (
        len(entries), str(first.get("reason") or "")[:200])
    body = {"kind": "fence_override", "agent": agent or "unknown", "text": text, "detail": detail}
    if first.get("task"):
        body["task"] = first["task"]
    return body


def journal_batch(entries, agent, limit=None):
    """The oldest entries that fit in one notify body (at most JOURNAL_SEND_MAX, at least one). -> (batch, body)."""
    limit = journal_body_max() if limit is None else limit
    n = min(len(entries), JOURNAL_SEND_MAX)
    while True:
        body = _journal_body(entries, entries[:n], agent)
        size = len(json.dumps(body, separators=(",", ":")).encode("utf-8"))
        if size <= limit or n == 1:
            return entries[:n], body
        n = max(1, min(n - 1, int(n * limit / size)))


def _journal_flush_once(client, env, agent):
    p = journal_path(env)
    entries = journal_entries(env)
    if not entries:
        return 0
    batch, body = journal_batch(entries, agent)
    first = batch[0]
    try:
        status, resp = client.post("/api/notify", body,
                                   idem=idem_key("fence-override", first.get("nonce"), first.get("n")))
    except Unreachable:
        return len(entries)
    notice = resp.get("notice") if isinstance(resp, dict) else None
    if status != 200 or not isinstance(notice, dict) or notice.get("kind") != "fence_override":
        return len(entries)  # only a real acknowledgement removes an entry
    sent = acked_entries(notice, batch)
    if not sent:
        return len(entries)
    work = p + ".%d.flush" % os.getpid()
    try:
        os.rename(p, work)
    except OSError:
        return len(entries)
    try:
        rest = [r for r in journal_entries_at(work) if r.get("n") not in sent]
        if rest:
            fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rest).encode("utf-8"))
            finally:
                os.close(fd)
        os.remove(work)
    except OSError:
        pass
    return len(journal_entries(env))


def journal_entries_at(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(ln) for ln in f.read().splitlines() if ln.strip()]
    except (OSError, ValueError):
        return []


# ----------------------------------------------------------------- E1: PreToolUse

def deny_output(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def ask_output(reason, system_message=None):
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                  "permissionDecisionReason": reason}}
    if system_message:
        out["systemMessage"] = system_message
    return out


def warn_output(text, system_message=None):
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text[:9000]}}
    out["systemMessage"] = system_message or text[:500]
    return out


def describe(kind, rel, violations=(), detail=None):
    if kind == "foreign_worktree":
        return "foreign_worktree: %s is in another worktree of this repo" % rel
    if kind == "protected_local":
        return "protected_local: %s is a protected local path (git internals, wavecoord state or Claude Code" \
               " config)" % rel
    if kind == "unavailable":
        return "unavailable: the coordinator does not answer (%s), so the fence cannot prove that %s is yours" % (
            detail or "no answer", rel)
    if kind == "auth":
        return "auth: the coordinator refused the token (%s)" % (detail or "auth")
    if kind == "bad_request":
        return "bad_request: the coordinator refused the path %s (%s)" % (rel, detail or "bad request")
    if kind == "malformed":
        return "malformed: the coordinator answer is not valid (%s)" % (detail or "malformed")
    parts = []
    for v in violations:
        k = v.get("kind")
        if k == "foreign_exclusive":
            parts.append("foreign_exclusive: %s is inside %s, reserved exclusive by task %s (holder %s)" % (
                v.get("path", rel), v.get("glob"), v.get("task"), v.get("holder")))
        elif k == "outside_lease":
            parts.append("outside_lease: %s is outside the paths of your lease" % v.get("path", rel))
        elif k == "stale_epoch":
            parts.append("stale_epoch: %s: your lease epoch is stale; another agent may own task %s now" % (
                v.get("path", rel), v.get("task")))
        elif k == "no_lease":
            parts.append("no_lease: %s: you hold no lease on task %s" % (v.get("path", rel), v.get("task")))
        else:
            parts.append("%s %s" % (k, v.get("path", rel)))
    return "; ".join(parts) or kind


def violation_line(v):
    """One violation as `<path>: <kind> (<what>)`, for the guard and gate-check output. Never prints None."""
    k, path = v.get("kind") or "violation", v.get("path") or "?"
    task, lid, glob, holder = v.get("task"), v.get("lease_id"), v.get("glob"), v.get("holder")
    lease = ("lease #%s of task %s" % (lid, task)) if lid is not None and task else (
        ("task %s" % task) if task else None)
    if k == "outside_lease":
        why = "no reservation of your %s covers it" % lease if lease else None
    elif k == "foreign_exclusive":
        why = ", ".join(x for x in (("inside %s" % glob) if glob else None,
                                    ("reserved exclusive by task %s" % task) if task else None,
                                    ("holder %s" % holder) if holder else None) if x) or None
    elif k == "stale_epoch":
        why = ("your epoch is not the current epoch of %s" % lease) if lease else None
    elif k == "no_lease":
        why = ("you hold no lease on task %s" % task) if task else "you hold no lease"
    elif k == "config_change":
        why = "a task PR changes %s; the lead makes this change in a plan or config PR" % (v.get("key") or "it")
    else:
        why = ", ".join(x for x in (("task %s" % task) if task else None, glob) if x) or None
    return "%s: %s%s" % (path, k, (" (%s)" % why) if why else "")


def _epoch_from_env(env):
    try:
        e = int((env.get("WAVECOORD_LEASE_EPOCH") or "").strip())
        return e if e > 0 else None
    except ValueError:
        return None


def pretool(hook):
    """E1. -> the hook output dict, or None (allow, no output)."""
    env, ident = hook.env, hook.id
    worker = bool(ident.task)  # a task from the launcher wins over any lead flag
    m = modes(env, hook.cwd)
    if m["edit"] == "off":
        return None
    enforce = worker and m["edit"] == "enforce"
    notes = []
    if (env.get("WAVECOORD_FENCE_OFFLINE") or "").strip():
        notes.append("WAVECOORD_FENCE_OFFLINE has no effect: only `wavecoord fence offline` (run by the human"
                     " in a terminal) sets the offline override.")
    ti = hook.inp.get("tool_input") if isinstance(hook.inp.get("tool_input"), dict) else {}
    raw = ti.get("file_path") or ti.get("notebook_path")
    if not raw:
        return _with_notes(None, notes)
    t = classify(raw, hook.cwd, env, worker)
    if t.kind == "outside":
        return _with_notes(None, notes)
    if t.kind == "bad":
        return _decide_worker(hook, enforce, "bad_request", str(raw), notes, detail="no usable path") if worker \
            else _with_notes(None, notes)
    if t.kind in ("protected_local", "foreign_worktree"):
        return _decide_worker(hook, enforce, t.kind, t.rel or t.abs, notes)
    rel = t.rel
    if worker and t.own and is_ignored(t.top, rel):
        return _with_notes(None, notes)  # decision D2: a git-ignored path in the own worktree
    if worker and not fencecore.globbable(rel):
        return _decide_worker(hook, enforce, "outside_lease", rel, notes,
                              violations=[{"kind": "outside_lease", "path": rel}])
    body = {"agent": ident.agent, "paths": [rel], "source": "hook"}
    if t.top and case_insensitive(t.top):
        body["fold_case"] = True
    if worker:
        body["task"] = ident.task
        epoch = _epoch_from_env(env)
        if epoch is None:
            try:
                hook.heartbeat(force=True)
            except Unreachable:
                pass
            lease = hook.state.get("lease") or {}
            if lease.get("task") == ident.task and lease.get("epoch"):
                epoch = int(lease["epoch"])
            elif hook.c.reached:
                return _decide_worker(hook, enforce, "no_lease", rel, notes,
                                      violations=[{"kind": "no_lease", "path": rel, "task": ident.task}])
        if epoch is not None:
            body["epoch"] = epoch
    hook.c.timeout = min(hook.c.timeout, E1_TIMEOUT_S)
    res = server_check(hook.c, body)
    if not res.ok:
        if worker:
            return _decide_worker(hook, enforce, res.fail, rel, notes, detail=res.detail)
        return _with_notes(None, notes + ["wavecoord fence: %s; the edit goes on (lead and other sessions fail open)."
                                          % describe(res.fail, rel, detail=res.detail)]) \
            if res.fail in ("unavailable", "auth") else _with_notes(None, notes)
    if worker:
        if not res.violations:
            return _with_notes(None, notes)
        return _decide_worker(hook, enforce, "violation", rel, notes, violations=res.violations)
    foreign = [v for v in res.violations if v.get("kind") == "foreign_exclusive"]
    if not foreign:
        return _with_notes(None, notes)
    text = "wavecoord fence: " + describe("violation", rel, foreign) + "."
    action = m.get("lead", "ask")
    if action == "deny":
        return _with_notes(deny_output(text + " Ask the holder or release the reservation first."), notes)
    if action == "ask":
        return _with_notes(ask_output(text + " Approve only if the holder agrees.", text), notes)
    return _with_notes(warn_output(text + " Do not edit this file unless the holder agrees."), notes)


def _with_notes(out, notes):
    if not notes:
        return out
    out = dict(out or {})
    msg = " ".join(notes)
    out["systemMessage"] = (out.get("systemMessage", "") + " " + msg).strip()
    return out


def _decide_worker(hook, enforce, kind, rel, notes, violations=(), detail=None):
    """A worker write that the fence cannot prove ok. Enforce: deny (or allow under the override)."""
    env, ident = hook.env, hook.id
    text = "wavecoord fence: " + describe(kind, rel, violations, detail) + "."
    if kind == "unavailable":
        ovr = override_active(env, hook.cwd)
        if ovr is not None:
            try:
                journal_append(env, ovr, ident.agent, ident.task, _epoch_from_env(env), rel, "edit")
            except OSError:
                return deny_output(text + " The offline override is set, but its journal cannot be written." +
                                   NO_WAY_AROUND)
            return _with_notes({"systemMessage": "wavecoord fence: offline override (%s): the edit of %s goes on"
                                                 " and is logged." % (ovr.get("reason"), rel)}, notes)
    if not enforce:
        if kind in FAIL_KINDS:
            return _with_notes(None, notes)  # advisory: a failed check is silent, as in P2
        return _with_notes(warn_output("wavecoord (advisory): " + text[len("wavecoord fence: "):] +
                                       " Do not edit this file unless the lead agrees. Send a message to the lead"
                                       " or the holder if you need the change.",
                                       "wavecoord (advisory): " + text[len("wavecoord fence: "):]), notes)
    return _with_notes(deny_output(text + NO_WAY_AROUND), notes)


def unavailable_deny(why):
    """The E1 output when no decision could be made (no URL, budget ended, a crash)."""
    return deny_output("wavecoord fence: unavailable: %s, so the fence cannot prove that this write is yours.%s"
                       % (why, NO_WAY_AROUND))


def e1_fail_closed(env, cwd):
    """Must E1 deny when it cannot decide? True for a worker in edit enforce mode."""
    if not (env.get("WAVECOORD_TASK") or "").strip():
        return False
    try:
        return modes(env, cwd)["edit"] == "enforce"
    except Exception:  # noqa: BLE001
        return True


# ----------------------------------------------------------------- E2/E3/E5 helpers

def diff_paths(cwd, *rev_args):
    """Changed paths of `git diff --name-only <DIFF_RENAME_FLAGS> -z <rev_args>`, or None on a git error."""
    rc, out = _git(cwd, *(DIFF_GIT_CONFIG + ["diff", "--name-only"] + DIFF_RENAME_FLAGS + ["-z"] + list(rev_args)))
    if rc != 0 or out is None:
        return None
    return sorted(set(p for p in out.split("\0") if p))


def check_paths(client, agent, paths, task=None, epoch=None, source="guard", fold_case=False, extra=None,
                check_fn=None):
    """Check many paths in requests of up to 1000. -> CheckResult (violations of all requests).

    check_fn(body) -> CheckResult replaces the REST call (the lead's in-process coordinator)."""
    allv, seq, lease = [], None, None
    for i in range(0, len(paths), 1000):
        body = {"agent": agent, "paths": paths[i:i + 1000], "source": source}
        if task:
            body["task"] = task
        if epoch:
            body["epoch"] = int(epoch)
        if fold_case:
            body["fold_case"] = True
        body.update(extra or {})
        bad = [p for p in body["paths"] if fencecore.path_syntax_error(p) or not fencecore.globbable(p)]
        body["paths"] = [p for p in body["paths"] if p not in bad]
        allv += [{"kind": "outside_lease", "path": p} for p in bad]
        if not body["paths"]:
            continue
        r = check_fn(body) if check_fn else server_check(client, body, retry=False)
        if not r.ok:
            return r
        allv += r.violations
        seq, lease = r.seq, r.lease
    return CheckResult(allv, seq, lease=lease)


def push_check(wt, base, agent, task, epoch, env, point="push"):
    """E3: the net diff merge-base(base, HEAD)..HEAD, checked for the attempt. Never raises.

    -> dict(ok, kind, paths, violations, seq, detail). ok False with kind in
    FAIL_KINDS, "violation", "journal" (the override journal is not sent yet)
    or "git".
    """
    out = {"ok": False, "kind": None, "paths": [], "violations": [], "seq": None, "detail": None}
    try:
        paths = diff_paths(wt, base + "...HEAD")
        if paths is None:
            out.update(kind="git", detail="git diff %s...HEAD failed" % base)
            return out
        out["paths"] = paths
        client = Client.from_env(agent=agent, deadline=Deadline(30), env=env, cwd=wt)
        if client is None:
            out.update(kind="unavailable", detail="no coordinator URL")
            return out
        left = journal_flush(client, env, agent) if journal_entries(env) else 0
        if left:
            out.update(kind="journal", detail="%d offline-override write(s) are not sent to the coordinator yet" % left)
            return out
        if not paths:
            out.update(ok=True)
            return out
        r = check_paths(client, agent, paths, task, epoch, source="push",
                        fold_case=case_insensitive(os.path.realpath(wt)))
        if not r.ok:
            out.update(kind=r.fail, detail=r.detail)
            return out
        out.update(violations=r.violations, seq=r.seq)
        if r.violations:
            out.update(kind="violation", detail=describe("violation", "", r.violations[:5]))
            return out
        out["ok"] = True
        return out
    except Exception as e:  # noqa: BLE001 - the launcher treats an exception as a refusal
        out.update(ok=False, kind="malformed", detail="%s: %s" % (type(e).__name__, e))
        return out
