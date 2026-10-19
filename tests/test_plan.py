"""The plan store: parse, validate, ready, graph; anchored edits; plan_edit on a plan branch; the command adapter."""

import glob
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import _paths

from wavecoord import schemaload
from wavecoord.plan import graph as g
from wavecoord.plan.adapters import BuiltinPlan, CommandPlan, DirSource, GitSource, PlanError
from wavecoord.plan.edit import EditError, apply_edits, plan_edit
from wavecoord.plan.spec import SpecError, load_text, parse_frontmatter, parse_text

PLAN = os.path.join(_paths.FIXTURES, "plan", "tasks")
ADAPTER = os.path.join(_paths.FIXTURES, "plan-adapter")
FAKE_PLAN = os.path.join(_paths.TESTS, "fake-plan", "plan")


def read(name):
    with open(os.path.join(PLAN, name), encoding="utf-8") as f:
        return f.read()


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def sh(args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True).stdout.strip()


class ParseTest(unittest.TestCase):
    def test_markdown_equals_the_json_fixture(self):
        spec = load_text(read("api-auth.md"), expect_id="api-auth")
        self.assertEqual(spec, load_json(os.path.join(_paths.FIXTURES, "task-spec", "api-auth.json")))

    def test_every_fixture_spec_is_valid(self):
        for p in sorted(glob.glob(os.path.join(PLAN, "*.md"))):
            with self.subTest(p=os.path.basename(p)):
                spec = load_text(read(os.path.basename(p)), expect_id=os.path.basename(p)[:-3])
                self.assertEqual(schemaload.errors(spec, "task-spec"), [])

    def test_quoted_values_notes_and_empty_risks(self):
        spec = load_text(read("api-docs.md"))
        self.assertEqual(spec["title"], "API docs: rotation")
        self.assertEqual(spec["sections"]["notes"], "Not sent to the worker.")
        self.assertEqual(load_text(read("session-store.md"))["sections"]["risks"], [])
        self.assertEqual(load_text(read("api-auth.md"))["exec"]["budget_usd"], 5)

    def test_rejects_outside_the_subset(self):
        base = read("cli-flags.md")
        cases = {
            "empty key": base.replace("needs: []", "needs:"),
            "list in a list item": base.replace("needs: []", "needs:\n  - id: a\n    x: [b]"),
            "mixed list": base.replace("needs: []", "needs:\n  - a\n  - id: b"),
            "nested map in a list item": base.replace("needs: []", "needs:\n  - id: a\n    x:\n      y: 1"),
            "tab": base.replace("size: S", "size:\tS"),
            "unknown section": base + "\n## Extra\ntext\n",
            "no frontmatter": base.split("---\n", 2)[2],
            "unterminated": base.replace("title: CLI flags", "title: 'CLI flags"),
            "nested list": base.replace("paths: [src/cli/**]", "paths: [[a]]"),
            "text outside": base.replace("## Context", "stray\n## Context"),
            "alias": base.replace("paths: [src/cli/**]", "paths: [*.py]"),
            "duplicate key": base.replace("size: S", "size: S\nsize: M"),
            "deep nesting": base.replace("  model: sonnet", "  model:\n    x: 1"),
        }
        for what, text in cases.items():
            with self.subTest(what=what):
                with self.assertRaises(SpecError):
                    load_text(text)

    def test_block_lists_parse_like_flow_lists(self):
        base = read("api-auth.md")
        block = base.replace("needs: [session-store]", "needs:\n  - session-store").replace(
            "paths: [src/auth/**, tests/test_rotate.py]", "paths:\n  - src/auth/**\n  - tests/test_rotate.py")
        self.assertNotEqual(block, base)
        self.assertEqual(load_text(block), load_text(base))
        fm = parse_frontmatter(["outputs:", "  - id: a.b", "    kind: interface", "    contract: 'f(x) -> y'",
                                "  - id: src/x.py", "    kind: file", "tags:", "  - z"])
        self.assertEqual(fm, {"outputs": [{"id": "a.b", "kind": "interface", "contract": "f(x) -> y"},
                                          {"id": "src/x.py", "kind": "file"}], "tags": ["z"]})

    def test_schema_errors_and_id_check(self):
        with self.assertRaises(SpecError) as cm:
            load_text(read("cli-flags.md").replace("class: B", "class: Z"))
        self.assertIn("class", str(cm.exception))
        with self.assertRaises(SpecError) as cm:
            load_text(read("cli-flags.md"), expect_id="other")
        self.assertIn("file name", str(cm.exception))
        with self.assertRaises(SpecError):  # acceptance needs one item
            load_text(read("cli-flags.md").replace("- `python -m unittest tests.test_cli`", ""))

    def test_parse_errors_carry_line_numbers(self):
        with self.assertRaises(SpecError) as cm:
            parse_text(read("cli-flags.md").replace("size: S", "size S"))
        self.assertIn("line 8", str(cm.exception))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.plan = BuiltinPlan(DirSource(PLAN))

    def test_get_and_missing(self):
        self.assertEqual(self.plan.get("cli-flags")["size"], "S")
        with self.assertRaises(PlanError) as cm:
            self.plan.get("nope")
        self.assertIn("no task nope", str(cm.exception))

    def test_ready_needs_and_overlap(self):
        r = self.plan.ready({}, [])
        self.assertEqual([t["id"] for t in r["tasks"]], ["api-auth", "cli-flags"])
        self.assertIn("needs not done: api-auth", r["why_not"]["api-docs"])
        # a live exclusive reservation of another task blocks an overlapping task
        live = [{"glob": "src/auth/tokens.py", "mode": "exclusive", "task": "other", "lease_id": 3}]
        r = self.plan.ready({}, live)
        self.assertEqual([t["id"] for t in r["tasks"]], ["cli-flags"])
        self.assertIn("overlaps src/auth/tokens.py", r["why_not"]["api-auth"])
        # shared vs shared does not block; shared vs exclusive does
        live = [{"glob": "docs/api.md", "mode": "shared", "task": "other", "lease_id": 3}]
        self.assertIn("api-auth", [t["id"] for t in self.plan.ready({}, live)["tasks"]])
        # coordinator state: an active task is not ready; a merged need is done
        r = self.plan.ready({"api-auth": "active"}, [])
        self.assertEqual([t["id"] for t in r["tasks"]], ["cli-flags"])
        r = self.plan.ready({"api-auth": "done"}, [])
        self.assertEqual([t["id"] for t in r["tasks"]], ["api-docs", "cli-flags"])

    def test_graph_critical_path_and_funnel_heads(self):
        out = self.plan.graph({})
        self.assertEqual(sorted((e["from"], e["to"]) for e in out["edges"]),
                         [("api-auth", "session-store"), ("api-docs", "api-auth")])
        self.assertEqual(out["critical_path"], ["api-auth", "api-docs"])  # M(3) + XS(1) > S(2)
        self.assertEqual(out["funnel_heads"], [{"id": "api-auth", "unblocks": 1, "state": "todo"}])
        nodes = {n["id"]: n for n in out["nodes"]}
        self.assertEqual(nodes["session-store"]["unblocks"], 2)
        self.assertEqual(nodes["api-auth"]["state"], "todo")
        nodes = {n["id"]: n for n in self.plan.graph({"api-auth": "review", "session-store": "active"})["nodes"]}
        self.assertEqual(nodes["api-auth"]["state"], "review")
        self.assertEqual(nodes["session-store"]["state"], "done")  # a closed spec state wins

    def test_validate_reports_cycles_and_unknown_needs(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        for n in ("api-auth.md", "session-store.md", "cli-flags.md"):
            shutil.copy(os.path.join(PLAN, n), d)
        self.assertTrue(BuiltinPlan(DirSource(d)).validate()["ok"])
        with open(os.path.join(d, "session-store.md"), "w") as f:
            f.write(read("session-store.md").replace("needs: []", "needs: [api-auth, ghost]"))
        with open(os.path.join(d, "bad.md"), "w") as f:
            f.write("not a spec\n")
        out = BuiltinPlan(DirSource(d)).validate()
        self.assertFalse(out["ok"])
        text = "\n".join(out["errors"])
        self.assertIn("needs unknown task ghost", text)
        self.assertIn("needs cycle", text)
        self.assertIn("bad.md", text)

    def test_graph_helpers(self):
        nodes = [{"id": i, "state": "todo", "phase": "p"} for i in "abcd"]
        edges = [{"from": "b", "to": "a"}, {"from": "c", "to": "a"}, {"from": "d", "to": "c"}]
        out = g.analyze(nodes, edges)
        self.assertEqual(out["funnel_heads"], [{"id": "a", "unblocks": 3, "state": "todo"}])
        self.assertEqual(out["critical_path"], ["a", "c", "d"])
        self.assertEqual(g.cycles(nodes, edges + [{"from": "a", "to": "d"}]), [["a", "d", "c"]])


class AnchoredEditTest(unittest.TestCase):
    def files(self):
        texts = {"plan/tasks/%s" % n: read(n) for n in os.listdir(PLAN)}
        return lambda p: texts.get(p)

    def test_ops(self):
        r = self.files()
        out = apply_edits(r, [
            {"op": "replace", "id": "cli-flags", "anchor": "size: S", "text": "size: M"},
            {"op": "insert_after", "id": "cli-flags", "anchor": "Add --verbose to the CLI.", "text": " Keep -v short."},
            {"op": "insert_before", "id": "cli-flags", "anchor": "## Prompt", "text": "## Notes\nwider scope\n\n"},
            {"op": "delete", "id": "cli-flags", "anchor": " no --verbose"},
        ], "plan/tasks")
        spec = load_text(out["plan/tasks/cli-flags.md"])
        self.assertEqual(spec["size"], "M")
        self.assertEqual(spec["sections"]["prompt"], "Add --verbose to the CLI. Keep -v short.")
        self.assertEqual(spec["sections"]["notes"], "wider scope")
        self.assertEqual(spec["sections"]["context"], "The CLI has flag.")

    def test_missing_and_ambiguous_anchor(self):
        r = self.files()
        with self.assertRaises(EditError) as cm:
            apply_edits(r, [{"op": "replace", "id": "cli-flags", "anchor": "no such text", "text": "x"}], "plan/tasks")
        self.assertIn("anchor not found", str(cm.exception))
        with self.assertRaises(EditError) as cm:
            apply_edits(r, [{"op": "replace", "id": "api-auth", "anchor": "mitigation:", "text": "x"}], "plan/tasks")
        self.assertIn("ambiguous (2 occurrences", str(cm.exception))
        out = apply_edits(r, [{"op": "replace", "id": "api-auth", "anchor": "kind: technical", "text": "kind: process",
                               "occurrence": 1}], "plan/tasks")
        self.assertEqual(load_text(out["plan/tasks/api-auth.md"])["sections"]["risks"][1]["kind"], "process")
        with self.assertRaises(EditError) as cm:
            apply_edits(r, [{"op": "delete", "id": "api-auth", "anchor": "mitigation:", "occurrence": 3}], "plan/tasks")
        self.assertIn("only 2 found", str(cm.exception))

    def test_atomic_and_validated(self):
        r = self.files()
        # the second edit fails: nothing is returned, so nothing is written
        with self.assertRaises(EditError):
            apply_edits(r, [{"op": "replace", "id": "cli-flags", "anchor": "size: S", "text": "size: M"},
                            {"op": "replace", "id": "cli-flags", "anchor": "missing", "text": "y"}], "plan/tasks")
        # an edit that leaves an invalid spec fails
        with self.assertRaises(EditError) as cm:
            apply_edits(r, [{"op": "replace", "id": "cli-flags", "anchor": "class: B", "text": "class: Q"}], "plan/tasks")
        self.assertIn("invalid", str(cm.exception))
        # create: only a new file; a file that exists is refused
        new = read("cli-flags.md").replace("cli-flags", "new-task")
        out = apply_edits(r, [{"op": "create", "id": "new-task", "text": new}], "plan/tasks")
        self.assertIn("plan/tasks/new-task.md", out)
        with self.assertRaises(EditError):
            apply_edits(r, [{"op": "create", "id": "cli-flags", "text": new}], "plan/tasks")
        with self.assertRaises(EditError):  # schema of the edit itself
            apply_edits(r, [{"op": "rewrite", "id": "cli-flags"}], "plan/tasks")


class PlanEditGitTest(unittest.TestCase):
    """plan_edit in a repo with a bare origin: a plan/<n> branch, a commit, a push, a client-side PR."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wc-plan-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {k: os.environ.get(k) for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "WAVECOORD_HOME")}
        cfg = os.path.join(self.tmp, "gitconfig")
        with open(cfg, "w") as f:
            f.write("[user]\n\tname = t\n\temail = t@example.invalid\n[init]\n\tdefaultBranch = main\n"
                    "[commit]\n\tgpgsign = false\n")
        os.environ.update(GIT_CONFIG_GLOBAL=cfg, GIT_CONFIG_NOSYSTEM="1", WAVECOORD_HOME=os.path.join(self.tmp, "h"))
        self.addCleanup(self._restore)
        self.origin = os.path.join(self.tmp, "origin.git")
        sh(["git", "init", "-q", "--bare", "-b", "main", self.origin], self.tmp)
        self.repo = os.path.join(self.tmp, "repo")
        sh(["git", "clone", "-q", self.origin, self.repo], self.tmp)
        shutil.copytree(PLAN, os.path.join(self.repo, "plan", "tasks"))
        sh(["git", "add", "."], self.repo)
        sh(["git", "commit", "-q", "-m", "plan"], self.repo)
        sh(["git", "push", "-q", "origin", "HEAD:main"], self.repo)
        self.main = sh(["git", "rev-parse", "main"], self.origin)
        self.prs = []

    def _restore(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def pr(self, branch, base, title, body):
        self.prs.append((branch, base, title, body))
        return "https://example.invalid/pull/%d" % len(self.prs), None

    def test_edit_branch_commit_push_pr(self):
        out = plan_edit(self.repo, "plan/tasks", [{"op": "replace", "id": "cli-flags", "anchor": "budget_usd: 1",
                                                   "text": "budget_usd: 3"}], pr=self.pr, title="plan: raise budget")
        self.assertEqual((out["branch"], out["pr"], out["base"]), ("plan/1", "https://example.invalid/pull/1", "main"))
        self.assertEqual(sh(["git", "rev-parse", "main"], self.origin), self.main)  # default branch untouched
        text = sh(["git", "show", "plan/1:plan/tasks/cli-flags.md"], self.origin)
        self.assertIn("budget_usd: 3", text)
        self.assertEqual(sh(["git", "rev-parse", "plan/1"], self.origin), out["commit"])
        self.assertEqual(self.prs[0][:3], ("plan/1", "main", "plan: raise budget"))
        # no local branch, no leftover worktree
        self.assertEqual(sh(["git", "branch", "--list", "plan/*"], self.repo), "")
        self.assertEqual(len(sh(["git", "worktree", "list"], self.repo).splitlines()), 1)
        # the next new edit is plan/2; an edit on the existing plan/1 adds a commit and opens no new PR
        out2 = plan_edit(self.repo, "plan/tasks", [{"op": "replace", "id": "cli-flags", "anchor": "max_turns: 20",
                                                    "text": "max_turns: 30"}], branch="plan/1", pr=self.pr)
        self.assertEqual((out2["branch"], out2["pr"], len(self.prs)), ("plan/1", None, 1))
        text = sh(["git", "show", "plan/1:plan/tasks/cli-flags.md"], self.origin)
        self.assertIn("max_turns: 30", text)
        self.assertIn("budget_usd: 3", text)
        out3 = plan_edit(self.repo, "plan/tasks", [{"op": "replace", "id": "api-docs", "anchor": "size: XS",
                                                    "text": "size: S"}], pr=self.pr)
        self.assertEqual(out3["branch"], "plan/2")

    def test_failure_pushes_nothing(self):
        with self.assertRaises(EditError):
            plan_edit(self.repo, "plan/tasks", [
                {"op": "replace", "id": "cli-flags", "anchor": "size: S", "text": "size: M"},
                {"op": "replace", "id": "cli-flags", "anchor": "absent anchor", "text": "x"}], pr=self.pr)
        self.assertEqual(sh(["git", "for-each-ref", "refs/heads/plan"], self.origin), "")
        self.assertEqual(self.prs, [])
        with self.assertRaises(EditError):  # never another branch than plan/<n>
            plan_edit(self.repo, "plan/tasks", [{"op": "delete", "id": "cli-flags", "anchor": "size: S"}],
                      branch="main", pr=self.pr)

    def test_git_source_reads_the_default_branch(self):
        sh(["git", "fetch", "-q", "origin"], self.repo)
        with open(os.path.join(self.repo, "plan", "tasks", "cli-flags.md"), "w") as f:
            f.write("dirty working tree, not on main\n")
        plan = BuiltinPlan(GitSource(self.repo, "refs/remotes/origin/main", "plan/tasks"))
        self.assertEqual(plan.get("cli-flags")["size"], "S")
        self.assertTrue(plan.validate()["ok"])


class CommandAdapterTest(unittest.TestCase):
    def setUp(self):
        self.log = tempfile.mktemp()
        self.addCleanup(lambda: os.path.exists(self.log) and os.unlink(self.log))
        os.environ["FAKE_PLAN_LOG"] = self.log
        self.addCleanup(os.environ.pop, "FAKE_PLAN_LOG", None)
        cmds = {v: [FAKE_PLAN, v] for v in ("ready", "get", "graph", "validate", "edit")}
        self.plan = CommandPlan(cmds, _paths.ROOT)

    def calls(self):
        with open(self.log) as f:
            return [json.loads(l) for l in f]

    def test_fixtures_match_the_contract(self):
        shapes = {"ready-in": "empty_in", "ready-out": "ready_out", "get-in": "get_in", "graph-in": "empty_in",
                  "graph-out": "graph_out", "validate-in": "validate_in", "validate-out": "validate_out",
                  "validate-out-errors": "validate_out", "edit-in": "edit_in", "edit-out": "edit_out"}
        seen = set()
        for p in sorted(glob.glob(os.path.join(ADAPTER, "*.json"))):
            name = os.path.basename(p)[:-5]
            obj = load_json(p)
            with self.subTest(fixture=name):
                if name == "get-out":
                    self.assertEqual(schemaload.errors(obj, "task-spec"), [])
                else:
                    self.assertEqual(schemaload.errors(obj, "plan-adapter", "#/definitions/" + shapes[name]), [])
                seen.add(name)
        self.assertEqual(seen, set(shapes) | {"get-out"})

    def test_calls(self):
        self.assertEqual(self.plan.get("api-auth")["id"], "api-auth")
        with self.assertRaises(PlanError) as cm:
            self.plan.get("nope")
        self.assertIn("no task nope", str(cm.exception))
        r = self.plan.ready({}, [])
        self.assertEqual([t["id"] for t in r["tasks"]], ["api-auth"])  # readme-install is done
        live = [{"glob": "src/auth/x.py", "mode": "exclusive", "task": "t9", "lease_id": 1}]
        self.assertEqual(self.plan.ready({}, live)["tasks"], [])  # wavecoord adds the overlap check
        out = self.plan.graph({"api-auth": "active"})
        self.assertEqual(out["funnel_heads"], [])
        self.assertEqual({n["id"]: n["state"] for n in out["nodes"]}["api-auth"], "active")
        self.assertTrue(self.plan.validate()["ok"])
        self.assertTrue(self.plan.validate(load_json(os.path.join(ADAPTER, "get-out.json")))["ok"])
        edits = load_json(os.path.join(ADAPTER, "edit-in.json"))["edits"]
        self.assertEqual(self.plan.edit(edits)["branch"], "plan/3")
        calls = self.calls()
        self.assertEqual([c["verb"] for c in calls], ["get", "get", "ready", "ready", "graph", "validate", "validate",
                                                      "edit"])
        self.assertEqual(calls[0]["stdin"], {"id": "api-auth"})
        self.assertEqual(calls[-1]["stdin"], {"branch": None, "edits": edits})

    def test_bad_output_and_failure(self):
        os.environ["FAKE_PLAN_BAD"] = "1"
        try:
            with self.assertRaises(PlanError) as cm:
                self.plan.ready({}, [])
            self.assertIn("invalid task spec", str(cm.exception))
            with self.assertRaises(PlanError):
                self.plan.graph({})
        finally:
            os.environ.pop("FAKE_PLAN_BAD")
        os.environ["FAKE_PLAN_FAIL"] = "1"
        try:
            with self.assertRaises(PlanError) as cm:
                self.plan.ready({}, [])
            self.assertIn("tracker is down", str(cm.exception))
        finally:
            os.environ.pop("FAKE_PLAN_FAIL")
        with self.assertRaises(PlanError):
            CommandPlan({}, _paths.ROOT).ready({}, [])


if __name__ == "__main__":
    unittest.main()
