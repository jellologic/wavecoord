"""The lead tools end to end: the real P1 server, the P3 launcher on a local host, the fake claude,
a bare origin and a fake `gh` (tests/fake-gh). No network, no model."""

import hashlib
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.request

import _paths
from launcher_env import LauncherTestCase, sh

from wavecoord.core import Coord
from wavecoord.launcher.common import FINAL
from wavecoord.lead import lead_for
from wavecoord.server import Server, Tokens

AGENT_TOKEN = "local-agent-test-token"
LEAD_TOKEN = "lead-test-token"
FAKE_GH_DIR = os.path.join(_paths.TESTS, "fake-gh")


def tokens():
    rows = {AGENT_TOKEN: ("agent", "host:local"), LEAD_TOKEN: ("lead", "lead")}
    return {"version": 1, "tokens": {hashlib.sha256(t.encode()).hexdigest():
                                     {"principal": p, "role": r, "created": "2026-10-18T12:00:00Z"}
                                     for t, (r, p) in rows.items()}}


def spec_md(tid, paths, needs=(), shared=(), size="S", provider=None, fallback=None, host_needs=None,
            prompt=None, state="todo"):
    ex = ["  model: sonnet", "  effort: low", "  max_turns: 5", "  budget_usd: 1", "  timeout_min: 2"]
    if provider:
        ex.append("  provider: %s" % provider)
    if fallback:
        ex.append("  provider_fallback: [%s]" % ", ".join(fallback))
    if host_needs:
        ex.append("  host_needs: [%s]" % ", ".join(host_needs))
    lines = ["---", "id: %s" % tid, "title: Task %s" % tid, "phase: p1", "needs: [%s]" % ", ".join(needs),
             "paths: [%s]" % ", ".join(paths)]
    if shared:
        lines.append("shared_paths: [%s]" % ", ".join(shared))
    lines += ["class: B", "size: %s" % size, "state: %s" % state, "exec:"] + ex + [
        "---", "", "## Context", "Context of %s." % tid, "", "## Risks", "- risk: scope creep",
        "  mitigation: paths are reserved", "  kind: llm", "", "## Acceptance", "- `true`", "",
        "## Prompt", prompt or "Do task %s." % tid, ""]
    return "\n".join(lines)


