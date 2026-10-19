"""Status commits and plan-branch commits under hostile conditions (docs/design/spec-layer.md, section 7.2).

Unit tests of the status line, the reconcile and the push classification; then the writer on real git with a stub
lead (a base that moves, CRLF and commented status lines, a busy writer, a push that is not a protection refusal,
a repeated block); the status PR on a real coordinator with fake gh (a PR left open, a PR closed); and
commit_files / spec_edit on real git (consumer hooks, the admin lock, delete then move, move then delete, symlinks,
a reused merged branch, the PR body's check line)."""

import json
import os
import stat
import subprocess
import threading
import time
import types
import unittest

import _paths
from launcher_env import LauncherTestCase, sh
from test_spec_status import IMPL, IMPL_FILE, StatusFlowCase

from wavecoord.launcher import worktrees
from wavecoord.launcher.common import LauncherError
from wavecoord.lead.base import LeadBase
from wavecoord.plan import edit
from wavecoord.specs import doc, source, status
from wavecoord.specs.ops import apply_ops
from wavecoord.specs.tree import Tree

TID = "auth/sessions/token-rotation/rotate-impl"
TESTS_FILE = "auth/sessions/token-rotation/tasks/rotate-tests.md"
TESTS_ID = "auth.sessions.token-rotation.rotate-tests"
STORE = "auth.sessions.session-store.store-api"
REL = "auth/sessions/token-rotation/tasks/rotate-impl.md"


def fixture():
    return source.read_tree(_paths.FIXTURES, "specs")


def with_status(fs, rel, line):
    fs = dict(fs)
    fs[rel] = fs[rel].replace("status: planned\n", line + "\n", 1)
    return fs


class StatusLineTest(unittest.TestCase):
    def test_any_parseable_status_line(self):
        canon = fixture()[REL]
        for line, keep in (("status: planned  # waiting on infra", "  # waiting on infra"), ("status: 'planned'", ""),
                           ('status: "planned"', ""), ("status:   planned", ""), ("status: planned   ", "")):
            for crlf in (False, True):
                text = canon.replace("status: planned\n", line + "\n", 1)
                if crlf:
                    text = text.replace("\n", "\r\n")
                self.assertEqual(doc.parse(text)["status"], "planned", line)
                new = status.set_status_line(text, "in_progress")
                self.assertEqual(doc.parse(new)["status"], "in_progress", (line, crlf))
                a, b = text.splitlines(True), new.splitlines(True)
                self.assertEqual(len(a), len(b))
                diff = [(x, y) for x, y in zip(a, b) if x != y]
                self.assertEqual(len(diff), 1, (line, crlf, diff))
                self.assertEqual(diff[0][1], "status: in_progress" + keep + ("\r\n" if crlf else "\n"))
        with self.assertRaises(ValueError):
            status.set_status_line(canon.replace("status: planned\n", "", 1), "done")
        # a `status:` line in the body is not the frontmatter's
        body_only = canon.replace("status: planned\n", "", 1) + "\nstatus: planned\n"
        with self.assertRaises(ValueError):
            status.set_status_line(body_only, "done")


