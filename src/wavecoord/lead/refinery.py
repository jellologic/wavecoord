"""The refinery: one serialized merge loop over the PRs of reported attempts.

Order: funnel heads first (the attempts whose tasks unblock the most open
work), then the oldest. Before each merge it fetches and re-checks the
merge-tree of the branch against the current default branch; a conflict goes
to a resolver instead of a merge. Then the protected paths, then the fence
check (E4/E5, the config and spec checks), and only then the checks that run
PR code: check_cmd, and for a spec tree task its Acceptance and its
requirement's Verify (on the merge result, with a scrubbed environment). Then
merge_cmd with the head sha the checks saw.

The fence merge mode and spec.verify_at_merge come from the config on the
default branch, never from the lead's working tree (edit-fence.md, section 8).
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
# The environment of Verify and Acceptance: these names (and LC_*) from the lead's environment, plus the names in
# spec.verify_env. Never a credential: names in NEVER_ENV, or with these prefixes, are not passed.
VERIFY_ENV = ("PATH", "HOME", "LANG", "TMPDIR", "TERM", "TZ", "USER", "LOGNAME", "SHELL")
NEVER_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "SSH_AUTH_SOCK",
             "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
NEVER_PREFIX = ("WAVECOORD_", "CLAUDE_PLUGIN_OPTION_", "GIT_")
MERGE_IDENT = {"GIT_AUTHOR_NAME": "wavecoord", "GIT_AUTHOR_EMAIL": "wavecoord@localhost",
               "GIT_COMMITTER_NAME": "wavecoord", "GIT_COMMITTER_EMAIL": "wavecoord@localhost"}


def verify_env(environ, names=()):
    """The scrubbed environment of a Verify or Acceptance run (not a sandbox: see docs/security.md)."""
    out = {}
    for k, v in environ.items():
        if k in NEVER_ENV or k.startswith(NEVER_PREFIX):
            continue
        if k in VERIFY_ENV or k.startswith("LC_") or k in names:
            out[k] = v
    return out


def verify_script(cmds):
    """One sh script that runs `cmds` in order and stops at the first failure. Each command ends at a newline,
    so a trailing `# comment` cannot swallow the rest."""
    return " && ".join("(\n%s\n)" % c for c in cmds)


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
            if any(r["state"] == "merged" for r in results):
                self.status_flush(force=True)
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
        base_cfg = self.base_cfg(default)
        # 3. the fence merge check (E5, docs/design/edit-fence.md): enforce blocks, advisory only notes; a config
        #    or spec change and a stale lease block. It runs before any check that runs the PR's code.
        fence_note = self._fence_check(it, sha, default, base_cfg)
        if fence_note and fence_note.get("block"):
            return self._block(it, fence_note.get("reason") or ("stale_lease" if fence_note.get("stale") else "fence"),
                               sha=sha,
                               detail=fence_note["detail"])
        # 4. check_cmd on the exact head
        check = self.cfg().raw.get("check_cmd")
        if check:
            ok, tail = self._check(check, it, sha, default)
            if not ok:
                return self._block(it, "check_failed", sha=sha, detail=tail)
        # 4b. a spec tree task: its Acceptance and its requirement's Verify, both from the default branch, so a
        #     worker cannot weaken what it is checked by (docs/design/spec-layer.md, section 7.5), on the merge
        #     result of the default branch and the head
        verify = self._verify(it, sha, default, base_cfg)
        if verify is not None and not verify["ok"]:
            return self._block(it, verify.get("reason") or "verify_failed", sha=sha, detail=verify["detail"])
        # 5. merge_cmd, pinned to the head sha that was checked
        ok, detail = self._merge_cmd(it, sha, default)
        if not ok:
            return self._block(it, "merge_failed", sha=sha, detail=detail)
        merged = {"pr": it["pr"], "pr_url": it["pr_url"], "branch": branch, "sha": sha,
                  "approved_protected": bool(hits)}
        if verify is not None:
            merged["spec"] = verify["spec"]
        self.coord.record(self.ctx, "refinery.merge", merged, task, n)
        with self.coord.wlock:
            lease = self.coord.state.task_lease(task)
        if lease and lease["attempt"] == n:
            self.coord.release(self.ctx, lease["id"], {"reason": "merged"})
        self.fetch(force=True)
        out = dict(it, state="merged", sha=sha)
        if fence_note:
            out["fence"] = fence_note["detail"]
        return out

    def base_cfg(self, default):
        """The consumer config on the default branch as fetched ({} when absent or unreadable): the authority for
        the fence mode and spec.verify_at_merge, which the lead's working tree copy cannot override."""
        from wavecoord import gatecheck
        cfg = gatecheck._load(gatecheck._show(self.repo, self._ref(default), gatecheck.CONFIG_PATH))
        return cfg if isinstance(cfg, dict) else {}

    def merge_mode(self, base_cfg):
        """The fence merge mode: the default branch's, or stricter with WAVECOORD_FENCE_FORCE (the lead's env)."""
        import json as _json
        from wavecoord import fencecore
        m = fencecore.parse_modes(base_cfg.get("fence"))
        over = os.environ.get("WAVECOORD_FENCE_FORCE")
        if over:
            try:
                m = fencecore.stricter(m, fencecore.parse_modes(_json.loads(over) if over.startswith("{") else over))
            except ValueError:
                pass
        return m["merge"]

    def _fence_check(self, it, sha, default, base_cfg=None):
        """E4 record and E5 gate-check for one PR. -> None (pass or off), or {block, detail}."""
        import io
        import json as _json
        from wavecoord import gatecheck
        mode = self.merge_mode(self.base_cfg(default) if base_cfg is None else base_cfg)
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
                             check_fn=gatecheck.coord_checker(self.coord, self.ctx), merge_mode=mode)
        try:
            d = _json.loads(res.getvalue())
        except ValueError:
            d = {}
        cfg = d.get("config_changes") or []
        keys = ", ".join(v.get("key") or "?" for v in cfg)
        kind = "spec_change" if any(v.get("kind") == "spec_change" for v in cfg) else "config_change"
        if cfg and mode != "off":
            # a task PR that changes the lead's config or the spec tree (edit-fence.md, sections 8 and 8.1):
            # refused unless the fence is off, whatever its Lease: line says
            return {"block": True, "reason": kind,
                    "detail": "%s: %s; %s" % (kind, keys, gatecheck.CONFIG_ADVICE)}
        if d.get("lease_ok") is False and body is not None:
            # a stale, missing or ambiguous Lease: line is never merged, in every fence mode (the epoch is the
            # fencing token). A body that `gh pr view` cannot read (no gh: a pr_cmd of your own) is an E5
            # problem below: enforce blocks, advisory notes.
            return {"block": True, "stale": True,
                    "detail": "stale lease: the PR's Lease: line is not the attempt's current lease (%s)"
                              % d.get("detail")}
        if not d.get("config_checked") and mode != "off":
            return {"block": True, "reason": "fence",
                    "detail": "E5: the config and spec checks did not run: gate-check exit %s: %s"
                              % (code, d.get("detail"))}
        if cfg:
            return {"block": False, "detail": "%s (reported, fence merge mode off): %s" % (kind, keys)}
        if mode == "off":
            return None
        if body is None:
            problems.append("E5: cannot read the PR body (gh pr view exit %s)" % rc)
        if code != 0:
            problems.append("E5: gate-check exit %s: %s" % (code, d.get("detail")))
        if not problems:
            return None
        return {"block": mode == "enforce", "detail": "; ".join(problems)}

    def _env(self, it, sha, default, base=None, **extra):
        return dict(os.environ if base is None else base, WAVECOORD_PR=str(it["pr"]),
                    WAVECOORD_PR_URL=it["pr_url"] or "", WAVECOORD_SHA=sha, WAVECOORD_BRANCH=it["branch"],
                    WAVECOORD_BASE=default, WAVECOORD_TASK=it["task"], WAVECOORD_ATTEMPT=str(it["attempt"]), **extra)

    def _verify(self, it, sha, default, base_cfg=None):
        """None for a task that is not in a spec tree (or with verify_at_merge false and no record); else
        {ok, detail, spec[, reason]}. It fails closed: a task or requirement record that is missing or does not
        parse blocks with reason verify_unavailable."""
        from wavecoord.plan.adapters import PlanError
        from wavecoord.specs.fingerprint import fingerprint
        from wavecoord.specs.plan import SpecTreePlan, coord_id
        plan = self.plan()
        if not isinstance(plan, SpecTreePlan):
            return None
        base_cfg = self.base_cfg(default) if base_cfg is None else base_cfg
        spec_cfg = base_cfg.get("spec") if isinstance(base_cfg.get("spec"), dict) else {}
        on = spec_cfg.get("verify_at_merge", True) is not False

        def unavailable(why, info=None):
            return {"ok": False, "reason": "verify_unavailable", "spec": info,
                    "detail": "cannot verify %s at merge: %s; fix the spec tree on %s (spec_edit), or set"
                              " spec.verify_at_merge false there" % (it["task"], why, default)}
        try:
            spec = plan.get(it["task"])
        except PlanError as e:
            return unavailable(str(e)) if on else None
        t = spec["tree"]
        node = plan.tree.nodes.get(t["requirement"])
        req = node.rec if node is not None else None
        info = {"task": t["id"], "requirement": t["requirement"], "basis": t.get("basis"),
                "requirement_fp": fingerprint(req) if req else None, "verified": None}
        if not on:
            return {"ok": True, "detail": None, "spec": info}
        if req is None:
            return unavailable("the requirement %s is missing or does not parse: %s" % (
                t["requirement"], "; ".join(plan.tree.parse_errors.get(t["requirement"], [])[:3]) or "no record"), info)
        # the requirement's Verify describes the finished requirement: it runs with the merge of the last
        # open task under the requirement (a dropped or on_hold sibling is not open); earlier merges run the
        # task's own Acceptance
        snap_states = self.states()
        others = [x for x in plan.tree.tasks_under(t["requirement"]) if x != t["id"]]
        pending = [x for x in others if snap_states.get(coord_id(x)) != "done" and
                   (plan.tree.nodes[x].rec or {}).get("status") not in ("done", "dropped", "on_hold")]
        info["requirement_pending"] = pending
        verify = [] if pending else list(t.get("verify") or [])
        cmds = []
        for c in list(spec["sections"].get("acceptance") or []) + verify:
            if c not in cmds:
                cmds.append(c)
        if cmds:
            at, why = self._merge_result(sha, default)
            if at is None:
                return unavailable(why, info)
            names = [n for n in (spec_cfg.get("verify_env") or []) if isinstance(n, str)]
            ok, tail = self._check(verify_script(cmds), it, sha, default, at=at,
                                   env=verify_env(os.environ, names), WAVECOORD_REQS=t["requirement"])
            if not ok:
                return {"ok": False, "detail": "the task's Acceptance or the requirement's Verify failed (from %s,"
                                               " on the merge result): %s" % (default, tail), "spec": info}
        # the requirement is verified once its last task merges and its Verify ran
        info["verified"] = not pending and bool(verify)
        info["commands"] = len(cmds)
        return {"ok": True, "detail": None, "spec": info}

    def _merge_result(self, sha, default):
        """A commit of the merge of the default branch (as fetched) and `sha`, for checks that must see the
        result. -> (sha, None) or (None, why)."""
        base = self._ref(default)
        rc, out, err = git_run(["merge-tree", "--write-tree", "--no-messages", base, sha], self.repo)
        if rc != 0 or not out:
            return None, "git merge-tree %s %s: exit %s %s" % (default, sha[:12], rc, (err or out)[-300:])
        tree = out.splitlines()[0].strip()
        rc, parent, err = git_run(["rev-parse", "--verify", base + "^{commit}"], self.repo)
        if rc != 0:
            return None, "no %s: %s" % (base, err[-300:])
        rc, commit, err = git_run(["commit-tree", tree, "-p", parent, "-p", sha, "-m", "wavecoord: verify merge"],
                                  self.repo, env=dict(os.environ, **MERGE_IDENT))
        if rc != 0 or not commit:
            return None, "git commit-tree: %s" % err[-300:]
        return commit, None

    def _check(self, cmd, it, sha, default, at=None, env=None, **extra):
        """Run `cmd` in a temporary detached worktree of `at` (default the head sha). `env`: the base
        environment (default the lead's), plus the WAVECOORD_ PR variables and `extra`."""
        root = os.path.join(self.launcher().index_dir(), "check-wt")
        os.makedirs(root, exist_ok=True)
        wt = tempfile.mkdtemp(prefix="check-", dir=root)
        os.rmdir(wt)
        rc, _, err = git_run(["worktree", "add", "--quiet", "--detach", wt, at or sha], self.repo)
        if rc != 0:
            return False, "cannot check out %s: %s" % (at or sha, err[-300:])
        try:
            rc, out, err = run_cmd(cmd, wt, env=self._env(it, sha, default, base=env, **extra), timeout=3600)
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
