"""Status commits: the coordinator writes task status into the spec tree (docs/design/spec-layer.md, section 7.2).

The writer reconciles. At each flush it reads the tree on the default branch,
works out each task's status from the coordinator's state, and commits only the
`status:` lines that differ:

    a live attempt or a running lease  -> in_progress
    a parked lease (a PR in review)    -> in_review
    a merged attempt                   -> done
    in_progress or in_review in the file, but nothing live any more -> planned

The lead's statuses (draft, planned, on_hold, dropped) are never touched, and
`done` is never undone. So a flush is idempotent, and `wavecoord spec
status-sync` is the same flush, forced.

The commit changes only `status:` lines (rule M13 keeps it from conflicting
with plan PRs). It is pushed to the default branch without force, after a
rebase on a moved remote. When the push is refused (a protected branch), the
same commit goes to the branch `wavecoord/status`, one open PR carries it, and
`merge_cmd` merges it. Each flush logs `spec.status` (or `spec.status_blocked`).
"""

import os
import re
import threading
import time

from .plan import coord_id
from .tree import Tree

STATUS_BRANCH = "wavecoord/status"
FROM_COORD = {"active": "in_progress", "review": "in_review", "done": "done"}
DEFAULT_AUTHOR = "wavecoord <wavecoord@localhost>"


def wanted(tree, states):
    """-> [(task id, path, from, to)] for the tasks whose file status differs from the coordinator's."""
    out = []
    for tid, n in sorted(tree.nodes.items()):
        if n.kind != "task" or n.rec is None:
            continue
        cur = n.rec.get("status")
        if cur == "done":
            continue  # never undone
        live = FROM_COORD.get(states.get(coord_id(tid)))
        if live:
            to = live
        elif cur in ("in_progress", "in_review"):
            to = "planned"  # the attempt ended without a merge
        else:
            continue
        if to != cur:
            out.append((tid, n.path, cur, to))
    return out


STATUS_LINE = re.compile(r"^status:[ \t]*\S+[ \t]*$", re.M)


def set_status_line(text, status):
    """Change only the frontmatter's `status:` line (section 7.2, must). Raises ValueError without one."""
    end = text.find("\n---", 4)
    head = text[:end if end > 0 else len(text)]
    m = STATUS_LINE.search(head)
    if m is None:
        raise ValueError("no status line")
    return text[:m.start()] + "status: %s" % status + text[m.end():]