class ReconcileTest(unittest.TestCase):
    def test_lead_statuses_are_never_written_over(self):
        for lead_status in ("draft", "on_hold", "dropped"):
            fs = with_status(fixture(), REL, "status: " + lead_status)
            t = Tree(fs)
            for live in ("active", "review", "done"):
                self.assertEqual(status.wanted(t, {IMPL: live}), [], (lead_status, live))
            self.assertEqual(status.wanted(t, {}), [])
            self.assertEqual(status.held(t, {IMPL: "active"}), [(TID, lead_status, "in_progress")])
            self.assertEqual(status.held(t, {}), [])

    def test_reset_done(self):
        t = Tree(with_status(fixture(), REL, "status: done"))
        self.assertEqual(status.wanted(t, {}), [])  # done is not undone by a plain flush
        self.assertEqual([(c[0], c[2], c[3]) for c in status.wanted(t, {}, reset=[IMPL])], [(TID, "done", "planned")])
        self.assertEqual([c[0] for c in status.wanted(t, {}, reset=[TID])], [TID])  # the tree id works too
        self.assertEqual(status.wanted(t, {IMPL: "done"}, reset=[IMPL]), [])  # the coordinator has the merge

    def test_push_failure_kinds(self):
        pf = status.push_failure
        self.assertEqual(pf(" ! [rejected]        HEAD -> main (fetch first)\nerror: failed to push some refs"), "stale")
        self.assertEqual(pf(" ! [rejected]        HEAD -> main (non-fast-forward)"), "stale")
        self.assertEqual(pf(" ! [remote rejected] HEAD -> main (pre-receive hook declined)"), "refused")
        self.assertEqual(pf("remote: error: GH006: Protected branch update failed for refs/heads/main."), "refused")
        self.assertEqual(pf("fatal: unable to access 'https://example.invalid/r.git/': Could not resolve host"), "error")
        self.assertEqual(pf("git@example.invalid: Permission denied (publickey)."), "error")

    def test_loop_period_follows_the_config(self):
        lead = types.SimpleNamespace(cfg=lambda: types.SimpleNamespace(raw={"spec": {"status_flush_s": 5}}))
        self.assertEqual(status.StatusLoop(lead).period(), 5)
        self.assertEqual(status.StatusLoop(lead, every=2).period(), 2)
        lead0 = types.SimpleNamespace(cfg=lambda: types.SimpleNamespace(raw={"spec": {"status_flush_s": 0}}))
        self.assertEqual(status.StatusLoop(lead0).period(), 1)  # never a busy loop

    def test_parked_without_a_pr_is_not_in_review(self):
        snap = {"leases": [{"task": "a", "attempt": 1, "state": "parked", "reason": "done"},
                           {"task": "b", "attempt": 1, "state": "parked", "reason": "done"}],
                "attempts": [{"task": "a", "n": 1, "state": "done", "pr_url": None},
                             {"task": "b", "n": 1, "state": "done", "pr_url": "https://example.invalid/pull/3"}]}
        self.assertEqual(LeadBase.states(None, snap), {"a": "active", "b": "review"})


# ---------------------------------------------------------------- the writer on real git, with a stub lead

class StubLead(object):
    def __init__(self, case, states, **cfg):
        self.case, self.repo, self._states = case, case.repo, dict(states)
        self.raw = dict({"spec": {"status_flush_s": 0}}, **cfg)
        self.records, self.ctx, self.coord = [], None, self
        self.idx = os.path.join(case.tmp, "index")
        self.hook = None  # called once from spec_files and default_branch: a concurrent fetch

    def cfg(self):
        c = self.case.config(**self.raw)
        return c

    def states(self, snap=None):
        return dict(self._states)

    def _race(self):
        h, self.hook = self.hook, None
        if h:
            h()

    def fetch(self, force=False):
        worktrees.fetch(self.repo)
        return True

    def default_branch(self):
        self._race()
        return "main"

    def spec_dir(self):
        return "specs"

    def spec_files(self):
        self.fetch()
        files = source.read_ref(self.repo, "refs/remotes/origin/main", "specs")
        self._race()
        return files or source.read_tree(self.repo, "specs")

    def launcher(self):
        return types.SimpleNamespace(index_dir=lambda: self.idx)

    def root(self):
        return self.repo

    def record(self, ctx, kind, payload, **kw):
        self.records.append((kind, payload))

    def writer(self):
        return status.StatusWriter(self)


