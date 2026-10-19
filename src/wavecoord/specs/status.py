"""Status commits: the coordinator writes task status into the spec tree (docs/design/spec-layer.md, section 7.2).

The writer reconciles. At each flush it reads the tree on the default branch at
one commit, works out each task's status from the coordinator's state, and
commits only the `status:` lines that differ, on top of that same commit:

    a live attempt or a running lease    -> in_progress
    a parked lease whose attempt has a PR -> in_review
    a merged attempt                     -> done
    in_progress or in_review in the file, but nothing live any more -> planned

It writes only over planned, in_progress and in_review. The lead's statuses
(draft, on_hold, dropped) are never touched, even while an attempt runs; the
flush lists such tasks under `held`. `done` is undone only by an explicit reset
(`wavecoord spec status-sync --reset <task>`) when the coordinator has no
merged attempt for the task. So a flush is idempotent, and `status-sync` is the
same flush, forced.

The commit is pushed to the default branch without force. A push that lost a
race (non-fast-forward) is retried after a fetch, with backoff. A push the
remote refuses (a protected branch) sends the same commit to the branch
`wavecoord/status` (force-with-lease: it is the coordinator's own branch), one
open PR carries it, and `merge_cmd` merges it. Any other failure is blocked.
Each flush logs `spec.status`, or `spec.status_blocked` once per distinct
reason. A flush never raises.
"""

import os
import re
import threading
import time

from .plan import coord_id
from .tree import Tree

STATUS_BRANCH = "wavecoord/status"
FROM_COORD = {"active": "in_progress", "review": "in_review", "done": "done"}
WRITABLE = ("planned", "in_progress", "in_review")
LEAD_STATUSES = ("draft", "on_hold", "dropped")
DEFAULT_AUTHOR = "wavecoord <wavecoord@localhost>"
PUSH_TRIES = 3


def wanted(tree, states, reset=()):
    """-> [(task id, path, from, to)] for the tasks whose file status differs from the coordinator's.

    `reset`: task ids (tree or coordinator form) whose `done` goes back to planned (or the live status) when the
    coordinator has no merged attempt for them."""
    resets = set(coord_id(r) for r in reset or ())
    out = []
    for tid, n in sorted(tree.nodes.items()):
        if n.kind != "task" or n.rec is None:
            continue
        cid = coord_id(tid)
        cur = n.rec.get("status")
        coord = states.get(cid)
        live = FROM_COORD.get(coord)
        if cur == "done":
            if cid not in resets or coord == "done":
                continue  # done is undone only by an explicit reset
            to = live or "planned"
        elif cur in WRITABLE:
            if live:
                to = live
            elif cur in ("in_progress", "in_review"):
                to = "planned"  # the attempt ended without a merge
            else:
                continue
        else:
            continue  # a lead status (draft, on_hold, dropped): never written over
        if to != cur:
            out.append((tid, n.path, cur, to))
    return out


def held(tree, states):
    """-> [(task id, lead status, coordinator status)]: tasks the lead held, dropped or drafted that still have
    a live attempt, a PR or a merge in the coordinator."""
    out = []
    for tid, n in sorted(tree.nodes.items()):
        if n.kind != "task" or n.rec is None or n.rec.get("status") not in LEAD_STATUSES:
            continue
        live = FROM_COORD.get(states.get(coord_id(tid)))
        if live:
            out.append((tid, n.rec["status"], live))
    return out


def _value_end(s):
    """The rest of a `status:` line after its value: a trailing comment, or ''."""
    v = s.lstrip(" \t")
    if v[:1] in ("'", '"'):
        q, j = v[0], 1
        while j < len(v):
            if q == '"' and v[j] == "\\":
                j += 2
                continue
            if v[j] == q:
                if q == "'" and v[j + 1:j + 2] == "'":
                    j += 2
                    continue
                break
            j += 1
        rest = v[j + 1:]
    else:
        m = re.search(r"[ \t]+#", v)
        rest = v[m.start():] if m else ""
    return rest.rstrip(" \t")


