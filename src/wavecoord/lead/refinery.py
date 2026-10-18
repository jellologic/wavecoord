"""The refinery: one serialized merge loop over the PRs of reported attempts.

Order: funnel heads first (the attempts whose tasks unblock the most open
work), then the oldest. Before each merge it fetches and re-checks the
merge-tree of the branch against the current default branch; a conflict goes
to a resolver instead of a merge. Then the protected paths, then check_cmd,
then merge_cmd with the head sha the checks saw.
"""

import os
import re
import shutil
import tempfile

from wavecoord.core import ApiError
from wavecoord.globs import globs_overlap
from wavecoord.launcher.client import fill
from wavecoord.launcher.common import git_run, run_cmd

DEFAULT_MERGE_CMD = ["gh", "pr", "merge", "{pr}", "--merge", "--match-head-commit", "{sha}"]


def pr_number(url):
    m = re.search(r"/pull/(\d+)/?$", url or "")
    return m.group(1) if m else (url or "")


class RefineryMixin(object):

    def refinery_queue(self, snap=None):
        snap = snap or self.snapshot()
        refs = {"%s/%d" % (r["task"], r["n"]): r for r in snap["refinery"]}
        try:
            unblocks = {n["id"]: n.get("unblocks", 0) for n in self.plan_graph()["nodes"]}
        except ApiError:
            unblocks = {}
        items = []
        for a in snap["attempts"]:
            if a["state"] != "reported" or not a.get("pr_url") or not a.get("pushed"):
                continue
            key = "%s/%d" % (a["task"], a["n"])
            r = refs.get(key) or {}
            items.append({"task": a["task"], "attempt": a["n"], "pr": pr_number(a["pr_url"]), "pr_url": a["pr_url"],
                          "branch": a.get("branch"), "unblocks": unblocks.get(a["task"], 0),
                          "state": r.get("state") or "waiting", "reason": r.get("reason"), "files": r.get("files"),
                          "detail": r.get("detail"), "ended": a.get("ended") or ""})
        items.sort(key=lambda i: (-i["unblocks"], i["ended"], i["task"], i["attempt"]))
        return items

    def protected_globs(self):
        cmd = self.cfg().raw.get("protected_paths_cmd")
        if not cmd:
            return []
        rc, out, err = run_cmd(cmd, self.root(), timeout=120)
        if rc != 0:
            raise ApiError("unavailable", "protected_paths_cmd exit %s: %s" % (rc, (err or out).strip()[-300:]))
        return [l.strip() for l in out.splitlines() if l.strip() and not l.strip().startswith("#")]

    def refinery_status(self):
        snap = self.snapshot()
        merged = [r for r in snap["refinery"] if r["state"] == "merged"][-20:]
        try:
            globs = self.protected_globs()
        except ApiError as e:
            globs = {"error": e.message}
        raw = self.cfg().raw
        return {"queue": self.refinery_queue(snap), "merged": merged, "busy": self.refinery_lock.locked(),
                "merge_cmd": raw.get("merge_cmd") or DEFAULT_MERGE_CMD, "check_cmd": raw.get("check_cmd"),
                "protected_globs": globs}

    def refinery_merge(self, pr=None, approved_protected=False):
        """Merge one PR (or, without `pr`, every queued PR in order), one at a time."""
        if not self.refinery_lock.acquire(timeout=600):
            raise ApiError("conflict", "the refinery is busy")
        try:
            queue = self.refinery_queue()
            if pr is not None:
                want = str(pr)
                queue = [i for i in queue if want in (i["pr"], i["pr_url"], i["branch"], "%s/%d" % (i["task"], i["attempt"]))]
                if not queue:
                    raise ApiError("not_found", "no PR %s in the refinery queue (reported, pushed, with a PR)" % pr)
            results = [self._merge_one(i, approved_protected) for i in queue]
            return {"results": results, "merged": [r for r in results if r["state"] == "merged"],
                    "blocked": [r for r in results if r["state"] == "blocked"]}
        finally:
            self.refinery_lock.release()

    def _transition(self, kind, state, it, p):
        """Log a refinery event only when it changes the item (transitions only)."""
        key = "%s/%d" % (it["task"], it["attempt"])

        def payload(st):
            cur = st.get("refinery", key) or {}
            if cur.get("state") == state and all(cur.get(k) == p.get(k) for k in ("sha", "reason", "files")):
                return None
            return p
        self.coord.record(self.ctx, kind, payload, it["task"], it["attempt"])

    def _block(self, it, reason, **kw):
        p = {"pr": it["pr"], "pr_url": it["pr_url"], "branch": it["branch"], "reason": reason}
        p.update(kw)
        self._transition("refinery.block", "blocked", it, p)
        out = dict(it, state="blocked", reason=reason)
        out.update(kw)
        return out

    def _merge_one(self, it, approved_protected):
        task, n, branch = it["task"], it["attempt"], it["branch"]
        self.fetch(force=True)
        default = self.default_branch()
        ref = self._ref(branch)
        rc, sha, _ = git_run(["rev-parse", "--verify", "--quiet", ref + "^{commit}"], self.repo)
        if rc != 0:
            return self._block(it, "branch_missing")
        if it["state"] != "blocked":
            self._transition("refinery.queue", "queued", it,
                             {"pr": it["pr"], "pr_url": it["pr_url"], "branch": branch, "sha": sha})
        # 1. re-check the merge-tree against the default branch as it is now
        files = self.merge_conflicts(branch, default)
        if files:
            out = self._block(it, "conflict", files=files[:200], sha=sha)
            c = self.coord.open_conflict(self.ctx, {"kind": "merge_tree", "branch": branch, "base": default,
                                                    "files": files[:200], "source": "refinery"}, task, n)
            try:
                out["resolver"] = self.resolve(attempt="%s/%d" % (task, n), conflict_id=c["id"] if c else None)
            except ApiError as e:
                out["resolve_error"] = e.message
            return out
        # 2. protected paths: only with the user's approval, given in the lead session
        globs = self.protected_globs()
        hits = [f for f in self.changed_files(branch, default) if any(globs_overlap(f, gl) for gl in globs)]
        if hits and not approved_protected:
            return self._block(it, "protected", files=hits, sha=sha,
                               detail="needs the user's approval in the lead session; then call refinery_merge "
                                      "with approved_protected true")
        # 3. check_cmd on the exact head
        check = self.cfg().raw.get("check_cmd")
        if check:
            ok, tail = self._check(check, it, sha, default)
            if not ok:
                return self._block(it, "check_failed", sha=sha, detail=tail)
        # 4. the fence merge check (E5, docs/design/edit-fence.md): enforce blocks, advisory only notes
        fence_note = self._fence_check(it, sha, default)
        if fence_note and fence_note.get("block"):
            return self._block(it, "stale_lease" if fence_note.get("stale") else "fence", sha=sha,
                               detail=fence_note["detail"])
        # 5. merge_cmd, pinned to the head sha that was checked
        ok, detail = self._merge_cmd(it, sha, default)
        if not ok:
            return self._block(it, "merge_failed", sha=sha, detail=detail)
        self.coord.record(self.ctx, "refinery.merge", {"pr": it["pr"], "pr_url": it["pr_url"], "branch": branch,
                                                       "sha": sha, "approved_protected": bool(hits)}, task, n)
        with self.coord.wlock:
            lease = self.coord.state.task_lease(task)
        if lease and lease["attempt"] == n:
            self.coord.release(self.ctx, lease["id"], {"reason": "merged"})
        self.fetch(force=True)
        out = dict(it, state="merged", sha=sha)
        if fence_note:
            out["fence"] = fence_note["detail"]
        return out

    def _fence_check(self, it, sha, default):
        """E4 record and E5 gate-check for one PR. -> None (pass or off), or {block, detail}."""
        import io
        import json as _json
        from wavecoord import fencecore, gatecheck
        mode = fencecore.parse_modes(self.cfg().raw.get("fence"))["merge"]
        problems = []
        with self.coord.wlock:
            att = self.coord.state.get("attempts", "%s/%d" % (it["task"], it["attempt"])) or {}
        if (att.get("fence") or {}).get("verdict") == "fail":
            problems.append("E4: the report's fence verdict is fail (%d violation(s))" % att["fence"].get("count", 0))
        rc, out, err = run_cmd(["gh", "pr", "view", str(it["pr"]), "--json", "body"], self.root(), timeout=120)
        try:
            body = _json.loads(out).get("body") if rc == 0 else None
        except ValueError:
            body = None
        res = io.StringIO()
        code = gatecheck.run(None, it["task"], it["attempt"], "%s/%s..%s" % ("origin", default, sha), repo=self.repo,
                             body_text=body if body is not None else "", out=res, err=io.StringIO(), as_json=True,
                             check_fn=gatecheck.coord_checker(self.coord, self.ctx))
        try:
            d = _json.loads(res.getvalue())
        except ValueError:
            d = {}
        if d.get("lease_ok") is False:
            # a stale Lease: line is never merged, in every fence mode (the epoch is the fencing token)
            return {"block": True, "stale": True,
                    "detail": "stale lease: the PR's Lease: line is not the attempt's current lease (%s)"
                              % d.get("detail")}
        if mode == "off":
            return None
        if code != 0:
            problems.append("E5: gate-check exit %s: %s" % (code, d.get("detail")))
        if not problems:
            return None
        return {"block": mode == "enforce", "detail": "; ".join(problems)}

    def _env(self, it, sha, default):
        return dict(os.environ, WAVECOORD_PR=str(it["pr"]), WAVECOORD_PR_URL=it["pr_url"] or "",
                    WAVECOORD_SHA=sha, WAVECOORD_BRANCH=it["branch"], WAVECOORD_BASE=default,
                    WAVECOORD_TASK=it["task"], WAVECOORD_ATTEMPT=str(it["attempt"]))

    def _check(self, cmd, it, sha, default):
        root = os.path.join(self.launcher().index_dir(), "check-wt")
        os.makedirs(root, exist_ok=True)
        wt = tempfile.mkdtemp(prefix="check-", dir=root)
        os.rmdir(wt)
        rc, _, err = git_run(["worktree", "add", "--quiet", "--detach", wt, sha], self.repo)
        if rc != 0:
            return False, "cannot check out %s: %s" % (sha, err[-300:])
        try:
            rc, out, err = run_cmd(cmd, wt, env=self._env(it, sha, default), timeout=3600)
        finally:
            git_run(["worktree", "remove", "--force", wt], self.repo)
            shutil.rmtree(wt, ignore_errors=True)
            git_run(["worktree", "prune"], self.repo)
        return rc == 0, ("exit %s: %s" % (rc, (out + "\n" + err).strip()[-600:])) if rc != 0 else None

    def _merge_cmd(self, it, sha, default):
        cmd = self.cfg().raw.get("merge_cmd") or DEFAULT_MERGE_CMD
        values = {"pr": str(it["pr"]), "sha": sha, "branch": it["branch"], "base": default, "task": it["task"],
                  "attempt": str(it["attempt"]), "pr_url": it["pr_url"] or ""}
        if isinstance(cmd, list):
            argv = [fill(a, values) for a in cmd]
            if not any("{pr}" in a or "{pr_url}" in a for a in cmd):
                argv.append(str(it["pr"]))  # the PR number as the last argument (adapters.md)
            cmd = argv
        rc, out, err = run_cmd(cmd, self.root(), env=self._env(it, sha, default), timeout=900)
        return rc == 0, None if rc == 0 else "merge_cmd exit %s: %s" % (rc, (err or out).strip()[-600:])