class WriterCase(LauncherTestCase):
    FILES = None

    def setUp(self):
        super().setUp()
        files = fixture() if self.FILES is None else self.FILES
        self.put(files, "specs")

    def put(self, files, msg):
        sh(["git", "pull", "-q", "origin", "main"], self.seed)
        for rel, text in files.items():
            p = os.path.join(self.seed, "specs", rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", newline="") as f:
                f.write(text)
        sh(["git", "add", "-A"], self.seed)
        sh(["git", "commit", "-q", "-m", msg], self.seed)
        sh(["git", "push", "-q", "origin", "main"], self.seed)

    def main_text(self, rel=REL):
        return sh(["git", "show", "main:specs/" + rel], self.origin)

    def protect_main(self):
        hook = os.path.join(self.origin, "hooks", "pre-receive")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\nwhile read old new ref; do\n"
                    "  if [ \"$ref\" = refs/heads/main ] && git log -1 --format=%s \"$new\" | grep -q '^wavecoord: status';"
                    " then echo 'main is protected' >&2; exit 1; fi\ndone\n")
        os.chmod(hook, os.stat(hook).st_mode | stat.S_IXUSR)


class WriterTest(WriterCase):
    def test_the_commit_is_built_on_the_files_it_read(self):
        lead = StubLead(self, {IMPL: "active"})
        fs = fixture()
        retitled = {REL: fs[REL].replace("title: ", "title: Renamed ", 1)}

        def race():  # a plan PR lands, and another thread fetches, in the middle of the flush
            self.put(retitled, "plan: retitle")
            sh(["git", "fetch", "-q", "origin"], self.repo)
        lead.hook = race
        out = lead.writer().flush(force=True)
        self.assertEqual(out.get("via"), "push", out)
        text = self.main_text()
        self.assertIn("title: Renamed ", text)
        self.assertIn("status: in_progress\n", text)
        diff = sh(["git", "show", "--format=", "-U0", "main"], self.origin)
        changed = [l for l in diff.split("\n") if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
        self.assertEqual(changed, ["-status: planned", "+status: in_progress"], diff)

    def test_no_working_tree_fallback(self):
        sh(["git", "pull", "-q", "origin", "main"], self.seed)
        sh(["git", "rm", "-q", "-r", "specs"], self.seed)
        sh(["git", "commit", "-q", "-m", "no specs"], self.seed)
        sh(["git", "push", "-q", "origin", "main"], self.seed)
        for rel, text in fixture().items():  # the lead's uncommitted local tree
            p = os.path.join(self.repo, "specs", rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(text)
        out = StubLead(self, {IMPL: "active"}).writer().flush(force=True)
        self.assertEqual(out, {"changes": []})
        self.assertNotIn("wavecoord: status", sh(["git", "log", "--format=%s", "main"], self.origin))

    def test_crlf_and_commented_lines_and_errors_never_escape(self):
        fs = fixture()
        self.put({REL: fs[REL].replace("status: planned\n", "status: planned  # waiting on infra\n", 1),
                  TESTS_FILE: fs[TESTS_FILE].replace("\n", "\r\n")}, "odd lines")
        out = StubLead(self, {IMPL: "active", TESTS_ID: "active"}).writer().flush(force=True)
        self.assertEqual(sorted(c["task"] for c in out.get("changes", [])), [IMPL, TESTS_ID], out)
        self.assertIn("status: in_progress  # waiting on infra\n", self.main_text())
        raw = subprocess.run(["git", "cat-file", "-p", "main:specs/" + TESTS_FILE], cwd=self.origin, check=True,
                             stdout=subprocess.PIPE).stdout
        self.assertIn(b"status: in_progress\r\n", raw)
        self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"))
        # any failure in the flush is a blocked result and an event, never an exception
        lead = StubLead(self, {IMPL: "review"})
        real = edit.commit_files

        def boom(*a, **kw):
            raise LauncherError("git worktree add failed: disk full")
        edit.commit_files = boom
        try:
            out = lead.writer().flush(force=True)
        finally:
            edit.commit_files = real
        self.assertTrue(out.get("blocked"), out)
        self.assertIn("disk full", out["reason"])
        self.assertEqual([k for k, _ in lead.records], ["spec.status_blocked"])

    def test_a_busy_writer_does_not_block_callers(self):
        lead = StubLead(self, {IMPL: "active"}, spec={"status_flush_s": 60})
        w = lead.writer()
        w.lock.acquire()
        try:
            res = {}
            th = threading.Thread(target=lambda: res.update(out=w.flush()))
            th.start()
            th.join(3)
            self.assertFalse(th.is_alive(), "a non-forced flush waited for the lock")
            self.assertEqual(res["out"].get("skipped"), "busy")
            t0 = time.time()
            out = w.flush(force=True)  # a merge's flush: queued for the running flush, not waited for
            self.assertLess(time.time() - t0, 2)
            self.assertEqual((out.get("skipped"), out.get("queued")), ("busy", True))
            self.assertTrue(w.again)
        finally:
            w.lock.release()

    def test_a_queued_flush_runs_after_the_running_one(self):
        lead = StubLead(self, {IMPL: "active"})
        w = lead.writer()
        calls = []
        real = w._flush_locked

        def run(reset):
            calls.append(1)
            if len(calls) == 1:
                w.flush(force=True)  # arrives while the first is running
            return real(reset)
        w._flush_locked = run
        w.flush(force=True, wait=True)
        self.assertEqual(len(calls), 2)

    def test_a_push_error_is_not_a_protected_branch(self):
        sh(["git", "remote", "set-url", "--push", "origin", os.path.join(self.tmp, "nowhere.git")], self.repo)
        lead = StubLead(self, {IMPL: "active"})
        out = lead.writer().flush(force=True)
        self.assertTrue(out.get("blocked"), out)
        self.assertTrue(out["reason"].startswith("push of main failed"), out)
        self.assertFalse(os.path.exists(os.environ["FAKE_PR_OUT"]), "no status PR for a push error")

    def test_a_stale_base_is_retried(self):
        lead = StubLead(self, {IMPL: "active"})
        real = edit.commit_files
        seen = []

        def racing(*a, **kw):
            if not seen:
                seen.append(1)
                self.put({"auth/_node.md": fixture()["auth/_node.md"] + "\nOne more line.\n"}, "plan: other")
            return real(*a, **kw)
        edit.commit_files = racing
        try:
            out = lead.writer().flush(force=True)
        finally:
            edit.commit_files = real
        self.assertEqual(out.get("via"), "push", out)
        self.assertIn("status: in_progress\n", self.main_text())
        self.assertIn("One more line.", self.main_text("auth/_node.md"))

    def test_a_blocked_writer_logs_each_reason_once(self):
        self.protect_main()
        os.environ["FAKE_PR_FAIL"] = "1"
        lead = StubLead(self, {IMPL: "active"})
        w = lead.writer()
        for _ in range(3):
            self.assertTrue(w.flush(force=True).get("blocked"))
        self.assertEqual([k for k, _ in lead.records], ["spec.status_blocked"])
        lead._states = {IMPL: "review"}  # a new change: still blocked, the same reason
        w.flush(force=True)
        self.assertEqual(len(lead.records), 1)
        os.environ.pop("FAKE_PR_FAIL")
        lead.raw["pr_cmd"] = ["false"]  # a different reason is logged
        w.flush(force=True)
        self.assertEqual([k for k, _ in lead.records], ["spec.status_blocked"] * 2)


# ---------------------------------------------------------------- the status PR on a real coordinator

CLOSE_ON_DELETE = """#!/usr/bin/env python3
import json, os, sys
ref, old, new = sys.argv[1:4]
if ref == "refs/heads/wavecoord/status" and set(new) == {"0"}:
    p = os.environ["FAKE_GH_STATE"]
    st = json.load(open(p))
    for pr in st["prs"]:
        if pr["head"] == "wavecoord/status" and pr["state"] == "open":
            pr["state"] = "closed"
    json.dump(st, open(p, "w"))
"""

HELD_MERGE = ('test ! -e "$STATUS_HOLD" || { echo "required status checks are pending" >&2; exit 1; }; '
              'gh pr merge "$WAVECOORD_PR" --merge --match-head-commit "$WAVECOORD_SHA"')


class StatusPrTest(StatusFlowCase):
    CHECKS = {"verify": "true", "acceptance": "true"}
    EXTRA_CFG = {"spec": {"status_flush_s": 0}, "merge_cmd": HELD_MERGE}

    def setUp(self):
        super().setUp()
        for name, text in (("pre-receive", "#!/bin/sh\nwhile read old new ref; do\n"
                            "  if [ \"$ref\" = refs/heads/main ] && git log -1 --format=%s \"$new\" | grep -q "
                            "'^wavecoord: status'; then echo 'main is protected' >&2; exit 1; fi\ndone\n"),
                           ("update", CLOSE_ON_DELETE)):
            hook = os.path.join(self.origin, "hooks", name)
            with open(hook, "w") as f:
                f.write(text)
            os.chmod(hook, 0o755)
        self.hold = os.path.join(self.tmp, "hold")
        open(self.hold, "w").close()
        self.addCleanup(self._restore, "STATUS_HOLD", os.environ.get("STATUS_HOLD"))
        os.environ["STATUS_HOLD"] = self.hold
        self.lead.states = lambda snap=None: {IMPL: "active"}

    def test_the_open_status_pr_is_kept_and_merged(self):
        out = self.lead.status_flush(force=True)
        self.assertTrue(out.get("blocked"), out)
        self.assertIn("required status checks are pending", out["reason"])
        os.remove(self.hold)
        out = self.lead.status_flush(force=True)
        self.assertEqual((out.get("via"), out.get("merged")), ("pr", True), out)
        prs = self.gh()["prs"]
        self.assertEqual([(p["head"], p["state"]) for p in prs], [("wavecoord/status", "merged")])
        self.assertEqual(self.main_status(), "in_progress")
        merges = [c for c in self.gh()["calls"] if c[:2] == ["pr", "merge"]]
        self.assertEqual(merges[-1][2], "1")  # WAVECOORD_PR reached a string merge_cmd
        self.assertEqual(len(self.events("spec.status_blocked")), 1)

    def test_a_closed_status_pr_is_replaced(self):
        self.assertTrue(self.lead.status_flush(force=True).get("blocked"))
        st = self.gh()
        st["prs"][0]["state"] = "closed"
        with open(self.gh_state, "w") as f:
            json.dump(st, f)
        os.remove(self.hold)
        out = self.lead.status_flush(force=True)
        self.assertEqual((out.get("via"), out.get("merged"), out.get("pr_url")),
                         ("pr", True, "https://example.invalid/pull/2"), out)
        self.assertEqual(self.main_status(), "in_progress")

    def test_reset_a_rolled_back_done(self):
        self.lead.states = lambda snap=None: {}
        err, out = self.mcp("spec_status_sync", {"reset": [STORE]})
        self.assertFalse(err, out)
        self.assertTrue(out.get("blocked"), out)  # main is protected: through the status PR
        os.remove(self.hold)
        out = self.lead.status_flush(force=True, wait=True, reset=[STORE])
        self.assertEqual([(c["task"], c["from"], c["to"]) for c in out["changes"]], [(STORE, "done", "planned")])
        self.assertEqual(self.main_status("specs/auth/sessions/session-store/tasks/store-api.md"), "planned")
        err, out = self.mcp("spec_status_sync", {"reset": ["no.such.task"]})
        self.assertTrue(err, out)


# ---------------------------------------------------------------- commit_files and spec_edit on real git

EXEC = {"model": "m", "effort": "low", "max_turns": 5, "budget_usd": 1, "timeout_min": 5}


def small_tree():
    ops = [{"op": "create", "id": "cap", "fields": {"title": "Cap"}, "sections": {"Intent": "Do it."}},
           {"op": "create", "id": "cap/f", "fields": {"title": "F"}, "sections": {"Intent": "Do it."}},
           {"op": "create", "id": "cap/f/r", "fields": {"title": "R"}, "sections": {"Statement": "The system shall work."}},
           {"op": "create", "id": "cap/f/r/t", "fields": {"title": "T", "paths": ["src/a.py"], "class": "B", "size": "S",
                                                          "exec": EXEC}, "sections": {"Context": "Ctx.", "Prompt": "Do."}},
           {"op": "create", "id": "cap2", "fields": {"title": "Cap2"}, "sections": {"Intent": "Do it."}}]
    ch, _, _ = apply_ops({}, ops)
    return {k: v for k, v in ch.items() if v is not None}


class EditGitTest(WriterCase):
    FILES = small_tree()

    def spec_edit(self, ops, **kw):
        kw.setdefault("wt_root", os.path.join(self.tmp, "wt"))
        return edit.spec_edit(self.repo, "specs", ops, **kw)

    def tree(self, commit):
        return sh(["git", "ls-tree", "-r", "--name-only", commit], self.repo).split("\n")

    def test_consumer_hooks_do_not_stop_bot_commits(self):
        hooks = os.path.join(self.repo, ".consumer-hooks")
        os.makedirs(hooks)
        for name in ("commit-msg", "pre-commit", "pre-push"):
            with open(os.path.join(hooks, name), "w") as f:
                f.write("#!/bin/sh\necho 'commitlint: type must be feat|fix|chore' >&2\nexit 1\n")
            os.chmod(os.path.join(hooks, name), 0o755)
        sh(["git", "config", "core.hooksPath", hooks], self.repo)
        out = self.spec_edit([{"op": "set", "id": "cap2", "key": "title", "value": "Cap two"}])
        self.assertIn("specs/cap2/_node.md", self.tree(out["commit"]))
        out = StubLead(self, {}).writer().flush(force=True)  # nothing to do, but no exception either
        self.assertEqual(out, {"changes": []})

    def test_worktree_admin_takes_the_lock(self):
        held, real = [], worktrees.AdminLock
        log = []

        class Rec(real):
            def __enter__(self):
                held.append(1)
                return real.__enter__(self)

            def __exit__(self, *a):
                held.pop()
                return real.__exit__(self, *a)
        from wavecoord.launcher import common
        real_git_run, real_git_fn = common.git_run, common.git

        def spy(fn):
            def g(args, cwd, *a, **kw):
                w = args[2:] if args[:1] == ["-c"] else args
                if w[:1] == ["worktree"]:
                    log.append((w[1], bool(held)))
                return fn(args, cwd, *a, **kw)
            return g
        worktrees.AdminLock, common.git_run, common.git = Rec, spy(real_git_run), spy(real_git_fn)
        try:
            self.spec_edit([{"op": "set", "id": "cap2", "key": "title", "value": "Cap two"}])
        finally:
            worktrees.AdminLock, common.git_run, common.git = real, real_git_run, real_git_fn
        mine = [x for x in log if x[0] in ("add", "remove")]
        self.assertTrue(mine, log)
        self.assertTrue(all(h for _, h in log), log)

    def test_delete_then_move_into_the_freed_id(self):
        out = self.spec_edit([{"op": "delete", "id": "cap2"}, {"op": "mv", "from": "cap", "to": "cap2"}])
        names = self.tree(out["commit"])
        self.assertIn("specs/cap2/f/r/tasks/t.md", names)
        self.assertFalse([n for n in names if n.startswith("specs/cap/")], names)
        self.assertIn("title: Cap\n", sh(["git", "show", "%s:specs/cap2/_node.md" % out["commit"]], self.repo) + "\n")

    def test_move_then_delete_deletes(self):
        out = self.spec_edit([{"op": "mv", "from": "cap/f/r/t", "to": "cap/f/r/t2"}, {"op": "delete", "id": "cap/f/r/t2"}])
        names = self.tree(out["commit"])
        self.assertFalse([n for n in names if "/tasks/" in n], names)
        out = self.spec_edit([{"op": "mv", "from": "cap/f", "to": "cap/g"}, {"op": "delete", "id": "cap/g/r/t"}])
        names = self.tree(out["commit"])
        self.assertFalse([n for n in names if "/tasks/" in n], names)
        self.assertIn("specs/cap/g/r/_node.md", names)

    def test_no_writes_through_a_symlink(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        sh(["git", "pull", "-q", "origin", "main"], self.seed)
        os.symlink(outside, os.path.join(self.seed, "specs", "cap9"))
        sh(["git", "add", "-A"], self.seed)
        sh(["git", "commit", "-q", "-m", "link"], self.seed)
        sh(["git", "push", "-q", "origin", "main"], self.seed)
        with self.assertRaises(edit.EditError) as cm:
            self.spec_edit([{"op": "create", "id": "cap9", "fields": {"title": "X"}, "sections": {"Intent": "X."}}])
        self.assertIn("symlink", str(cm.exception))
        self.assertEqual(os.listdir(outside), [])

    def test_a_merged_or_closed_branch_is_not_reused(self):
        bodies = []

        def pr(br, base, t, b):
            bodies.append(b)
            return "https://example.invalid/pull/%d" % len(bodies), None
        out = self.spec_edit([{"op": "set", "id": "cap2", "key": "title", "value": "Cap two"}], pr=pr)
        self.assertEqual(out["branch"], "plan/1")
        # the PR is merged; the branch stays on origin
        sh(["git", "pull", "-q", "origin", "main"], self.seed)
        sh(["git", "fetch", "-q", "origin"], self.seed)
        sh(["git", "merge", "-q", "--no-ff", "-m", "merge", "origin/plan/1"], self.seed)
        sh(["git", "push", "-q", "origin", "main"], self.seed)
        with self.assertRaises(edit.EditError) as cm:
            self.spec_edit([{"op": "set", "id": "cap", "key": "title", "value": "Cap one"}], branch="plan/1", pr=pr)
        self.assertIn("merged", str(cm.exception))
        # a second branch whose PR was closed
        out = self.spec_edit([{"op": "set", "id": "cap", "key": "title", "value": "Cap one"}], pr=pr)
        self.assertEqual(out["branch"], "plan/2")
        with self.assertRaises(edit.EditError) as cm:
            self.spec_edit([{"op": "set", "id": "cap", "key": "title", "value": "Cap 1"}], branch="plan/2", pr=pr,
                           pr_state=lambda br: ("CLOSED", None))
        self.assertIn("closed", str(cm.exception))
        # a branch with no PR (its creation failed): reusing it opens one
        out = self.spec_edit([{"op": "set", "id": "cap", "key": "title", "value": "Cap 1"}], branch="plan/2", pr=pr,
                             pr_state=lambda br: (None, "no pull requests found for branch plan/2"))
        self.assertEqual(out["pr"], "https://example.invalid/pull/3")
        out = self.spec_edit([{"op": "set", "id": "cap", "key": "title", "value": "Cap 2"}], branch="plan/2", pr=pr,
                             pr_state=lambda br: ("OPEN", None))
        self.assertIsNone(out["pr"])
        self.assertEqual(len(bodies), 3)

    def test_the_pr_body_reports_errors_on_the_base(self):
        fs = small_tree()
        self.put({"cap/f/r/tasks/t.md": fs["cap/f/r/tasks/t.md"].replace("class: B", "class: Z")}, "broken")
        bodies = []
        self.spec_edit([{"op": "set", "id": "cap2", "key": "title", "value": "Cap two"}],
                       pr=lambda br, base, t, b: (bodies.append(b) or ("https://example.invalid/pull/1", None)))
        self.assertNotIn("no errors", bodies[0])
        self.assertIn("`wavecoord spec check`: 1 error", bodies[0])


if __name__ == "__main__":
    unittest.main()