class LeadTestCase(LauncherTestCase):
    SPECS = {}
    EXTRA_CFG = {}

    def setUp(self):
        super(LeadTestCase, self).setUp()
        os.environ["PATH"] = FAKE_GH_DIR + os.pathsep + os.environ["PATH"]
        self.gh_state = os.path.join(self.tmp, "gh.json")
        for k, v in (("FAKE_GH_STATE", self.gh_state), ("WAVECOORD_PLAN_FETCH_S", "0"),
                     ("CLAUDE_PLUGIN_OPTION_TOKEN", AGENT_TOKEN)):
            self.addCleanup(self._restore, k, os.environ.get(k))
            os.environ[k] = v
        self.alt_env = os.path.join(self.tmp, "alt.env")
        with open(self.alt_env, "w") as f:
            f.write("ALT_PROVIDER=1\n")
        os.chmod(self.alt_env, 0o600)
        self.push_plan(self.SPECS)
        cfg = {"project": "demo", "branch_prefix": "wave", "wip_cap": 3, "auto_resolve": True,
               "hosts": [{"name": "local", "slots": 3, "repo_path": self.repo, "labels": ["python3"]}],
               "providers": {"alt": {"env_file": self.alt_env, "cost_basis": "none"}},
               "plan": {"adapter": "builtin", "dir": "plan/tasks"}}
        cfg.update(self.EXTRA_CFG)
        os.makedirs(os.path.join(self.repo, ".claude"))
        with open(os.path.join(self.repo, ".claude", "wavecoord.json"), "w") as f:
            json.dump(cfg, f)
        self.core = Coord(self.repo, home=os.path.join(self.tmp, "coordhome"), push=False,
                          commit_window_ms=0).open()
        self.addCleanup(self.core.close)
        self.srv = Server(self.core, port=0, tokens=Tokens(data=tokens())).start()
        self.addCleanup(self.srv.stop)
        self.url = "http://127.0.0.1:%d" % self.srv.port
        self.lead = lead_for(self.core)

    def _restore(self, k, v):
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    def push_plan(self, specs):
        if not specs:
            return
        sh(["git", "pull", "-q", "origin", "main"], self.seed)
        d = os.path.join(self.seed, "plan", "tasks")
        os.makedirs(d, exist_ok=True)
        for tid, text in specs.items():
            with open(os.path.join(d, tid + ".md"), "w") as f:
                f.write(text)
        sh(["git", "add", "plan"], self.seed)
        sh(["git", "commit", "-q", "-m", "plan"], self.seed)
        sh(["git", "push", "-q", "origin", "main"], self.seed)

    # ----- helpers -----

    def wait_host(self, aid, timeout=60):
        L = self.lead.launcher()
        end = time.time() + timeout
        r = None
        while time.time() < end:
            r = L.result(aid)
            if r.get("state") in FINAL:
                return r
            time.sleep(0.2)
        self.fail("attempt %s did not end: %s" % (aid, r))

    def collect_until(self, aid, timeout=60):
        self.wait_host(aid, timeout)
        out = self.lead.collect()
        hits = [c for c in out["collected"] if c.get("id") == aid]
        self.assertTrue(hits, out)
        return hits[0]

    def events(self, kind=None):
        return [e for e in self.core.log.events if kind is None or e["kind"] == kind]

    def attempt(self, task, n):
        return self.core.state.get("attempts", "%s/%d" % (task, n))

    def gh(self):
        with open(self.gh_state) as f:
            return json.load(f)

    def host_file(self, aid, name):
        r = self.lead.launcher().result(aid)
        with open(os.path.join(r["state_dir"], name)) as f:
            return f.read()

    def mcp(self, name, args=None, token=LEAD_TOKEN):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}
        req = urllib.request.Request(self.url + "/mcp", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            res = json.loads(r.read().decode())["result"]
        text = res["content"][0]["text"]
        try:
            return res["isError"], json.loads(text)
        except ValueError:
            return res["isError"], text


class ExternalMergeTest(LeadTestCase):
    """A PR merged (or closed) outside the refinery releases its parked lease (0.1.1 fix)."""
    SPECS = {"one": spec_md("one", ["core/**"]), "two": spec_md("two", ["core/**"])}

    def set_pr(self, n, state):
        st = self.gh()
        st["prs"][n - 1]["state"] = state
        with open(self.gh_state, "w") as f:
            json.dump(st, f)

    def ready(self):
        return [t["id"] for t in self.lead.plan_ready()["tasks"]]

    def test_merged_and_closed_prs_release_the_lease(self):
        os.environ.update(FAKE_CLAUDE_COMMIT="1", FAKE_CLAUDE_COMMIT_FILE="{task}/work.txt")
        self.lead.dispatch("one")
        item = self.collect_until("one-1")
        self.assertIn("pr", item["actions"])
        self.assertEqual(self.core.state.task_lease("one")["state"], "parked")
        self.assertNotIn("two", self.ready())  # the parked lease holds core/** exclusive
        # an open PR changes nothing
        self.assertEqual(self.lead.collect()["parked"], [])
        # the consumer merges PR 1 with its own gate, not the refinery
        self.set_pr(1, "merged")
        self.assertEqual([p["state"] for p in self.lead.collect(dry_run=True)["parked"]], ["merged"])
        self.assertEqual(self.core.state.task_lease("one")["state"], "parked")  # a dry run changes nothing
        out = self.lead.collect()
        self.assertEqual([(p["task"], p["attempt"], p["state"]) for p in out["parked"]], [("one", 1, "merged")])
        rel = self.events("lease.release")[-1]
        self.assertEqual((rel["task"], rel["payload"]["reason"]), ("one", "merged"))
        self.assertEqual(self.attempt("one", 1)["state"], "merged")
        self.assertEqual([r for r in self.core.state.live_reservations() if r["task"] == "one"], [])
        self.assertIn("two", self.ready())
        n = self.core.log.last_seq
        self.assertEqual(self.lead.collect()["parked"], [])  # once only
        self.assertEqual(self.core.log.last_seq, n)
        self.assertIn(["pr", "view", "1", "--json", "state,mergedAt"], self.gh()["calls"])
        # a PR closed without a merge: release, and the attempt is abandoned
        self.lead.dispatch("two")
        self.collect_until("two-1")
        self.set_pr(2, "closed")
        out = self.lead.collect()
        self.assertEqual([(p["task"], p["state"]) for p in out["parked"]], [("two", "abandoned")])
        self.assertEqual(self.events("lease.release")[-1]["payload"]["reason"], "abandoned")
        self.assertEqual(self.attempt("two", 1)["state"], "abandoned")
        self.assertEqual([r for r in self.core.state.live_reservations() if r["task"] == "two"], [])

    def test_pr_state_cmd_is_configurable(self):
        script = os.path.join(self.tmp, "bin", "pr-state")
        with open(script, "w") as f:
            f.write("#!/bin/sh\necho \"$1 $WAVECOORD_PR_URL\" >> %s\necho '{\"state\": \"MERGED\"}'\n"
                    % os.path.join(self.tmp, "pr-state.log"))
        os.chmod(script, 0o755)
        with open(os.path.join(self.repo, ".claude", "wavecoord.json")) as f:
            cfg = json.load(f)
        cfg["pr_state_cmd"] = [script, "{pr}"]
        with open(os.path.join(self.repo, ".claude", "wavecoord.json"), "w") as f:
            json.dump(cfg, f)
        self.assertEqual(self.lead.pr_state("https://example.invalid/pull/7"), ("MERGED", None, None))
        with open(os.path.join(self.tmp, "pr-state.log")) as f:
            self.assertEqual(f.read(), "7 https://example.invalid/pull/7\n")


class DispatchTest(LeadTestCase):
    SPECS = {"alpha": spec_md("alpha", ["alpha/**"], shared=["docs/index.md"], host_needs=["python3"]),
             "beta": spec_md("beta", ["beta/**"], provider="alt", fallback=["anthropic"]),
             "gamma": spec_md("gamma", ["gamma/**"], provider="alt", fallback=["anthropic"]),
             "needy": spec_md("needy", ["needy/**"], needs=["alpha"]),
             "gpu": spec_md("gpu", ["gpu/**"], host_needs=["gpu"])}

    def test_dispatch_end_to_end(self):
        os.environ.update(FAKE_CLAUDE_COMMIT="1", FAKE_CLAUDE_COMMIT_FILE="{task}/work.txt")
        out = self.lead.dispatch("alpha")
        self.assertEqual((out["id"], out["host"], out["branch"], out["provider"]),
                         ("alpha-1", "local", "wave/alpha-1", "anthropic"))
        # the lease is claimed with the spec's reservations, by the worker agent
        lease = self.core.state.task_lease("alpha")
        self.assertEqual((lease["holder"], lease["epoch"]), (out["agent"], out["lease_epoch"]))
        res = sorted((r["glob"], r["mode"]) for r in self.core.state.live_reservations() if r["task"] == "alpha")
        self.assertEqual(res, [("alpha/**", "exclusive"), ("docs/index.md", "shared")])
        self.assertEqual(self.events("dispatch.decide")[-1]["payload"]["host"], "local")
        self.assertEqual(self.events("attempt.start")[-1]["payload"]["holder"], out["agent"])
        # a second dispatch of the same task is refused (not ready: it is active)
        with self.assertRaises(Exception) as cm:
            self.lead.dispatch("alpha")
        self.assertEqual(getattr(cm.exception, "reason", None), "not_ready")
        # needs not done, host_needs nobody has
        self.assertEqual(self.refusal("needy"), "not_ready")
        self.assertEqual(self.refusal("gpu", force=True), "host_needs")

        item = self.collect_until("alpha-1")
        self.assertEqual(item["state"], "reported")
        self.assertIn("pr", item["actions"])
        # the PR was opened here, by the client, with the default pr_cmd (`gh pr create`) and the lease line
        prs = self.gh()["prs"]
        self.assertEqual([(p["head"], p["base"]) for p in prs], [("wave/alpha-1", "main")])
        self.assertIn("Lease: alpha@%d" % out["lease_epoch"], prs[0]["body"])
        self.assertIn("Task: alpha", prs[0]["body"])
        a = self.attempt("alpha", 1)
        self.assertEqual((a["state"], a["pr_url"], a["report"]["status"]),
                         ("reported", "https://example.invalid/pull/1", "done"))
        self.assertEqual(self.core.state.task_lease("alpha")["state"], "parked")
        self.assertEqual(self.events("attempt.end")[-1]["payload"]["pr_url"], "https://example.invalid/pull/1")
        self.assertIn("alpha/work.txt", sh(["git", "ls-tree", "-r", "--name-only", "wave/alpha-1"], self.origin))
        # the host never ran pr_cmd: its result says only that the branch is ready
        r = self.lead.launcher().result("alpha-1")
        self.assertTrue(r["pr_ready"])
        self.assertIsNone(r.get("pr_url"))
        # collecting again changes nothing
        self.assertEqual(self.lead.collect()["collected"], [])
        self.assertEqual(len(self.gh()["prs"]), 1)

    def refusal(self, task, **ov):
        try:
            self.lead.dispatch(task, overrides=ov or None)
        except Exception as e:  # noqa: BLE001
            return getattr(e, "reason", repr(e))
        self.fail("dispatch of %s was not refused" % task)

    def test_wip_cap_and_wave(self):
        os.environ["FAKE_CLAUDE_SLEEP"] = "6"
        plan = self.lead.dispatch_wave(dry_run=True)
        self.assertEqual([d["task"] for d in plan["dispatched"]], ["alpha", "beta", "gamma"])
        self.assertIn("wip_cap", [s["why"] for s in plan["skipped"]])  # gpu: wip_cap 3 reached first
        # each task placed in the wave counts for the next one (0.1.4 printed "wip 1/3" for all)
        self.assertEqual([d["reason"].split(";")[1].strip() for d in plan["dispatched"]],
                         ["wip 1/3", "wip 2/3", "wip 3/3"])
        with self.assertRaises(Exception) as e:
            self.lead.decide(self.lead.plan().get("alpha"), flying=3)
        self.assertEqual(getattr(e.exception, "reason", None), "wip_cap")
        self.assertEqual(self.events("dispatch.decide"), [])  # a dry run logs nothing
        out = self.lead.dispatch_wave(["alpha", "beta"])
        self.assertEqual(sorted(d["task"] for d in out["dispatched"]), ["alpha", "beta"])
        self.assertEqual(out["wip"]["in_flight"], 2)
        self.assertIn({"task": "needy", "why": "needs not done: alpha"},
                      [{"task": s["task"], "why": s["why"]} for s in self.lead.dispatch_wave(["needy"])["skipped"]])

    def test_provider_fallback_after_rate_limit(self):
        ev = os.path.join(self.tmp, "events.jsonl")
        with open(ev, "w") as f:
            f.write(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected",
                                                                                 "rateLimitType": "five_hour"}}) + "\n")
        os.environ.update(FAKE_CLAUDE_EVENTS=ev, FAKE_CLAUDE_EXIT="1")
        out = self.lead.dispatch("beta")
        self.assertEqual(out["provider"], "alt")
        r = self.wait_host("beta-1")
        self.assertEqual((r["state"], r["throttled"], r["next_provider"]), ("failed", True, "anthropic"))
        # the next run works; the collector starts it on the next provider
        for k in ("FAKE_CLAUDE_EVENTS", "FAKE_CLAUDE_EXIT"):
            os.environ.pop(k)
        os.environ.update(FAKE_CLAUDE_COMMIT="1", FAKE_CLAUDE_COMMIT_FILE="{task}/work.txt")
        item = self.lead.collect()["collected"][0]
        self.assertIn("fallback", item["actions"])
        self.assertEqual((item["fallback"]["id"], item["fallback"]["provider"]), ("beta-2", "anthropic"))
        self.assertEqual(self.attempt("beta", 1)["state"], "failed")
        self.assertTrue(self.attempt("beta", 1)["throttled"])
        self.assertEqual(self.attempt("beta", 2)["provider"], "anthropic")
        # a new task that asks for the backed-off provider goes down its fallback list
        d = self.lead.dispatch("gamma", dry_run=True)
        self.assertEqual(d["provider"], "anthropic")
        self.assertIn("alt: backed off", d["reason"])
        self.assertEqual(self.collect_until("beta-2")["state"], "reported")


