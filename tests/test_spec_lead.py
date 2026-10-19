"""The spec tree in the lead (phase S3): the plan store, the spec_* tools, spec_edit's plan PRs, dispatch from the
tree with the requirement in the worker prompt, and whole waves of leases in fence enforce mode (P8)."""

import json
import os
import random
import shutil
import subprocess
import unittest

import _paths
import specsim
from launcher_env import sh
from test_lead import LeadTestCase

from wavecoord.core import ApiError
from wavecoord.specs.graph import Graph
from wavecoord.specs.plan import SpecTreePlan, coord_id
from wavecoord.specs.tree import Tree

IMPL = "auth.sessions.token-rotation.rotate-impl"


class SpecLeadCase(LeadTestCase):
    FILES = None  # {path under specs/: text}; default the fixture tree

    def setUp(self):
        super().setUp()
        files = self.FILES
        if files is None:
            from wavecoord.specs import source
            files = source.read_tree(_paths.FIXTURES, "specs")
        sh(["git", "pull", "-q", "origin", "main"], self.seed)
        for rel, text in files.items():
            p = os.path.join(self.seed, "specs", rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(text)
        sh(["git", "add", "specs"], self.seed)
        sh(["git", "commit", "-q", "-m", "specs"], self.seed)
        sh(["git", "push", "-q", "origin", "main"], self.seed)
        cfg_path = os.path.join(self.repo, ".claude", "wavecoord.json")
        with open(cfg_path) as f:
            cfg = json.load(f)
        cfg.pop("plan", None)  # no plan key: the spec tree on the default branch is the plan
        with open(cfg_path, "w") as f:
            json.dump(cfg, f)
        os.utime(cfg_path, (1, 1))

    def tool(self, name, args=None):
        err, out = self.mcp(name, args or {})
        self.assertFalse(err, out)
        return out

    def origin_git(self, *args):
        return subprocess.run(["git"] + list(args), cwd=self.origin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, check=True).stdout


class SpecToolsTest(SpecLeadCase):
    def test_plan_store_and_reads(self):
        self.assertIsInstance(self.lead.plan(), SpecTreePlan)
        ready = self.tool("plan_ready")
        self.assertEqual([t["id"] for t in ready["tasks"]], [IMPL])
        self.assertIn("auth.sessions.token-rotation.rotate-tests", ready["why_not"])
        got = self.tool("plan_get", {"id": IMPL})
        self.assertEqual(got["spec"]["tree"]["requirement"], "auth/sessions/token-rotation")
        self.assertEqual(self.tool("plan_validate")["ok"], True)
        tree = self.tool("spec_tree")["tree"]
        self.assertEqual(tree[0]["progress"]["total"], 3)
        self.assertEqual(self.tool("spec_get", {"id": "auth/sessions/token-rotation"})["kind"], "requirement")
        q = self.tool("spec_query", {"q": "kind=task status=planned"})["records"]
        self.assertEqual(len(q), 2)
        w = self.tool("spec_waves")
        self.assertEqual(w["waves"][0], ["auth/sessions/token-rotation/rotate-impl"])
        self.assertEqual(self.tool("spec_trace", {"id": "auth/sessions/token-rotation"})["needs"],
                         ["auth/sessions/session-store"])
        self.assertTrue(self.tool("spec_check")["ok"])
        err, out = self.mcp("spec_get", {"id": "nope"})
        self.assertTrue(err)

    def test_spec_edit_builds_one_pr(self):
        out = self.tool("spec_edit", {"ops": [
            {"op": "create", "id": "auth/sessions/revoke", "fields": {"title": "Revoke"},
             "sections": {"Statement": "If a token is revoked, then the service shall reject it."}}],
            "title": "spec: revoke"})
        self.assertEqual(out["branch"], "plan/1")
        self.assertEqual(out["files"], ["specs/auth/sessions/revoke/_node.md"])
        self.assertEqual(out["pr"], "https://example.invalid/pull/1")
        out2 = self.tool("spec_edit", {"branch": "plan/1", "ops": [
            {"op": "create", "id": "auth/sessions/revoke/check", "fields": {
                "title": "Check", "paths": ["src/auth/revoke.py"], "class": "B", "size": "S",
                "exec": {"model": "sonnet", "effort": "low", "max_turns": 5, "budget_usd": 1, "timeout_min": 5},
                "outputs": [{"id": "src/auth/revoke.py", "kind": "file"}]},
             "sections": {"Context": "None yet.", "Prompt": "Add it."}}]})
        self.assertEqual((out2["branch"], out2["new_branch"], out2["pr"]), ("plan/1", False, None))
        tree = self.origin_git("ls-tree", "-r", "--name-only", "plan/1", "specs/auth/sessions/revoke")
        self.assertEqual(tree.split(), ["specs/auth/sessions/revoke/_node.md",
                                        "specs/auth/sessions/revoke/tasks/check.md"])
        with open(self.gh_state) as f:
            self.assertEqual(len(json.load(f)["prs"]), 1)
        self.assertNotIn("revoke", self.origin_git("ls-tree", "-r", "--name-only", "main"))

    def test_spec_edit_refuses_an_invalid_tree(self):
        err, out = self.mcp("spec_edit", {"ops": [{"op": "set", "id": "auth/sessions/token-rotation/rotate-impl",
                                                   "key": "title", "value": "x"},
                                                  {"op": "add", "id": "auth/sessions/token-rotation/rotate-impl",
                                                   "key": "needs", "value": "auth/nowhere"}]})
        self.assertTrue(err)
        self.assertIn("does not exist", json.dumps(out))
        self.assertEqual(self.origin_git("for-each-ref", "refs/heads/plan/"), "")

    def test_mv_commits_as_a_rename(self):
        out = self.tool("spec_edit", {"ops": [{"op": "mv", "from": "auth/sessions/token-rotation",
                                               "to": "auth/sessions/rotation"}]})
        self.assertTrue(out["moves"])
        status = self.origin_git("diff", "-M", "--name-status", "main", out["branch"])
        renames = [l for l in status.splitlines() if l.startswith("R")]
        self.assertIn("specs/auth/sessions/rotation/_node.md", "\n".join(renames))
        self.assertEqual(len(renames), 3, status)


class SpecDispatchTest(SpecLeadCase):
    def test_dispatch_from_the_tree(self):
        out = self.tool("dispatch_wave")
        self.assertEqual([d["task"] for d in out["dispatched"]], [IMPL])
        d = out["dispatched"][0]
        self.assertEqual(d["branch"], "wave/%s-1" % IMPL)
        r = self.wait_host(d["id"])
        self.assertEqual(r["state"], "reported", r)
        with open(os.path.join(os.environ["WAVECOORD_HOME"], "attempts", "demo", d["id"], "prompt.md")) as f:
            prompt = f.read()
        for want in ("## Requirement", "the auth service shall issue a new token",
                     "`auth.session-store.api` (interface) from `auth/sessions/session-store/store-api`",
                     "## Also run (the requirement's checks)"):
            self.assertIn(want, prompt)
        grants = [e for e in self.core.events_after(0) if e["kind"] == "lease.grant" and e["task"] == IMPL]
        globs = sorted((r["glob"], r["mode"]) for r in grants[0]["payload"]["reservations"])
        self.assertEqual(globs, [("docs/api.md", "shared"), ("src/auth/rotate.py", "exclusive")])


class SpecWaveLeaseTest(SpecLeadCase):
    """P8 with the coordinator: in fence enforce mode, every lease of a computed wave is granted."""

    FILES = specsim.build(random.Random(3), caps=2, feats=2, reqs=2, max_tasks=3)
    EXTRA_CFG = {"fence": {"edit": "enforce", "commit": "enforce", "push": "enforce", "merge": "enforce"}}

    def test_wave_leases_are_granted(self):
        g = Graph(Tree(self.FILES))
        wave = g.waves()["waves"][0]
        self.assertGreaterEqual(len(wave), 4)
        plan = self.lead.plan()
        for i, t in enumerate(wave):
            spec = plan.get(coord_id(t))
            body = {"task": spec["id"], "attempt": 1, "holder": "agent-%d" % i, "host": "local",
                    "reservations": [{"glob": x, "mode": "exclusive"} for x in spec["paths"]]}
            self.core.claim(self.lead.ctx, body)  # raises ApiError(conflict) on an overlapping grant
        # and the predicate's opposite: a task that clashes is refused in enforce mode
        clash = {"task": "intruder", "attempt": 1, "holder": "agent-x", "host": "local",
                 "reservations": [{"glob": plan.get(coord_id(wave[0]))["paths"][0], "mode": "exclusive"}]}
        with self.assertRaises(ApiError):
            self.core.claim(self.lead.ctx, clash)


if __name__ == "__main__":
    unittest.main()
