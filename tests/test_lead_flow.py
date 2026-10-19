"""The lead flow after the second review: what the refinery merges (a done report, a task the plan still wants),
per-item failures, protected approvals, resolver limits and fallbacks, the status PR after a restart, plan
selection, and spec_edit's checks. Real server, launcher, fake claude and fake gh; no model."""

import json
import os
import stat
import unittest

import _paths  # noqa: F401
from launcher_env import sh
from test_lead import LeadTestCase, spec_md
from test_spec_lead import SpecLeadCase
from test_spec_status import IMPL, IMPL_FILE, StatusFlowCase

from wavecoord import feedback
from wavecoord.core import ApiError
from wavecoord.specs import source
from wavecoord.specs.ops import apply_ops
from wavecoord.specs.plan import SpecTreePlan
from wavecoord.specs.status import set_status_line

TESTS = "auth.sessions.token-rotation.rotate-tests"
REQ = "auth/sessions/token-rotation"


def env(**kw):
    os.environ.update(kw)


class FlowCase(LeadTestCase):
    SPECS = {"head": spec_md("head", ["x.txt"]),
             "solo": spec_md("solo", ["solo/**"]),
             "late": spec_md("late", ["late/**"], provider="alt", fallback=["anthropic"])}

    def setUp(self):
        super().setUp()
        for k in ("FAKE_CLAUDE_COMMIT", "FAKE_CLAUDE_COMMIT_FILE", "FAKE_CLAUDE_COMMIT_TEXT", "FAKE_CLAUDE_STATUS",
                  "FAKE_CLAUDE_MODES", "FAKE_CLAUDE_SLEEP", "WAVECOORD_MAX_RESOLVES"):
            self.addCleanup(self._restore, k, os.environ.get(k))

    def run_task(self, task, fname, status=None):
        env(FAKE_CLAUDE_COMMIT="1", FAKE_CLAUDE_COMMIT_FILE=fname, FAKE_CLAUDE_COMMIT_TEXT="{task}\n")
        if status:
            env(FAKE_CLAUDE_STATUS=status)
        else:
            os.environ.pop("FAKE_CLAUDE_STATUS", None)
        d = self.lead.dispatch(task)
        self.collect_until(d["id"])
        return d

    def merges(self):
        return [c for c in self.gh()["calls"] if c[:2] == ["pr", "merge"]]

    def set_cfg(self, **keys):
        p = os.path.join(self.repo, ".claude", "wavecoord.json")
        with open(p) as f:
            cfg = json.load(f)
        cfg.update(keys)
        with open(p, "w") as f:
            json.dump(cfg, f)
        st = os.stat(p).st_mtime + 5
        os.utime(p, (st, st))


class ReportStatusTest(FlowCase):
    def test_only_a_done_report_merges(self):
        """Items 11 and 70: failed, blocked and needs_decision reports are never merged."""
        for task, status in (("solo", "blocked"), ("late", "needs_decision")):
            self.run_task(task, "%s/f.txt" % task, status)
        out = self.lead.refinery_merge()
        self.assertEqual(sorted((r["task"], r["state"], r["reason"]) for r in out["results"]),
                         [("late", "blocked", "report_not_done"), ("solo", "blocked", "report_not_done")])
        self.assertIn("status is blocked", [r for r in out["results"] if r["task"] == "solo"][0]["detail"])
        self.assertEqual(self.merges(), [])
        self.assertEqual(self.events("resolve.start"), [])



class HostPrUrlTest(FlowCase):
    def test_collect_ignores_a_pr_url_from_the_host(self):
        """Review 2, item 82: the PR of an attempt is the one the lead opened, never a URL from result.json."""
        env(FAKE_CLAUDE_COMMIT="1", FAKE_CLAUDE_COMMIT_FILE="solo/f.txt", FAKE_CLAUDE_COMMIT_TEXT="solo\n")
        os.environ.pop("FAKE_CLAUDE_STATUS", None)
        d = self.lead.dispatch("solo")
        self.wait_host(d["id"])
        sd = self.lead.launcher().result(d["id"])["state_dir"]
        for name in ("result.json", "status.json"):  # both are on the host, where the worker runs
            with open(os.path.join(sd, name)) as f:
                data = json.load(f)
            data["pr_url"] = "https://github.com/acme/other/pull/7"
            with open(os.path.join(sd, name), "w") as f:
                json.dump(data, f)
        self.lead.collect()
        pr = [p for p in self.gh()["prs"] if p["head"] == d["branch"]]
        self.assertEqual(len(pr), 1, self.gh()["prs"])
        self.assertNotIn("acme/other", self.attempt("solo", 1)["pr_url"])
        self.assertEqual([i["pr_url"] for i in self.lead.refinery_queue()], [self.attempt("solo", 1)["pr_url"]])