class ConflictResolveTest(LeadTestCase):
    SPECS = {"delta": spec_md("delta", ["shared.txt"], prompt="Write shared.txt for delta.")}

    def test_seeded_conflict_dispatches_a_resolver(self):
        os.environ.update(FAKE_CLAUDE_COMMIT="1", FAKE_CLAUDE_COMMIT_FILE="shared.txt",
                          FAKE_CLAUDE_COMMIT_TEXT="worker side\n", FAKE_CLAUDE_SLEEP="1.5")
        first = self.lead.dispatch("delta")
        self.push_to_main("shared.txt", "main side\n")  # main changes the same file under the worker
        r = self.wait_host("delta-1")
        self.assertEqual((r["state"], r["pushed"], r["conflicts"], r["pr_ready"]),
                         ("reported", True, ["shared.txt"], False))
        # the resolver merges origin/main and keeps its side
        for k in ("FAKE_CLAUDE_COMMIT", "FAKE_CLAUDE_SLEEP"):
            os.environ.pop(k)
        os.environ["FAKE_CLAUDE_MERGE"] = "origin/main"
        col = self.lead.collect()
        item = col["collected"][0]
        self.assertEqual(item["actions"][:2], ["report", "conflict"])
        self.assertIn("resolve", item["actions"])
        self.assertFalse(os.path.exists(self.gh_state))  # no PR for a conflicting branch
        c = self.events("conflict.open")[0]["payload"]["conflict"]
        self.assertEqual((c["kind"], c["files"], c["task"], c["attempt"]), ("merge_tree", ["shared.txt"], "delta", 1))
        rs = self.events("resolve.start")[0]
        self.assertEqual((rs["task"], rs["attempt"], rs["payload"]["files"]), ("delta", 1, ["shared.txt"]))
        # the resolver continues the same branch, with a brief of the files and both intents
        res = item["resolver"]
        self.assertEqual((res["id"], res["branch"]), ("delta-1", "wave/delta-1"))
        brief = self.host_file("delta-1", "prompt.md")
        for want in ("Resolve merge conflicts: task delta, attempt 1", "- `shared.txt`", "Write shared.txt for delta.",
                     "main: shared.txt", "git merge origin/main"):
            self.assertIn(want, brief)
        # the resolver stole the parked lease: a new epoch for the same attempt
        lease = self.core.state.task_lease("delta")
        self.assertEqual((lease["attempt"], lease["holder"]), (1, res["agent"]))
        self.assertGreater(lease["epoch"], first["lease_epoch"])
        # the resolver run ends clean: resolve.end ok, conflict closed, the PR opened by the client
        item = self.collect_until("delta-1")
        self.assertIn("conflict.close", item["actions"])
        self.assertIn("pr", item["actions"])
        self.assertTrue(self.events("resolve.end")[0]["payload"]["ok"])
        self.assertEqual(self.core.state.get("conflicts", c["id"])["state"], "closed")
        self.assertEqual([p["head"] for p in self.gh()["prs"]], ["wave/delta-1"])
        self.assertEqual(self.attempt("delta", 1)["pr_url"], "https://example.invalid/pull/1")
        # the report of the resolver run was kept (its own idempotency key)
        self.assertEqual(len(self.events("report.post")), 2)