class StatusWriter(object):
    def __init__(self, lead):
        self.lead = lead
        self.lock = threading.Lock()
        self.last = 0.0
        self.pr_url = None

    def cfg(self):
        return self.lead.cfg().raw.get("spec") or {}

    def enabled(self):
        return self.cfg().get("status_commits", True) is not False

    def flush(self, force=False):
        """One reconcile; at most every status_flush_s unless forced. -> a summary dict."""
        from wavecoord.plan.edit import EditError
        if not self.enabled():
            return {"skipped": "status_commits is off"}
        every = float(self.cfg().get("status_flush_s", os.environ.get("WAVECOORD_STATUS_FLUSH_S", 60)))
        with self.lock:
            if not force and time.time() - self.last < every:
                return {"skipped": "not due"}
            self.last = time.time()
            for attempt in range(3):
                try:
                    return self._flush_once()
                except EditError as e:
                    err = str(e)
                    if "push" not in err:
                        return self._blocked(err)
                    self.lead.fetch(force=True)
            return self._via_pr(err)

    def _changes(self):
        states = self.lead.states()
        files = self.lead.spec_files()
        if not files:
            return None, []
        return files, wanted(Tree(files), states)

    def _commit(self, branch, base_sha, changes, files):
        from wavecoord.plan.edit import commit_files
        d = self.lead.spec_dir()
        out = {}
        for tid, path, frm, to in changes:
            out["%s/%s" % (d, path)] = set_status_line(files[path], to)
        msg = "wavecoord: status (%d task%s)" % (len(changes), "" if len(changes) == 1 else "s")
        body = "\n".join("Wavecoord-Status: %s %s %s" % (coord_id(t), f, to) for t, _, f, to in changes)
        author = self.cfg().get("status_author") or DEFAULT_AUTHOR
        wt_root = os.path.join(self.lead.launcher().index_dir(), "status-wt")
        return commit_files(self.lead.repo, base_sha, branch, out, msg, body, wt_root, author=author)

    def _record(self, kind, payload):
        try:
            self.lead.coord.record(self.lead.ctx, kind, payload)
        except Exception:  # noqa: BLE001 - the log is best effort here; the files carry the status
            pass

    def _flush_once(self):
        from wavecoord.launcher.common import git
        from wavecoord.launcher.worktrees import REMOTE
        files, changes = self._changes()
        if not changes:
            return {"changes": []}
        default = self.lead.default_branch()
        base = git(["rev-parse", "--verify", "refs/remotes/%s/%s^{commit}" % (REMOTE, default)], self.lead.repo)
        sha = self._commit(default, base, changes, files)
        payload = {"sha": sha, "via": "push", "changes": [{"task": coord_id(t), "from": f, "to": to}
                                                          for t, _, f, to in changes]}
        self._record("spec.status", payload)
        self.lead.fetch(force=True)
        return payload

    def _via_pr(self, push_error):
        """The default branch refused the push: the status commit goes through one PR (section 7.2)."""
        from wavecoord.launcher.client import run_pr_cmd
        from wavecoord.launcher.common import git, git_run
        from wavecoord.launcher.worktrees import REMOTE
        from wavecoord.plan.edit import EditError
        files, changes = self._changes()
        if not changes:
            return {"changes": []}
        default = self.lead.default_branch()
        repo = self.lead.repo
        base = git(["rev-parse", "--verify", "refs/remotes/%s/%s^{commit}" % (REMOTE, default)], repo)
        git_run(["push", "--quiet", REMOTE, "--delete", STATUS_BRANCH], repo)  # our own branch, rebuilt each time
        try:
            sha = self._commit(STATUS_BRANCH, base, changes, files)
        except EditError as e:
            return self._blocked("status branch: %s" % e)
        cfg = self.lead.cfg()
        if not self.pr_url:
            body = "Status commits of the spec tree (docs/specs.md); the default branch refused a direct push:\n\n" + \
                   push_error[-300:]
            path = os.path.join(self.lead.launcher().index_dir(), "status-pr-body.md")
            url, err = run_pr_cmd(cfg, STATUS_BRANCH, default, "wavecoord: status", body, path)
            if not url:
                return self._blocked("status PR: %s" % err)
            self.pr_url = url
        ok, detail = self._merge_status_pr(sha, default)
        payload = {"sha": sha, "via": "pr", "pr_url": self.pr_url, "merged": ok,
                   "changes": [{"task": coord_id(t), "from": f, "to": to} for t, _, f, to in changes]}
        if not ok:
            return self._blocked("status PR %s not merged: %s" % (self.pr_url, detail), payload)
        self.pr_url = None  # merged: the next refusal opens a new one
        self._record("spec.status", payload)
        self.lead.fetch(force=True)
        return payload

    def _merge_status_pr(self, sha, default):
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
        rc, out, err = run_cmd(cmd, self.lead.root(), timeout=900)
        return rc == 0, (err or out).strip()[-300:]

    def _blocked(self, why, payload=None):
        p = dict(payload or {}, reason=why[-500:])
        self._record("spec.status_blocked", p)
        return dict(p, blocked=True)


class StatusLoop(object):
    """Flush every status_flush_s while the server runs (started by `wavecoord serve`)."""

    def __init__(self, lead, every=None):
        self.lead = lead
        self.every = every
        self.stopping = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name="wavecoord-status", daemon=True)
        self.thread.start()

    def run(self):
        while not self.stopping.wait(self.every or 60):
            try:
                if self.lead.spec_files():
                    self.lead.status_flush()
            except Exception:  # noqa: BLE001 - keep the loop alive; the next tick retries
                pass

    def stop(self):
        self.stopping.set()