class LeaseReleaseTest(FlowCase):
    def test_lead_releases_a_parked_lease_but_not_an_active_one(self):
        """The triage step "steal and redispatch" has a tool: lease_release (MCP) and `wavecoord lease release`."""
        env(FAKE_CLAUDE_SLEEP="20")
        self.lead.dispatch("head")
        err, out = self.mcp("lease_release", {"task": "head"})
        self.assertTrue(err, out)
        self.assertIn("a worker holds it", str(out))
        os.environ.pop("FAKE_CLAUDE_SLEEP", None)
        self.run_task("solo", "solo/f.txt")
        self.assertEqual(self.core.state.task_lease("solo")["state"], "parked")
        err, out = self.mcp("lease_release", {"task": "solo", "reason": "redo"})
        self.assertFalse(err, out)
        self.assertEqual((out["state"], out["task"]), ("released", "solo"))
        self.assertIsNone(self.core.state.task_lease("solo"))
        err, out = self.mcp("lease_release", {})
        self.assertTrue(err, out)


class HeldFlatTaskTest(FlowCase):
    def test_a_dropped_task_is_not_merged(self):
        """Items 3 and 14 (flat plan): the lead dropped the task on the default branch after its PR opened."""
        self.run_task("solo", "solo/f.txt")
        self.push_plan({"solo": spec_md("solo", ["solo/**"], state="dropped")})
        r = self.lead.refinery_merge()["results"][0]
        self.assertEqual((r["state"], r["reason"]), ("blocked", "held"), r)
        self.assertIn("dropped", r["detail"])
        self.assertEqual(self.merges(), [])


class PerItemFailureTest(FlowCase):
    def test_one_failing_item_does_not_stop_the_loop(self):
        """Item 70: protected_paths_cmd fails for the second PR: it is blocked `unavailable`, the first stays merged,
        and the call answers with every item."""
        marker = os.path.join(self.tmp, "called")
        prot = os.path.join(self.tmp, "protected")
        with open(prot, "w") as f:
            f.write("#!/bin/sh\nif [ -f %s ]; then echo transient >&2; exit 1; fi\ntouch %s\necho 'nothing/**'\n"
                    % (marker, marker))
        os.chmod(prot, 0o755)
        self.set_cfg(protected_paths_cmd=[prot])
        self.run_task("solo", "solo/f.txt")
        self.run_task("head", "x.txt")
        out = self.lead.refinery_merge()
        got = [(r["task"], r["state"], r.get("reason")) for r in out["results"]]
        self.assertEqual([g[1:] for g in got], [("merged", None), ("blocked", "unavailable")], got)
        self.assertIn("transient", out["results"][1]["detail"])
        self.assertEqual(len(self.merges()), 1)

    def test_approved_protected_needs_a_pr(self):
        """Item 71: an approval covers one PR the user saw, never the queue."""
        with self.assertRaises(ApiError) as e:
            self.lead.refinery_merge(approved_protected=True)
        self.assertEqual(e.exception.code, "bad_request")
        err, out = self.mcp("refinery_merge", {"approved_protected": True})
        self.assertTrue(err)