class RefineryTest(LeadTestCase):
    SPECS = {"head": spec_md("head", ["x.txt"]),
             "tail-1": spec_md("tail-1", ["t1/**"], needs=["head"]),
             "tail-2": spec_md("tail-2", ["t2/**"], needs=["head"]),
             "solo": spec_md("solo", ["solo/**"]),
             "late": spec_md("late", ["late/**"])}

    def run_task(self, task, fname, **ov):
        os.environ.update(FAKE_CLAUDE_COMMIT="1", FAKE_CLAUDE_COMMIT_FILE=fname, FAKE_CLAUDE_COMMIT_TEXT="{task}\n")
        d = self.lead.dispatch(task, overrides=ov or None)
        self.collect_until(d["id"])
        return d

    def test_merges_in_funnel_order_and_rechecks(self):
        self.run_task("solo", "solo/f.txt")
        self.run_task("head", "x.txt")
        self.run_task("late", "x.txt")  # also writes x.txt: clean now, a conflict after head merges
        status = self.lead.refinery_status()
        self.assertEqual([(i["task"], i["unblocks"]) for i in status["queue"]], [("head", 2), ("solo", 0), ("late", 0)])
        out = self.lead.refinery_merge()
        self.assertEqual([(r["task"], r["state"]) for r in out["results"]],
                         [("head", "merged"), ("solo", "merged"), ("late", "blocked")])
        # late was re-checked against main after head merged: a conflict, so a resolver, no merge
        late = out["results"][2]
        self.assertEqual((late["reason"], late["files"]), ("conflict", ["x.txt"]))
        self.assertEqual(late["resolver"]["id"], "late-1")
        merges = [c for c in self.gh()["calls"] if c[:2] == ["pr", "merge"]]
        self.assertEqual([c[2] for c in merges], ["2", "1"])  # head (PR 2), then solo (PR 1)
        self.assertEqual(merges[0][3:5], ["--merge", "--match-head-commit"])
        tree = sh(["git", "ls-tree", "-r", "--name-only", "main"], self.origin).split()
        self.assertTrue({"x.txt", "solo/f.txt"} <= set(tree))
        self.assertEqual(sh(["git", "show", "main:x.txt"], self.origin), "head")
        kinds = [(e["kind"], e["task"]) for e in self.events() if e["kind"].startswith("refinery.")]
        self.assertIn(("refinery.merge", "head"), kinds)
        self.assertIn(("refinery.block", "late"), kinds)
        self.assertEqual(self.attempt("head", 1)["state"], "merged")
        self.assertEqual(self.core.state.lease(self.attempt("head", 1)["lease_id"])["reason"], "merged")
        # a merged task is done for the plan: its dependents are ready now
        ready = [t["id"] for t in self.lead.plan_ready()["tasks"]]
        self.assertIn("tail-1", ready)
        # the resolver run is in flight, so late is out of the queue until it reports again
        self.assertNotIn("late", [i["task"] for i in self.lead.refinery_status()["queue"]])

    def test_fence_blocks_a_merge_outside_the_lease(self):
        """P6 E4/E5 in the refinery: with fence merge enforce, a PR outside its lease is not merged."""
        cfgp = os.path.join(self.repo, ".claude", "wavecoord.json")
        with open(cfgp) as f:
            cfg = json.load(f)
        cfg["fence"] = {"merge": "enforce"}
        with open(cfgp, "w") as f:
            json.dump(cfg, f)
        os.utime(cfgp, (time.time() + 5, time.time() + 5))
        self.run_task("solo", "elsewhere.txt")  # the lease is solo/**
        self.assertEqual(self.attempt("solo", 1)["fence"]["verdict"], "fail")
        out = self.lead.refinery_merge()
        r = out["results"][0]
        self.assertEqual((r["state"], r["reason"]), ("blocked", "fence"))
        self.assertIn("E4: the report's fence verdict is fail", r["detail"])
        self.assertIn("E5: gate-check exit 1", r["detail"])
        self.assertEqual([c for c in self.gh()["calls"] if c[:2] == ["pr", "merge"]], [])
        self.run_task("late", "late/f.txt")  # inside the lease: merged
        out = self.lead.refinery_merge("late/1")
        self.assertEqual(out["results"][0]["state"], "merged")

    def test_refinery_refuses_a_task_pr_that_changes_the_config(self):
        """A task PR may not change the lead's config (edit-fence.md, section 8): refused in the default advisory
        merge mode with reason config_change; in mode off it is only reported."""
        self.run_task("solo", ".claude/wavecoord.json")  # the fake worker overwrites the config
        r = self.lead.refinery_merge()["results"][0]
        self.assertEqual((r["state"], r["reason"]), ("blocked", "config_change"))
        self.assertIn("plan or config PR", r["detail"])
        self.assertEqual([c for c in self.gh()["calls"] if c[:2] == ["pr", "merge"]], [])
        cfgp = os.path.join(self.repo, ".claude", "wavecoord.json")
        with open(cfgp) as f:
            cfg = json.load(f)
        cfg["fence"] = "off"
        with open(cfgp, "w") as f:
            json.dump(cfg, f)
        os.utime(cfgp, (time.time() + 5, time.time() + 5))
        r = self.lead.refinery_merge()["results"][0]
        self.assertEqual(r["state"], "merged")
        self.assertIn("config_change (reported, fence merge mode off)", r["fence"])

    def test_refinery_refuses_a_stale_lease_line(self):
        """In every fence mode (here the default, advisory), a PR whose Lease: epoch is not the
        attempt's lease is not merged."""
        self.run_task("solo", "solo/f.txt")
        st = self.gh()
        epoch = self.core.state.task_lease("solo")["epoch"]
        st["prs"][0]["body"] = st["prs"][0]["body"].replace("Lease: solo@%d" % epoch, "Lease: solo@%d" % (epoch - 1))
        with open(self.gh_state, "w") as f:
            json.dump(st, f)
        out = self.lead.refinery_merge()
        self.assertEqual((out["results"][0]["state"], out["results"][0]["reason"]), ("blocked", "stale_lease"))
        self.assertEqual([c for c in self.gh()["calls"] if c[:2] == ["pr", "merge"]], [])
        st = self.gh()
        st["prs"][0]["body"] = st["prs"][0]["body"].replace("Lease: solo@%d" % (epoch - 1), "Lease: solo@%d" % epoch)
        with open(self.gh_state, "w") as f:
            json.dump(st, f)
        self.assertEqual(self.lead.refinery_merge()["results"][0]["state"], "merged")

    def test_resolver_updates_the_pr_lease_line(self):
        """A resolver takes over the attempt with a new epoch: collect sets the PR's Lease: line (P6, E5)."""
        self.run_task("head", "x.txt")
        self.run_task("late", "x.txt")
        old = self.core.state.task_lease("late")["epoch"]
        pr = [p for p in self.gh()["prs"] if p["head"] == "wave/late-1"][0]
        self.assertIn("Lease: late@%d" % old, pr["body"])
        for k in ("FAKE_CLAUDE_COMMIT", "FAKE_CLAUDE_COMMIT_FILE", "FAKE_CLAUDE_COMMIT_TEXT"):
            os.environ.pop(k, None)
        os.environ["FAKE_CLAUDE_MERGE"] = "origin/main"
        out = self.lead.refinery_merge()
        self.assertEqual([r["state"] for r in out["results"]], ["merged", "blocked"])
        item = self.collect_until("late-1")
        new = self.core.state.task_lease("late")["epoch"]
        self.assertGreater(new, old)
        self.assertIn("pr.lease", item["actions"])
        pr = [p for p in self.gh()["prs"] if p["head"] == "wave/late-1"][0]
        self.assertIn("Lease: late@%d" % new, pr["body"])
        self.assertNotIn("Lease: late@%d" % old, pr["body"])

    def test_protected_paths_and_check_cmd(self):
        prot = os.path.join(self.tmp, "protected")
        with open(prot, "w") as f:
            f.write("#!/bin/sh\necho 'solo/**'\n")
        os.chmod(prot, 0o755)
        cfgp = os.path.join(self.repo, ".claude", "wavecoord.json")
        with open(cfgp) as f:
            cfg = json.load(f)
        cfg.update(protected_paths_cmd=[prot], check_cmd="test -f solo/f.txt || test -f x.txt")
        with open(cfgp, "w") as f:
            json.dump(cfg, f)
        os.utime(cfgp, (time.time() + 5, time.time() + 5))
        self.run_task("solo", "solo/f.txt")
        out = self.lead.refinery_merge("1")
        self.assertEqual((out["results"][0]["state"], out["results"][0]["reason"]), ("blocked", "protected"))
        self.assertEqual(out["results"][0]["files"], ["solo/f.txt"])
        # transitions only: the same block again logs nothing
        n = len(self.events("refinery.block"))
        self.assertEqual(self.lead.refinery_merge("1")["results"][0]["reason"], "protected")
        self.assertEqual(len(self.events("refinery.block")), n)
        out = self.lead.refinery_merge("1", approved_protected=True)
        self.assertEqual(out["results"][0]["state"], "merged")