def set_status_line(text, status):
    """Change only the frontmatter's `status:` line (section 7.2, must): any form the parser reads (quotes, a
    trailing comment, CRLF). The comment and the line ending are kept. Raises ValueError without one."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r").strip() != "---":
        raise ValueError("no frontmatter")
    for i in range(1, len(lines)):
        line = lines[i]
        eol = "\r" if line.endswith("\r") else ""
        body = line[:len(line) - len(eol)]
        if body.strip() == "---":
            break
        if body.startswith("status:"):
            lines[i] = "status: %s%s%s" % (status, _value_end(body[len("status:"):]), eol)
            return "\n".join(lines)
    raise ValueError("no status line")


def push_failure(err):
    """Classify a failed `git push`: stale (the branch moved: fetch and retry), refused (the remote will not take
    the push: a protected branch, a hook) or error (network, credentials, anything else)."""
    if re.search(r"\[rejected\][^\n]*\((fetch first|non-fast-forward|stale info)\)", err):
        return "stale"
    low = err.lower()
    if "[remote rejected]" in low or "protected branch" in low or "gh006" in low or "hook declined" in low:
        return "refused"
    return "error"


class StatusWriter(object):
    def __init__(self, lead):
        self.lead = lead
        self.lock = threading.Lock()
        self.last = 0.0
        self.again = False  # a flush asked for while one ran: the running one goes once more
        self.pending_reset = set()
        self.pr_url = None
        self.pr_key = None  # (base, changes) the status branch carries
        self.pr_sha = None
        self.blocked_reason = None

    def cfg(self):
        return self.lead.cfg().raw.get("spec") or {}

    def enabled(self):
        return self.cfg().get("status_commits", True) is not False

    def period(self):
        return float(self.cfg().get("status_flush_s", os.environ.get("WAVECOORD_STATUS_FLUSH_S", 60)))

    def flush(self, force=False, wait=False, reset=()):
        """One reconcile; at most every status_flush_s unless forced. Never waits for a running flush unless
        `wait`: a forced flush is then queued (the running one goes once more). -> a summary dict."""
        try:
            if not self.enabled():
                return {"skipped": "status_commits is off"}
            if not force and time.time() - self.last < self.period():
                return {"skipped": "not due"}
        except Exception as e:  # noqa: BLE001 - a flush never raises
            return {"skipped": "config: %s" % e}
        if not self.lock.acquire(blocking=bool(wait)):
            if force:
                self.pending_reset.update(reset or ())
                self.again = True
                return {"skipped": "busy", "queued": True}
            return {"skipped": "busy"}
        try:
            reset = set(reset or ()) | self.pending_reset
            self.pending_reset = set()
            while True:
                self.again = False
                self.last = time.time()
                try:
                    out = self._flush_locked(reset)
                except Exception as e:  # noqa: BLE001 - logged as spec.status_blocked, never raised
                    out = self._blocked("%s: %s" % (type(e).__name__, e))
                if not self.again:
                    return out
                reset = self.pending_reset
                self.pending_reset = set()
        finally:
            self.lock.release()

    def _flush_locked(self, reset):
        from wavecoord.const import get as const
        from wavecoord.plan.edit import EditError, PushError
        for attempt in range(PUSH_TRIES):
            try:
                return self._flush_once(reset)
            except PushError as e:
                kind = push_failure(e.stderr)
                if kind == "refused":
                    return self._via_pr(str(e), reset)
                if kind == "error" or attempt == PUSH_TRIES - 1:
                    return self._blocked(str(e))
                time.sleep(min(float(const("PUSH_RETRY_S")), 2.0 ** attempt))
                self.lead.fetch(force=True)
            except EditError as e:
                return self._blocked(str(e))

    def _read(self, reset):
        """-> (files, base sha, changes, held): the tree at one commit of the default branch, read at that sha."""
        from wavecoord.launcher.common import git
        from wavecoord.launcher.worktrees import REMOTE
        from wavecoord.specs import source
        self.lead.fetch()
        default = self.lead.default_branch()
        base = git(["rev-parse", "--verify", "refs/remotes/%s/%s^{commit}" % (REMOTE, default)], self.lead.repo)
        files = source.read_ref(self.lead.repo, base, self.lead.spec_dir())
        if not files:  # no tree on the default branch: nothing to write (never the working tree)
            return default, base, {}, [], []
        tree = Tree(files)
        states = self.lead.states()
        return default, base, files, wanted(tree, states, reset), held(tree, states)

    def _summary(self, out, hold):
        if hold:
            out["held"] = [{"task": coord_id(t), "status": s, "coordinator": c} for t, s, c in hold]
        return out

    def _flush_once(self, reset):
        default, base, files, changes, hold = self._read(reset)
        if not changes:
            self.blocked_reason = None
            return self._summary({"changes": []}, hold)
        sha = self._commit(default, base, changes, files)
        payload = {"sha": sha, "via": "push", "changes": [{"task": coord_id(t), "from": f, "to": to}
                                                          for t, _, f, to in changes]}
        self._landed(payload)
        return self._summary(payload, hold)

    def _landed(self, payload):
        self.blocked_reason = None
        self._record("spec.status", payload)
        self.lead.fetch(force=True)

    def _texts(self, changes, files):
        """{repo path: new text}, each differing from the base text in its status line only (section 7.2)."""
        from wavecoord.plan.edit import EditError
        d = self.lead.spec_dir()
        out = {}
        for tid, path, frm, to in changes:
            old = files[path]
            try:
                new = set_status_line(old, to)
            except ValueError as e:
                raise EditError("%s/%s: %s" % (d, path, e))
            diff = [(a, b) for a, b in zip(old.split("\n"), new.split("\n")) if a != b]
            if len(old.split("\n")) != len(new.split("\n")) or len(diff) != 1 or \
                    not diff[0][1].startswith("status: "):
                raise EditError("%s/%s: a status write would change more than the status line" % (d, path))
            out["%s/%s" % (d, path)] = new
        return out

    def _commit(self, branch, base_sha, changes, files, lease=None):
        from wavecoord.plan.edit import commit_files
        out = self._texts(changes, files)
        msg = "wavecoord: status (%d task%s)" % (len(changes), "" if len(changes) == 1 else "s")
        body = "\n".join("Wavecoord-Status: %s %s %s" % (coord_id(t), f, to) for t, _, f, to in changes)
        author = self.cfg().get("status_author") or DEFAULT_AUTHOR
        wt_root = os.path.join(self.lead.launcher().index_dir(), "status-wt")
        return commit_files(self.lead.repo, base_sha, branch, out, msg, body, wt_root, author=author,
                            lease=lease)

    def _record(self, kind, payload):
        try:
            self.lead.coord.record(self.lead.ctx, kind, payload)
        except Exception:  # noqa: BLE001 - the log is best effort here; the files carry the status
            pass

    def _remote_sha(self, branch):
        from wavecoord.launcher.common import git_run
        from wavecoord.launcher.worktrees import REMOTE
        rc, out, err = git_run(["ls-remote", REMOTE, "refs/heads/" + branch], self.lead.repo, timeout=120)
        if rc != 0:
            from wavecoord.plan.edit import EditError
            raise EditError("ls-remote of %s failed: %s" % (branch, err[-300:]))
        return out.split()[0] if out.split() else ""

    def _pr_state(self):
        fn = getattr(self.lead, "pr_state", None)
        if fn is None or not self.pr_url:
            return None
        st, merged_at, err = fn(self.pr_url, STATUS_BRANCH)
        return "MERGED" if merged_at else st

    def _via_pr(self, push_error, reset):
        """The default branch refused the push: the status commit goes through one PR (section 7.2)."""
        from wavecoord.launcher.client import run_pr_cmd
        default, base, files, changes, hold = self._read(reset)
        if not changes:
            self.blocked_reason = None
            return self._summary({"changes": []}, hold)
        if self._pr_state() in ("MERGED", "CLOSED"):
            self.pr_url = None  # the next PR is a new one
        key = (base, tuple(changes))
        remote = self._remote_sha(STATUS_BRANCH)
        if key == self.pr_key and remote and remote == self.pr_sha:
            sha = self.pr_sha  # the branch carries these changes already: let its checks finish
        else:
            sha = self._commit(STATUS_BRANCH, base, changes, files, lease=remote)
            self.pr_key, self.pr_sha = key, sha
        if not self.pr_url:
            body = "Status commits of the spec tree (docs/specs.md); the default branch refused a direct push:\n\n" + \
                   push_error[-300:]
            path = os.path.join(self.lead.launcher().index_dir(), "status-pr-body.md")
            url, err = run_pr_cmd(self.lead.cfg(), STATUS_BRANCH, default, "wavecoord: status", body, path)
            if not url:
                return self._blocked("status PR: %s" % err)
            self.pr_url = url
        ok, detail = self._merge_status_pr(sha, default)
        payload = {"sha": sha, "via": "pr", "pr_url": self.pr_url, "merged": ok,
                   "changes": [{"task": coord_id(t), "from": f, "to": to} for t, _, f, to in changes]}
        if not ok:
            if self._pr_state() in ("MERGED", "CLOSED"):
                self.pr_url = None
            return self._blocked("status PR not merged: %s" % detail, payload)
        self.pr_url = self.pr_key = self.pr_sha = None  # merged: the next refusal opens a new one
        self._landed(payload)
        return self._summary(payload, hold)

    def _merge_status_pr(self, sha, default):
        """merge_cmd on the status PR, with the placeholders and the environment the refinery gives it."""
        from wavecoord.lead.refinery import DEFAULT_MERGE_CMD, pr_number
        from wavecoord.launcher.common import run_cmd
        from wavecoord.launcher.client import fill
        cmd = self.lead.cfg().raw.get("merge_cmd") or DEFAULT_MERGE_CMD
        pr = str(pr_number(self.pr_url) or self.pr_url)
        values = {"pr": pr, "sha": sha, "branch": STATUS_BRANCH, "base": default, "task": "", "attempt": "",
                  "pr_url": self.pr_url}
        if isinstance(cmd, list):
            argv = [fill(a, values) for a in cmd]
            if not any("{pr}" in a or "{pr_url}" in a for a in cmd):
                argv.append(pr)
            cmd = argv
        env = dict(os.environ, WAVECOORD_PR=pr, WAVECOORD_PR_URL=self.pr_url, WAVECOORD_SHA=sha,
                   WAVECOORD_BRANCH=STATUS_BRANCH, WAVECOORD_BASE=default, WAVECOORD_TASK="", WAVECOORD_ATTEMPT="")
        rc, out, err = run_cmd(cmd, self.lead.root(), env=env, timeout=900)
        return rc == 0, (err or out).strip()[-300:]

    def _blocked(self, why, payload=None):
        """spec.status_blocked, logged once per distinct reason (a repeat is not a transition)."""
        why = why[-500:]
        p = dict(payload or {}, reason=why)
        if why != self.blocked_reason:
            self.blocked_reason = why
            self._record("spec.status_blocked", p)
        return dict(p, blocked=True)


class StatusLoop(object):
    """Flush every spec.status_flush_s (at least 1 s) while the server runs (started by `wavecoord serve`).
    `every` overrides the config (WAVECOORD_STATUS_FLUSH_S)."""

    def __init__(self, lead, every=None):
        self.lead = lead
        self.every = every
        self.stopping = threading.Event()
        self.thread = None

    def period(self):
        if self.every is not None:
            return max(1.0, float(self.every))
        try:
            s = (self.lead.cfg().raw.get("spec") or {}).get("status_flush_s")
            return max(1.0, float(s if s is not None else os.environ.get("WAVECOORD_STATUS_FLUSH_S", 60)))
        except Exception:  # noqa: BLE001 - an unreadable config: the default period
            return 60.0

    def start(self):
        self.thread = threading.Thread(target=self.run, name="wavecoord-status", daemon=True)
        self.thread.start()

    def run(self):
        while not self.stopping.wait(self.period()):
            try:
                if self.lead.spec_files():
                    self.lead.status_flush()
            except Exception:  # noqa: BLE001 - keep the loop alive; the next tick retries
                pass

    def stop(self):
        self.stopping.set()