class ResolverLimitTest(FlowCase):
    EXTRA_CFG = {"auto_resolve": False}

    def conflict(self):
        self.run_task("head", "x.txt")
        self.run_task("solo", "x.txt")  # clean now; a conflict once head merges

    def test_refinery_respects_auto_resolve_and_max_resolves(self):
        """Item 65: no resolver with auto_resolve off; with it on, at most MAX_RESOLVES runs per attempt."""
        self.conflict()
        out = self.lead.refinery_merge()
        solo = [r for r in out["results"] if r["task"] == "solo"][0]
        self.assertEqual(solo["reason"], "conflict")
        self.assertNotIn("resolver", solo)
        self.assertIn("auto_resolve is off", solo["resolve_note"])
        self.assertEqual(self.events("resolve.start"), [])
        # auto_resolve on, MAX_RESOLVES 1: one resolver; it leaves the conflict; the next round starts none
        self.set_cfg(auto_resolve=True)
        env(WAVECOORD_MAX_RESOLVES="1")
        os.environ.pop("FAKE_CLAUDE_COMMIT", None)
        r = self.lead.refinery_merge("solo/1")["results"][0]
        self.assertEqual(r["resolver"]["id"], "solo-1")
        self.collect_until("solo-1")
        r = self.lead.refinery_merge("solo/1")["results"][0]
        self.assertEqual(r["reason"], "conflict")
        self.assertIn("MAX_RESOLVES", r["resolve_error"])
        self.assertEqual(len(self.events("resolve.start")), 1)

    def test_no_second_resolver_while_one_runs(self):
        """Item 69: resolve refuses an attempt whose run is in flight; the running lease is not touched."""
        env(FAKE_CLAUDE_SLEEP="3")
        d = self.lead.dispatch("solo")
        lease = self.core.state.task_lease("solo")
        with self.assertRaises(ApiError) as e:
            self.lead.resolve(attempt="solo/1")
        self.assertIn("running", e.exception.message)
        self.assertEqual(self.core.state.task_lease("solo")["epoch"], lease["epoch"])
        self.assertEqual(self.wait_host(d["id"])["state"], "reported")

    def test_the_users_decision_reaches_the_resolver(self):
        """Items 72 and 74: resolve takes a brief (the user's decision); the resolver brief puts it first."""
        brief = self.lead.resolver_brief("solo", 1, "wave/solo-1", "main", ["x.txt"], None, None,
                                         self.lead.snapshot(), decision="Keep the head side's behaviour.")
        self.assertIn("## The lead's decision", brief)
        self.assertLess(brief.index("Keep the head side"), brief.index("## Conflicting files"))
        self.assertIn("as the lead's decision says", brief)
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        import urllib.request
        r = urllib.request.Request(self.url + "/mcp", data=json.dumps(req).encode(), method="POST",
                                   headers={"Authorization": "Bearer lead-test-token",
                                            "Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=60) as f:
            tools = {t["name"]: t for t in json.loads(f.read().decode())["result"]["tools"]}
        self.assertIn("brief", tools["resolve"]["inputSchema"]["properties"])


class FallbackTest(FlowCase):
    def rate_limit(self, key):
        modes = os.path.join(self.tmp, "modes.json")
        with open(modes, "w") as f:
            json.dump({key: {"rate_limit": True}}, f)
        env(FAKE_CLAUDE_MODES=modes)
        return modes

    def test_fallback_respects_the_plan(self):
        """Items 68 and 86: the fallback after a rate limit is a normal dispatch: a task dropped meanwhile is not
        started again."""
        self.rate_limit("late")
        d = self.lead.dispatch("late")
        self.assertEqual(self.wait_host(d["id"])["throttled"], True)
        self.push_plan({"late": spec_md("late", ["late/**"], provider="alt", fallback=["anthropic"],
                                        state="dropped")})
        item = [c for c in self.lead.collect()["collected"] if c.get("id") == "late-1"][0]
        self.assertNotIn("fallback", item)
        self.assertIn("not ready", item["fallback_error"])
        self.assertIsNone(self.attempt("late", 2))

    def test_a_rate_limited_resolver_runs_again(self):
        """Item 67: a rate-limited resolver keeps the attempt reported with its PR, and the resolver (not a new
        attempt) goes on with the next provider."""
        self.run_task("head", "x.txt")
        self.run_task("late", "x.txt")
        os.environ.pop("FAKE_CLAUDE_COMMIT", None)
        self.rate_limit("late:resolve")
        out = self.lead.refinery_merge()
        late = [r for r in out["results"] if r["task"] == "late"][0]
        self.assertEqual(late["resolver"]["provider"], "alt")
        self.wait_host("late-1")
        item = [c for c in self.lead.collect()["collected"] if c.get("id") == "late-1"][0]
        self.assertIn("fallback", item, item)
        self.assertEqual((item["fallback"]["id"], item["fallback"]["provider"]), ("late-1", "anthropic"))
        self.assertIsNone(self.attempt("late", 2))
        ends = [e["payload"] for e in self.events("resolve.end")]
        self.assertEqual([e["ok"] for e in ends], [False])
        self.assertEqual([e["payload"]["state"] for e in self.events("attempt.end") if e["task"] == "late"][-1],
                         "reported")
        self.wait_host("late-1")
        item = [c for c in self.lead.collect()["collected"] if c.get("id") == "late-1"][0]
        self.assertNotIn("fallback", item)  # anthropic was the last provider of the chain
        self.assertEqual([e["payload"]["ok"] for e in self.events("resolve.end")], [False, False])
        a = self.attempt("late", 1)
        self.assertEqual((a["state"], a["pr_url"]), ("reported", "https://example.invalid/pull/2"))


class SpecHeldTest(StatusFlowCase):
    CHECKS = {"verify": "true", "acceptance": "true"}

    def test_a_task_dropped_on_main_is_not_merged(self):
        """Items 3 and 14 (spec tree): the plan drops the task while its PR waits; the refinery holds it."""
        self.run_impl()
        sh(["git", "pull", "-q", "origin", "main"], self.seed)
        p = os.path.join(self.seed, IMPL_FILE)
        with open(p) as f:
            text = f.read()
        with open(p, "w") as f:
            f.write(set_status_line(text, "dropped"))
        sh(["git", "commit", "-qam", "drop"], self.seed)
        sh(["git", "push", "-q", "origin", "main"], self.seed)
        r = self.lead.refinery_merge()["results"][0]
        self.assertEqual((r["state"], r["reason"]), ("blocked", "held"), r)
        self.assertEqual([c for c in self.gh()["calls"] if c[:2] == ["pr", "merge"]], [])


class SpecVerifyAliasTest(SpecLeadCase):
    def test_last_task_check_follows_aliases(self):
        """Item 6: the sibling moved (aliased) and merged under its old id: this merge is the requirement's last,
        so Verify runs; a sibling that does not parse fails closed."""
        files = source.read_tree(_paths.FIXTURES, "specs")
        changes, _, _ = apply_ops(files, [{"op": "mv", "from": REQ + "/rotate-impl", "to": REQ + "/impl2"}])
        self.push_files(changes)
        ran = []
        self.lead.states = lambda snap=None: {IMPL: "done"}
        self.lead._merge_result = lambda sha, default: ("0" * 40, None)
        self.lead._check = lambda cmd, *a, **k: (ran.append(cmd) or True, None)
        it = {"task": TESTS, "attempt": 1, "pr": "1", "pr_url": "u", "branch": "wave/x-1"}
        v = self.lead._verify(it, "0" * 40, "main")
        self.assertEqual((v["ok"], v["spec"]["requirement_pending"], v["spec"]["verified"]), (True, [], True), v)
        rel = "auth/sessions/token-rotation/tasks/impl2.md"
        self.push_files({rel: "---\nstatus: planned\nkind: task\n: broken\n"})
        v = self.lead._verify(it, "0" * 40, "main")
        self.assertEqual((v["ok"], v["reason"]), (False, "verify_unavailable"), v)

    def push_files(self, changes):
        sh(["git", "pull", "-q", "origin", "main"], self.seed)
        for rel, text in changes.items():
            p = os.path.join(self.seed, "specs", rel)
            if text is None:
                if os.path.exists(p):
                    os.remove(p)
                continue
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(text)
        sh(["git", "add", "-A"], self.seed)
        sh(["git", "commit", "-qm", "specs"], self.seed)
        sh(["git", "push", "-q", "origin", "main"], self.seed)
        self.lead._fetched = 0.0


class PlanSelectionTest(SpecLeadCase):
    def test_a_read_error_fails_closed(self):
        """Item 9: when the default branch cannot be read, the lead does not fall back to the flat plan."""
        def broken():
            raise ApiError("unavailable", "cannot read the spec tree: cat-file hiccup")
        self.lead.spec_files = broken
        with self.assertRaises(ApiError) as e:
            self.lead.plan()
        self.assertIn("cannot choose the plan store", e.exception.message)
        self.assertEqual(self.lead.plan_store()[0], "unavailable")

    def test_spec_tools_say_when_the_tree_is_not_the_plan(self):
        """Item 10: with plan.adapter builtin, spec_tree and spec_waves say the tree is not dispatched."""
        self.assertNotIn("plan_note", self.tool("spec_tree"))
        p = os.path.join(self.repo, ".claude", "wavecoord.json")
        with open(p) as f:
            cfg = json.load(f)
        cfg["plan"] = {"adapter": "builtin", "dir": "plan/tasks"}
        with open(p, "w") as f:
            json.dump(cfg, f)
        os.utime(p, (3, 3))
        for name in ("spec_tree", "spec_waves"):
            self.assertIn("plan.adapter is builtin", self.tool(name)["plan_note"], name)

    def test_spec_edit_refuses_a_reused_task_id(self):
        """Item 2: a new task may not take the id of a deleted task the coordinator merged."""
        ops = [{"op": "delete", "id": REQ + "/rotate-tests"},
               {"op": "create", "id": REQ + "/rotate-tests", "fields": {
                   "title": "Again", "status": "planned", "paths": ["tests/again.py"], "class": "B", "size": "S",
                   "exec": {"model": "sonnet", "effort": "low", "max_turns": 5, "budget_usd": 1, "timeout_min": 5},
                   "outputs": [{"id": "tests/again.py", "kind": "file"}]},
                "sections": {"Context": "Again.", "Prompt": "Do it again."}}]
        self.lead.states = lambda snap=None: {TESTS: "done"}
        err, out = self.mcp("spec_edit", {"ops": ops})
        self.assertTrue(err)
        self.assertIn("belongs to an earlier task", json.dumps(out))
        self.lead.states = lambda snap=None: {}
        self.assertTrue(self.lead.spec_edit(ops)["branch"])

    def test_later_calls_update_the_pr_body(self):
        """Item 15: a second spec_edit on the branch sets the PR body for the whole branch."""
        out = self.tool("spec_edit", {"ops": [
            {"op": "set", "id": REQ + "/rotate-impl", "key": "title", "value": "One"}], "body": "B1"})
        out2 = self.tool("spec_edit", {"branch": out["branch"], "body": "BODY-2", "ops": [
            {"op": "set", "id": REQ + "/rotate-tests", "key": "title", "value": "Two"}]})
        self.assertIsNone(out2["pr_body_error"])
        body = self.gh()["prs"][0]["body"]
        self.assertIn("BODY-2", body)
        self.assertIn("rotate-impl.md", body)
        self.assertIn("rotate-tests.md", body)


class FirstTreeTest(LeadTestCase):
    SPECS = {"alpha": spec_md("alpha", ["alpha/**"])}

    def test_first_records_warn_of_the_switch(self):
        """Item 5: a plan PR that adds the first spec records while plan/tasks has tasks says so."""
        p = os.path.join(self.repo, ".claude", "wavecoord.json")
        with open(p) as f:
            cfg = json.load(f)
        cfg.pop("plan")
        with open(p, "w") as f:
            json.dump(cfg, f)
        os.utime(p, (1, 1))
        out = self.lead.spec_edit([{"op": "create", "id": "shop", "fields": {"title": "Shop", "status": "active"},
                                    "sections": {"Intent": "Sell."}}])
        self.assertTrue(any(w.startswith("switch:") and "migrate-plan" in w for w in out["warnings"]), out)
        self.assertIn("migrate-plan", self.gh()["prs"][0]["body"])


class StatusPrRestartTest(StatusFlowCase):
    CHECKS = {"verify": "true", "acceptance": "true"}

    def test_the_open_status_pr_is_adopted_after_a_restart(self):
        """Items 1 and 95: the status PR stays open (its merge fails); after a restart the writer adopts it instead
        of opening a second one. The block reaches the lead (its own actor, not the lead's tool call)."""
        hook = os.path.join(self.origin, "hooks", "pre-receive")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\nwhile read old new ref; do\n"
                    "  if [ \"$ref\" = refs/heads/main ] && git log -1 --format=%s \"$new\" | grep -q '^wavecoord: status';"
                    " then echo 'main is protected' >&2; exit 1; fi\ndone\n")
        os.chmod(hook, os.stat(hook).st_mode | stat.S_IXUSR)
        hold = os.path.join(self.tmp, "hold")
        with open(hold, "w") as f:
            f.write("x")
        merge = os.path.join(self.tmp, "merge.sh")
        with open(merge, "w") as f:
            f.write("#!/bin/sh\nif [ -f %s ]; then echo 'checks are pending' >&2; exit 1; fi\nexec gh \"$@\"\n" % hold)
        os.chmod(merge, 0o755)
        p = os.path.join(self.repo, ".claude", "wavecoord.json")
        with open(p) as f:
            cfg = json.load(f)
        cfg["merge_cmd"] = [merge, "pr", "merge", "{pr}", "--merge", "--match-head-commit", "{sha}"]
        with open(p, "w") as f:
            json.dump(cfg, f)
        os.utime(p, (4, 4))
        env(FAKE_CLAUDE_SLEEP="5")
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_SLEEP", None)
        self.lead.dispatch(IMPL)
        blocked = self.events("spec.status_blocked")
        self.assertTrue(blocked)
        self.assertEqual(blocked[-1]["actor"]["agent"], "status-writer")
        self.assertIsNotNone(feedback.lead_line(blocked[-1]))
        self.lead._status = None  # a restart: the writer forgets its PR
        os.remove(hold)
        out = self.lead.status_flush(force=True, wait=True)
        self.assertTrue(out.get("merged"), out)
        self.assertEqual(self.main_status(), "in_progress")
        self.assertEqual([p["head"] for p in self.gh()["prs"]].count("wavecoord/status"), 1)


if __name__ == "__main__":
    unittest.main()