class McpToolsTest(LeadTestCase):
    SPECS = {"alpha": spec_md("alpha", ["alpha/**"]), "beta": spec_md("beta", ["beta/**"], needs=["alpha"])}

    def test_every_tool(self):
        err, out = self.mcp("plan_ready")
        self.assertFalse(err, out)
        self.assertEqual([t["id"] for t in out["tasks"]], ["alpha"])
        self.assertEqual(out["why_not"]["beta"], "needs not done: alpha")
        err, out = self.mcp("plan_get", {"id": "alpha"})
        self.assertEqual((err, out["spec"]["id"], out["state"]), (False, "alpha", "todo"))
        err, out = self.mcp("plan_graph")
        self.assertEqual((err, out["funnel_heads"][0]["id"]), (False, "alpha"))
        err, out = self.mcp("plan_validate")
        self.assertEqual((err, out["ok"]), (False, True))
        err, out = self.mcp("plan_edit", {"edits": [{"op": "replace", "id": "beta", "anchor": "size: S",
                                                     "text": "size: M"}], "title": "plan: beta is M"})
        self.assertFalse(err, out)
        self.assertEqual((out["branch"], out["pr"]), ("plan/1", "https://example.invalid/pull/1"))
        self.assertEqual(self.gh()["prs"][0]["head"], "plan/1")
        err, out = self.mcp("plan_edit", {"edits": [{"op": "replace", "id": "beta", "anchor": "nope", "text": "x"}]})
        self.assertTrue(err)
        self.assertIn("anchor not found", out["error"]["message"])
        err, out = self.mcp("propose", {"task": "alpha", "text": "split alpha"})
        self.assertEqual((err, out["summary"], out["state"]), (False, "split alpha", "open"))
        self.assertEqual(self.mcp("plan_get", {"id": "alpha"})[1]["proposals"][0]["summary"], "split alpha")
        err, out = self.mcp("proposals", {"task": "alpha", "state": "open"})
        self.assertEqual((err, [p["summary"] for p in out]), (False, ["split alpha"]))
        err, out = self.mcp("proposal_decide", {"id": out[0]["id"], "decision": "done", "note": "plan/1"})
        self.assertEqual((err, out["state"], out["note"]), (False, "done", "plan/1"))
        self.assertEqual(self.mcp("plan_get", {"id": "alpha"})[1]["proposals"], [])
        self.assertEqual(self.events("proposal.decide")[-1]["payload"]["decision"], "done")
        # one plan PR for many specs: an edit of two files and a new spec file
        new = spec_md("gamma", ["gamma/**"], needs=["alpha"])
        err, out = self.mcp("plan_edit", {"edits": [
            {"op": "replace", "id": "alpha", "anchor": "size: S", "text": "size: M"},
            {"op": "insert_after", "id": "beta", "anchor": "Context of beta.", "text": " More context."},
            {"op": "create", "id": "gamma", "text": new}], "title": "plan: wave 2"})
        self.assertFalse(err, out)
        self.assertEqual((out["branch"], out["files"]),
                         ("plan/2", ["plan/tasks/alpha.md", "plan/tasks/beta.md", "plan/tasks/gamma.md"]))
        self.assertEqual(len(self.gh()["prs"]), 2)
        err, out = self.mcp("dispatch_wave", {"dry_run": True})
        self.assertEqual((err, [d["task"] for d in out["dispatched"]]), (False, ["alpha"]))
        os.environ.update(FAKE_CLAUDE_COMMIT="1", FAKE_CLAUDE_COMMIT_FILE="{task}/w.txt")
        err, out = self.mcp("dispatch", {"task": "alpha", "overrides": {"max_turns": 3}})
        self.assertFalse(err, out)
        self.assertEqual(out["id"], "alpha-1")
        self.wait_host("alpha-1")
        err, out = self.mcp("collect")
        self.assertEqual((err, out["collected"][0]["state"]), (False, "reported"))
        err, out = self.mcp("attempts", {"task": "alpha"})
        self.assertEqual((err, out[0]["state"]), (False, "reported"))
        err, out = self.mcp("attempt_get", {"task": "alpha", "n": 1})
        self.assertEqual((err, out["pr_url"]), (False, "https://example.invalid/pull/3"))
        err, out = self.mcp("conflicts")
        self.assertFalse(err, out)
        self.assertEqual((out["merge_tree"], out["base"]), ([], "main"))
        err, out = self.mcp("conflicts", {"globs": ["alpha/x"]})
        self.assertEqual(err, False)
        err, out = self.mcp("resolve", {"attempt": "alpha/1"})
        self.assertEqual((err, out["note"]), (False, "the branch merges cleanly"))
        err, out = self.mcp("refinery_status")
        self.assertEqual((err, [i["task"] for i in out["queue"]]), (False, ["alpha"]))
        err, out = self.mcp("refinery_merge", {"pr": "999"})
        self.assertTrue(err)
        err, out = self.mcp("refinery_merge", {"pr": "3"})
        self.assertEqual((err, out["merged"][0]["task"]), (False, "alpha"))
        err, out = self.mcp("send", {"to": {"kind": "lead"}, "body": "hello"})
        self.assertFalse(err)
        err, out = self.mcp("inbox")
        self.assertFalse(err)
        err, out = self.mcp("ack", {"id": 1})
        self.assertFalse(err)
        err, out = self.mcp("config")
        self.assertEqual((err, out["auto_resolve"]), (False, True))
        # writes need the lead role
        err, out = self.mcp("dispatch", {"task": "beta"}, token=AGENT_TOKEN)
        self.assertTrue(err)
        self.assertEqual(out["error"]["code"], "forbidden")
        err, out = self.mcp("plan_ready", token=AGENT_TOKEN)
        self.assertFalse(err)
        # every tool in the table has been called
        listed = self.rpc_list()
        self.assertEqual(sorted(listed), sorted(["plan_ready", "plan_get", "plan_graph", "plan_validate", "plan_edit",
                                                 "dispatch", "dispatch_wave", "collect", "attempts", "attempt_get",
                                                 "inbox", "ack", "send", "conflicts", "resolve", "refinery_status",
                                                 "refinery_merge", "propose", "proposals", "proposal_decide",
                                                 "config"]))

    def rpc_list(self):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        req = urllib.request.Request(self.url + "/mcp", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": "Bearer " + LEAD_TOKEN})
        with urllib.request.urlopen(req, timeout=30) as r:
            tools = json.loads(r.read().decode())["result"]["tools"]
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object", t["name"])
        return [t["name"] for t in tools]


class DispatchLoopCliTest(LeadTestCase):
    SPECS = {"alpha": spec_md("alpha", ["alpha/**"]), "beta": spec_md("beta", ["beta/**"])}

    def wc(self, *args):
        env = dict(os.environ, WAVECOORD_URL=self.url, CLAUDE_PLUGIN_OPTION_TOKEN=LEAD_TOKEN,
                   PYTHONPATH=_paths.SRC)
        return subprocess.run([sys.executable, "-m", "wavecoord"] + list(args), cwd=self.repo, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=300)

    def test_loop(self):
        p = self.wc("dispatch", "--loop", "--dry-run", "--ticks", "1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("would dispatch alpha", p.stdout)
        self.assertEqual(self.events("dispatch.decide"), [])
        os.environ.update(FAKE_CLAUDE_COMMIT="1", FAKE_CLAUDE_COMMIT_FILE="{task}/w.txt")
        p = self.wc("dispatch", "--loop", "--ticks", "1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("dispatched alpha-1", p.stdout)
        self.assertIn("dispatched beta-1", p.stdout)
        self.wait_host("alpha-1")
        self.wait_host("beta-1")
        p = self.wc("collect")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("alpha-1: reported [report,pr] https://example.invalid/pull/", p.stdout)
        p = self.wc("refinery", "status")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("alpha/1", p.stdout)


if __name__ == "__main__":
    unittest.main()
