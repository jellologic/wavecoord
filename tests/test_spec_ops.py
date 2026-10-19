"""The op API (docs/design/spec-layer.md, section 9.3): each op, atomicity (P6), the write CLI and migrate-plan."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import _paths
from wavecoord.specs import doc, source
from wavecoord.specs.fingerprint import fingerprint
from wavecoord.specs.ops import OpsError, apply_ops, split_sentences

ROT = "auth/sessions/token-rotation"
IMPL = ROT + "/rotate-impl"
EXEC = {"model": "sonnet", "effort": "medium", "max_turns": 30, "budget_usd": 1, "timeout_min": 20}


def files():
    return source.read_tree(_paths.FIXTURES, "specs")


def after(fs, ops, **kw):
    changes, moves, problems = apply_ops(fs, ops, **kw)
    out = dict(fs)
    for p, t in changes.items():
        if t is None:
            out.pop(p, None)
        else:
            out[p] = t
    return out, changes, moves, problems


def rec(fs, rel):
    return doc.parse(fs[rel])


class OpsTest(unittest.TestCase):
    def test_create_requirement_and_task(self):
        fs, changes, _, _ = after(files(), [
            {"op": "create", "id": "auth/sessions/revoke", "fields": {"title": "Revoke"},
             "sections": {"Statement": "If a token is revoked, then the service shall reject it. It stays revoked."}},
            {"op": "create", "id": "auth/sessions/revoke/check", "fields": {
                "title": "Check", "paths": ["src/auth/revoke.py"], "class": "B", "size": "S", "exec": EXEC,
                "outputs": [{"id": "src/auth/revoke.py", "kind": "file"}]},
             "sections": {"Context": "None yet.", "Prompt": "Add it."}}])
        r = rec(fs, "auth/sessions/revoke/_node.md")
        self.assertEqual((r["status"], r["sections"]["statement"]),
                         ("draft", "If a token is revoked, then the service shall reject it. It stays revoked."))  # never split
        t = rec(fs, "auth/sessions/revoke/tasks/check.md")
        self.assertEqual(t["basis"], fingerprint(r))
        self.assertEqual(sorted(changes), ["auth/sessions/revoke/_node.md", "auth/sessions/revoke/tasks/check.md"])

    def test_create_refusals(self):
        for op, msg in (({"op": "create", "id": ROT}, "exists"),
                        ({"op": "create", "id": "auth/nowhere/x"}, "parent"),
                        ({"op": "create", "id": "a/b/c/d/e"}, "1 to 4"),
                        ({"op": "create", "id": "auth/x", "kind": "task"}, "feature by its depth")):
            with self.subTest(op=op):
                with self.assertRaises(OpsError) as cm:
                    apply_ops(files(), [op])
                self.assertIn(msg, str(cm.exception))

    def test_set_status_section_lists_artifacts(self):
        fs, _, _, _ = after(files(), [
            {"op": "set", "id": IMPL, "key": "exec.max_turns", "value": 90},
            {"op": "set_status", "id": IMPL, "status": "on_hold"},
            {"op": "add", "id": IMPL, "key": "tags", "value": "zz"},
            {"op": "add", "id": IMPL, "key": "tags", "value": "aa"},
            {"op": "add", "id": IMPL, "key": "Acceptance", "value": "python -m unittest tests.test_more"},
            {"op": "set_artifact", "id": IMPL, "list": "outputs",
             "artifact": {"id": "auth.rotation.service", "kind": "interface",
                          "contract": "rotate(refresh_token) -> TokenPair; the old refresh token is revoked"}},
            {"op": "set_scenario", "id": ROT, "name": "Expired token",
             "steps": [["given", "an expired token"], ["then", "refresh fails"]]},
        ])
        t = rec(fs, ROT + "/tasks/rotate-impl.md")
        self.assertEqual((t["exec"]["max_turns"], t["status"], t["tags"]), (90, "on_hold", ["aa", "zz"]))
        self.assertEqual(t["sections"]["acceptance"][-1], "python -m unittest tests.test_more")
        self.assertEqual(rec(fs, ROT + "/_node.md")["sections"]["scenarios"][-1]["steps"][0],
                         ["Given", "an expired token"])

    def test_coordinator_status_is_never_a_plan_change(self):
        with self.assertRaises(OpsError) as cm:
            apply_ops(files(), [{"op": "set_status", "id": IMPL, "status": "in_progress"}])
        self.assertIn("coordinator", str(cm.exception))
        # the flag is not part of the public op API (MCP spec_edit, REST, the CLI)
        with self.assertRaises(OpsError):
            apply_ops(files(), [{"op": "set_status", "id": IMPL, "status": "done", "coordinator": True}])
        with self.assertRaises(OpsError) as cm:
            apply_ops(files(), [{"op": "create", "id": ROT + "/fake", "fields": {
                "status": "done", "title": "Fake", "paths": ["src/fake.py"], "class": "B", "size": "S",
                "exec": EXEC, "outputs": [{"id": "src/fake.py", "kind": "file"}]},
                "sections": {"Context": "None.", "Prompt": "Nothing."}}])
        self.assertIn("coordinator", str(cm.exception))
        # migrate-plan keeps done tasks done: an internal caller only
        apply_ops(files(), [{"op": "set_status", "id": IMPL, "status": "done"}], coordinator=True)

    def test_lead_status_transitions(self):
        done = "auth/sessions/session-store/store-api"  # status done in the fixture
        for st in ("planned", "draft", "dropped", "on_hold"):
            with self.subTest(frm="done", to=st):
                with self.assertRaises(OpsError):
                    apply_ops(files(), [{"op": "set_status", "id": done, "status": st}])
        live = after(files(), [{"op": "set_status", "id": IMPL, "status": "in_progress"}], coordinator=True)[0]
        with self.assertRaises(OpsError):
            apply_ops(live, [{"op": "set_status", "id": IMPL, "status": "on_hold"}])
        with self.assertRaises(OpsError):
            apply_ops(live, [{"op": "set_status", "id": IMPL, "status": "planned"}])
        after(live, [{"op": "set", "id": IMPL, "key": "title", "value": "Still live"}])  # other keys may change
        # a done task may move: its status stays
        after(files(), [{"op": "mv", "from": "auth/sessions/session-store", "to": "auth/sessions/storage"}])

    def test_plan_change_problems_for_hand_edits(self):
        from wavecoord.specs.check import plan_change_problems
        from wavecoord.specs.tree import Tree
        fs = files()
        hand = dict(fs)
        hand[ROT + "/tasks/rotate-impl.md"] = fs[ROT + "/tasks/rotate-impl.md"].replace("status: planned", "status: done")
        probs = plan_change_problems(Tree(fs), Tree(hand))
        self.assertEqual([p.rule for p in probs], ["status"])
        self.assertEqual(plan_change_problems(Tree(fs), Tree(fs)), [])

    def test_newlines_in_values_are_refused(self):
        bad = [
            {"op": "set_scenario", "id": ROT, "name": "n\n- Then evil", "steps": [["Given", "x"]]},
            {"op": "set_scenario", "id": ROT, "name": "n", "steps": [["Given", "x\n- Then y"]]},
            {"op": "set", "id": IMPL, "key": "title", "value": "a\nb"},
            {"op": "set_section", "id": IMPL, "section": "Risks",
             "value": [{"risk": "r\nx", "mitigation": "m", "kind": "llm"}]},
            {"op": "set_artifact", "id": IMPL, "list": "outputs",
             "artifact": {"id": "src/auth/rotate.py", "kind": "file", "contract": "a\nb"}},
        ]
        for op in bad:
            with self.subTest(op=op):
                with self.assertRaises(OpsError):
                    apply_ops(files(), [op])

    def test_bad_steps_and_unparsable_subtrees_are_ops_errors(self):
        for steps in ([["Given"]], [[1, "x"]], ["Given x"], [["Given", "x", "y"]]):
            with self.subTest(steps=steps):
                with self.assertRaises(OpsError):
                    apply_ops(files(), [{"op": "set_scenario", "id": ROT, "name": "n", "steps": steps}])
        fs = files()
        fs["auth/README.md"] = "notes\n"
        with self.assertRaises(OpsError):
            apply_ops(fs, [{"op": "mv", "from": "auth", "to": "authn"}])
        fs = files()
        fs[ROT + "/tasks/rotate-tests.md"] += "\n## Bogus\nx\n"
        with self.assertRaises(OpsError):
            apply_ops(fs, [{"op": "mv", "from": ROT, "to": "auth/sessions/rotation"}])

    def test_ops_refuse_records_they_cannot_write_back(self):
        fs = files()
        fs[ROT + "/_node.md"] = fs[ROT + "/_node.md"].replace("title: Token", "owner: alice\ntitle: Token")
        with self.assertRaises(OpsError) as cm:
            apply_ops(fs, [{"op": "add", "id": ROT, "key": "tags", "value": "x"}])
        self.assertIn("owner", str(cm.exception))

    def test_basis_of_tasks_created_in_the_batch_is_the_final_one(self):
        task = {"op": "create", "id": ROT + "/docs", "fields": {
            "status": "planned", "title": "Docs", "paths": ["docs/rotate.md"], "class": "C", "size": "S",
            "exec": EXEC, "outputs": [{"id": "docs/rotate.md", "kind": "file"}]},
            "sections": {"Context": "None.", "Prompt": "Write it."}}
        fs, _, _, problems = after(files(), [task, {"op": "add", "id": ROT, "key": "Verify", "value": "true"},
                                             {"op": "rebase_basis", "task": IMPL},
                                             {"op": "rebase_basis", "task": ROT + "/rotate-tests"}])
        self.assertEqual(rec(fs, ROT + "/tasks/docs.md")["basis"], fingerprint(rec(fs, ROT + "/_node.md")))
        self.assertEqual([str(p) for p in problems if p.rule == "suspect"], [])

    def test_mv_keeps_every_basis(self):
        for frm, to in (("auth/sessions/session-store", "auth/sessions/storage"), ("auth/sessions", "auth/sess")):
            with self.subTest(frm=frm):
                fs, _, _, problems = after(files(), [{"op": "mv", "from": frm, "to": to}])
                self.assertEqual([str(p) for p in problems if p.rule == "suspect"], [])
                from wavecoord.specs.tree import Tree
                self.assertEqual(Tree(fs).resolve(frm), to)

    def test_mv_back_drops_the_alias(self):
        fs, _, _, _ = after(files(), [{"op": "mv", "from": ROT + "/rotate-tests", "to": ROT + "/rotate-tests2"}])
        fs, _, _, _ = after(fs, [{"op": "mv", "from": ROT + "/rotate-tests2", "to": ROT + "/rotate-tests"}])
        t = rec(fs, ROT + "/tasks/rotate-tests.md")
        self.assertEqual(t.get("aliases"), [ROT + "/rotate-tests2"])

    def test_delete_force_message(self):
        with self.assertRaises(OpsError) as cm:
            apply_ops(files(), [{"op": "delete", "id": IMPL}])
        self.assertNotIn("use force", str(cm.exception))
        self.assertIn("change them first", str(cm.exception))

    def test_supersede_itself_is_refused(self):
        with self.assertRaises(OpsError):
            apply_ops(files(), [{"op": "supersede", "old": ROT, "new": ROT}])

    def test_mv_rewrites_references_and_keeps_aliases(self):
        fs, changes, moves, _ = after(files(), [{"op": "mv", "from": ROT, "to": "auth/sessions/rotation"}])
        self.assertIn(("auth/sessions/token-rotation/_node.md", "auth/sessions/rotation/_node.md"), moves)
        self.assertFalse([p for p in fs if p.startswith(ROT + "/")])
        tests = rec(fs, "auth/sessions/rotation/tasks/rotate-tests.md")
        self.assertEqual(tests["needs"], ["auth/sessions/rotation/rotate-impl"])
        self.assertEqual(tests["inputs"][0]["from"], "auth/sessions/rotation/rotate-impl")
        self.assertIn(ROT + "/rotate-tests", tests["aliases"])
        self.assertIn(ROT, rec(fs, "auth/sessions/rotation/_node.md")["aliases"])
        self.assertEqual(rec(fs, "auth/decisions/2026-10-19-rotating-tokens.md")["affects"], ["auth/sessions/rotation"])
        # the fingerprint does not depend on the path, so the tasks are not suspect
        self.assertEqual(tests["basis"], fingerprint(rec(fs, "auth/sessions/rotation/_node.md")))

    def test_rebase_supersede_delete(self):
        fs = files()
        fs, _, _, _ = after(fs, [{"op": "set_section", "id": ROT, "section": "Statement",
                                  "value": "When an access token is near expiry, the auth service shall rotate it."}])
        self.assertNotEqual(rec(fs, ROT + "/tasks/rotate-impl.md")["basis"], fingerprint(rec(fs, ROT + "/_node.md")))
        fs, _, _, _ = after(fs, [{"op": "rebase_basis", "task": IMPL}])
        self.assertEqual(rec(fs, ROT + "/tasks/rotate-impl.md")["basis"], fingerprint(rec(fs, ROT + "/_node.md")))
        with self.assertRaises(OpsError):
            apply_ops(fs, [{"op": "delete", "id": IMPL}])  # rotate-tests needs it
        fs2, changes, _, _ = after(fs, [{"op": "delete", "id": ROT + "/rotate-tests"}])
        self.assertIsNone(changes[ROT + "/tasks/rotate-tests.md"])
        with self.assertRaises(OpsError):
            apply_ops(fs, [{"op": "delete", "id": ROT}])  # has children

    def test_p6_atomic(self):
        fs = files()
        ops = [{"op": "set", "id": IMPL, "key": "title", "value": "Renamed"},
               {"op": "add", "id": IMPL, "key": "needs", "value": "auth/nowhere"}]  # leaves a broken reference
        with self.assertRaises(OpsError) as cm:
            apply_ops(fs, ops)
        self.assertIn("invalid", str(cm.exception))
        self.assertEqual(fs, files())
        with self.assertRaises(OpsError):
            apply_ops(fs, [{"op": "bogus"}])

    def test_split_sentences(self):
        self.assertEqual(split_sentences("One. Two! Three? `x.` four.\n- a. B."), "One.\nTwo!\nThree?\n`x.` four.\n- a. B.")

    def test_split_sentences_keeps_lists_code_and_abbreviations(self):
        self.assertEqual(split_sentences("Steps:\n1. Write the tests first. Then implement.\n2. Run them.\n   Keep it small."),
                         "Steps:\n1. Write the tests first. Then implement.\n2. Run them.\n   Keep it small.")
        self.assertEqual(split_sentences("    code block line one. Two\n    more."), "    code block line one. Two\n    more.")
        for one in ("The rate limiter shall allow approx. 100 requests per second.",
                    "Use a store, e.g. Redis, for it.", "Keep v2. 0 too.", "Serve the U.S. Region first.",
                    "Retry after 1.5 s, i.e. Twice."):
            with self.subTest(one=one):
                self.assertEqual(split_sentences(one), one)
        self.assertEqual(split_sentences("~~~\nA. B. C.\n~~~"), "~~~\nA. B. C.\n~~~")

    def test_statement_is_not_split(self):
        fs, _, _, _ = after(files(), [{"op": "set_section", "id": ROT, "section": "Statement",
                                       "value": "The service shall allow approx. 100 rotations per second."}])
        self.assertEqual(rec(fs, ROT + "/_node.md")["sections"]["statement"],
                         "The service shall allow approx. 100 rotations per second.")


def run(*args, cwd, stdin=None):
    env = dict(os.environ, PYTHONPATH=_paths.SRC)
    return subprocess.run([sys.executable, "-m", "wavecoord", "spec"] + list(args), cwd=cwd, env=env, input=stdin,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)


class CliWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wc-specw-"))
        shutil.copytree(os.path.join(_paths.FIXTURES, "specs"), os.path.join(self.tmp, "specs"))
        subprocess.run(["git", "init", "-q"], cwd=self.tmp, check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_then_write(self):
        before = source.read_tree(self.tmp, "specs")
        out = run("set", IMPL, "title", "Rotate tokens", "--dry-run", cwd=self.tmp)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("-title: Rotate refresh tokens\n+title: Rotate tokens", out.stdout)
        self.assertEqual(source.read_tree(self.tmp, "specs"), before)
        self.assertEqual(run("set", IMPL, "title", "Rotate tokens", cwd=self.tmp).returncode, 0)
        self.assertEqual(rec(source.read_tree(self.tmp, "specs"), ROT + "/tasks/rotate-impl.md")["title"], "Rotate tokens")

    def test_refusal_writes_nothing(self):
        before = source.read_tree(self.tmp, "specs")
        out = run("add", IMPL, "needs", "auth/nowhere", cwd=self.tmp)
        self.assertEqual(out.returncode, 1)
        self.assertIn("does not exist", out.stderr)
        self.assertEqual(source.read_tree(self.tmp, "specs"), before)

    def test_apply_mv_and_reads(self):
        ops = {"ops": [{"op": "mv", "from": ROT, "to": "auth/sessions/rotation"}]}
        out = run("apply", "-", "--json", cwd=self.tmp, stdin=json.dumps(ops))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(json.loads(out.stdout)["moves"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "specs", ROT)))
        self.assertEqual(run("check", cwd=self.tmp).returncode, 0)
        waves = json.loads(run("waves", "--json", cwd=self.tmp).stdout)
        self.assertEqual(waves["waves"][0], ["auth/sessions/rotation/rotate-impl"])
        self.assertEqual(run("suspect", cwd=self.tmp).returncode, 0)
        trace = json.loads(run("trace", "auth/sessions/token-rotation", cwd=self.tmp).stdout)
        self.assertEqual(trace["id"], "auth/sessions/rotation")  # resolved through the alias

    def test_new_and_scenario(self):
        out = run("new", "auth/sessions/revoke", "--title", "Revoke", "--section",
                  "Statement=If a token is revoked, then the service shall reject it.", cwd=self.tmp)
        self.assertEqual(out.returncode, 0, out.stderr)
        out = run("scenario", "auth/sessions/revoke", "Revoked token", "Given a revoked token",
                  "Then the request fails", cwd=self.tmp)
        self.assertEqual(out.returncode, 0, out.stderr)
        r = rec(source.read_tree(self.tmp, "specs"), "auth/sessions/revoke/_node.md")
        self.assertEqual(r["sections"]["scenarios"][0]["steps"], [["Given", "a revoked token"],
                                                                  ["Then", "the request fails"]])

    def test_migrate_plan(self):
        shutil.rmtree(os.path.join(self.tmp, "specs"))
        shutil.copytree(os.path.join(_paths.FIXTURES, "plan", "tasks"), os.path.join(self.tmp, "plan", "tasks"))
        out = run("migrate-plan", "--dry-run", "--no-coord", cwd=self.tmp)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "plan", "tasks", "api-auth.md")))
        out = run("migrate-plan", "--no-coord", cwd=self.tmp)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("api-auth -> legacy/unsorted/unsorted/api-auth", out.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "plan", "tasks", "api-auth.md")))
        fs = source.read_tree(self.tmp, "specs")
        t = rec(fs, "legacy/unsorted/unsorted/tasks/api-auth.md")
        self.assertEqual(t["needs"], ["legacy/unsorted/unsorted/session-store"])
        self.assertEqual(t["exec"]["model"], "opus")
        self.assertEqual(t["sections"]["acceptance"][0], "python -m unittest tests.test_rotate")
        self.assertEqual(run("check", cwd=self.tmp).returncode, 0, run("check", cwd=self.tmp).stdout)


if __name__ == "__main__":
    unittest.main()
