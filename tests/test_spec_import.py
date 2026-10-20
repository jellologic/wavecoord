"""`wavecoord spec import` (docs/import.md): Spec Kit, Kiro and OpenSpec sample projects become draft records that
pass `spec check`; ids are deterministic; --dry-run writes nothing; the source files stay as they are; a re-import
changes nothing or is refused."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import _paths
from wavecoord.specs import importers, source
from wavecoord.specs.check import ears_class
from wavecoord.specs.importers import find_paths, to_ears
from wavecoord.specs.ops import apply_ops
from wavecoord.specs.tree import Tree

SDD = os.path.join(_paths.FIXTURES, "sdd")


def run(*args, cwd):
    env = dict(os.environ, PYTHONPATH=_paths.SRC)
    return subprocess.run([sys.executable, "-m", "wavecoord", "spec"] + list(args), cwd=cwd, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)


def snapshot(root):
    out = {}
    for d, dirs, fs in os.walk(root):
        dirs.sort()
        for f in sorted(fs):
            p = os.path.join(d, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = fh.read()
    return out


class Project(unittest.TestCase):
    fmt = None
    extra = ()  # import flags
    spec_dir = "specs"

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wc-import-"))
        self.repo = os.path.join(self.tmp, "repo")
        shutil.copytree(os.path.join(SDD, self.fmt), self.repo)
        self.source = snapshot(self.repo)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def imp(self, *flags):
        return run("import", "--repo", self.repo, "--from", self.fmt, *(list(self.extra) + list(flags)), cwd=self.tmp)

    def tree(self):
        return Tree(source.read_tree(self.repo, self.spec_dir))

    def rec(self, nid):
        n = self.tree().nodes.get(nid) or self.tree().decisions.get(nid)
        self.assertIsNotNone(n, nid)
        return n.rec

    def check_ok(self):
        out = run("check", "--repo", self.repo, "--dir", self.spec_dir, "--json", cwd=self.tmp)
        data = json.loads(out.stdout)
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertEqual([p for p in data["problems"] if p["level"] == "error"], [])
        return data

    def assert_imports(self, expected_ids):
        """The common contract: dry-run writes nothing; the import passes check with warnings only; ids are
        as expected; every record is draft (or proposed, or a deprecated requirement); sources untouched;
        a re-import changes nothing; an import over a changed tree is refused."""
        dry = self.imp("--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("+++ b/%s/" % self.spec_dir, dry.stdout)
        self.assertIn("next:", dry.stderr)
        self.assertEqual(snapshot(self.repo), self.source, "--dry-run wrote something")
        out = self.imp()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("/wavecoord:wave-plan", out.stdout)
        data = self.check_ok()
        self.assertTrue([p for p in data["problems"] if p["level"] == "warning"])
        tree = self.tree()
        self.assertEqual(sorted(tree.nodes) + sorted(tree.decisions), expected_ids)
        for nid, n in list(tree.nodes.items()) + list(tree.decisions.items()):
            self.assertIn(n.status, ("draft", "proposed", "deprecated"), nid)
        for n in tree.nodes.values():  # provenance
            if n.kind in ("requirement", "feature", "task"):
                self.assertRegex(n.text, r"Imported from |Made by the import", n.id)
        after = snapshot(self.repo)
        self.assertEqual({k: v for k, v in after.items() if not k.startswith(self.spec_dir + os.sep)}, self.source)
        again = self.imp()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("already imported, nothing changed", again.stdout)
        self.assertEqual(snapshot(self.repo), after)
        cap = expected_ids[0]
        self.assertEqual(run("set", cap, "title", "Changed", "--repo", self.repo, "--dir", self.spec_dir,
                             cwd=self.tmp).returncode, 0)
        refused = self.imp()
        self.assertEqual(refused.returncode, 2)
        self.assertIn("exists and differs from this import", refused.stderr)
        return out

    def assert_deterministic(self):
        root2 = os.path.join(self.tmp, "elsewhere", "copy")
        shutil.copytree(os.path.join(SDD, self.fmt), root2)
        a = importers.build(self.fmt, self.repo, self.repo, self.fmt)
        b = importers.build(self.fmt, root2, root2, self.fmt)
        self.assertEqual(a, b)
        ops, report = a
        changes, _, problems = apply_ops({}, ops)  # what spec_edit gets: plain ops on an empty tree
        self.assertFalse([p for p in problems if p.level == "error"])
        self.assertEqual(report["from"], self.fmt)
        for v in report["verify"]:
            self.assertEqual(v["msg"], "no Verify in the source: add a Verify command")
        for nid in (op["id"] for op in ops):
            self.assertLessEqual(len(nid.replace("/", ".")), 64)
        return ops, report


class SpecKitTest(Project):
    fmt = "speckit"
    extra = ("--dir", "docs/specs")
    spec_dir = "docs/specs"
    IDS = ["speckit", "speckit/album-sharing", "speckit/album-sharing/fr-001", "speckit/album-sharing/fr-002",
           "speckit/album-sharing/fr-003", "speckit/album-sharing/fr-004", "speckit/album-sharing/us-1",
           "speckit/album-sharing/us-1/t001", "speckit/album-sharing/us-1/t002", "speckit/album-sharing/us-1/t003",
           "speckit/photo-albums", "speckit/photo-albums/fr-001", "speckit/photo-albums/fr-002",
           "speckit/photo-albums/fr-003", "speckit/photo-albums/fr-004", "speckit/photo-albums/fr-005",
           "speckit/photo-albums/polish", "speckit/photo-albums/polish/t012", "speckit/photo-albums/polish/t013",
           "speckit/photo-albums/setup", "speckit/photo-albums/setup/t002", "speckit/photo-albums/setup/t003",
           "speckit/photo-albums/setup/t004", "speckit/photo-albums/us-1", "speckit/photo-albums/us-1/t005",
           "speckit/photo-albums/us-1/t006", "speckit/photo-albums/us-1/t007", "speckit/photo-albums/us-2",
           "speckit/photo-albums/us-2/t009", "speckit/photo-albums/us-2/t010", "speckit/photo-albums/us-2/t011",
           "speckit/decisions/2025-06-13-constitution"]

    def test_import(self):
        out = self.assert_imports(self.IDS)
        self.assertIn('Set "spec": {"dir": "docs/specs"}', out.stdout)
        fa = "speckit/photo-albums"
        r = self.rec(fa + "/fr-003")
        self.assertEqual(r["sections"]["statement"], "The system shall let users reorder albums by drag and drop.")
        self.assertIn("Imported from specs/001-photo-albums/spec.md (FR-003).", r["sections"]["notes"])
        us1 = self.rec(fa + "/us-1")
        self.assertEqual(us1["needs"], [fa + "/setup"])
        self.assertEqual(us1["tags"], ["p1"])
        self.assertEqual(us1["sections"]["scenarios"][0]["steps"],
                         [["Given", "a library with three photos"], ["When", "the user creates an album from two of"
                                                                              " them"],
                          ["Then", "the album lists those two photos"]])
        self.assertIn("T008 Add logging for album operations (no file path in the source)", us1["sections"]["notes"])
        self.assertIn("checked done in the source", self.rec(fa + "/setup")["sections"]["notes"])
        t6 = self.rec(fa + "/us-1/t006")
        self.assertEqual(t6["paths"], ["src/albums/service.py"])
        self.assertEqual(t6["needs"], [fa + "/setup/t004", fa + "/us-1/t005"])  # "depends on T004", then the stage
        self.assertEqual(self.rec(fa + "/us-2/t011")["needs"], [fa + "/us-2/t009", fa + "/us-2/t010"])
        self.assertNotIn("needs", self.rec(fa + "/us-2/t010"))  # [P]: no need inside its phase
        self.assertEqual(self.rec(fa + "/setup/t002")["paths"], ["tests/__init__.py"])
        self.assertEqual(self.rec(fa + "/polish")["needs"], [fa + "/us-1", fa + "/us-2"])
        self.assertIn("plan.md", self.rec(fa)["sections"]["design"])
        t3 = self.rec("speckit/album-sharing/us-1/t003")  # [P] tasks that write the same file
        self.assertEqual(t3["conflicts_with"], ["speckit/album-sharing/us-1/t001", "speckit/album-sharing/us-1/t002"])
        sharing = self.rec("speckit/album-sharing")
        self.assertIn("FR-003: notification channel not specified - email or in-app?",
                      sharing["sections"]["open_questions"])
        self.assertIn("Review Checklist", sharing["sections"]["design"])
        dec = self.rec("speckit/decisions/2025-06-13-constitution")
        self.assertIn("Library-First", dec["sections"]["decision"])
        for flag in ("non-ears", "no-paths", "done", "conflict", "unknown-section", "grouping"):
            self.assertIn(flag, out.stdout)
        self.assertEqual(out.stdout.count("add a Verify command"), 14)

    def test_spec_dir_collision_is_refused(self):
        out = run("import", "--repo", self.repo, "--from", "speckit", "--dry-run", cwd=self.tmp)
        self.assertEqual(out.returncode, 2)
        self.assertIn("inside the spec dir specs/", out.stderr)
        self.assertEqual(snapshot(self.repo), self.source)
        moved = os.path.join(self.repo, "speckit")  # moved away from specs/: --src reads it
        os.rename(os.path.join(self.repo, "specs"), moved)
        out = run("import", "--repo", self.repo, "--from", "speckit", "--src", moved, cwd=self.tmp)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.check_ok_in("specs")
        self.assertIn("Imported from speckit/001-photo-albums/spec.md (FR-001).",
                      Tree(source.read_tree(self.repo, "specs")).nodes["speckit/photo-albums/fr-001"].text)

    def check_ok_in(self, d):
        self.spec_dir = d
        self.check_ok()

    def test_deterministic(self):
        ops, report = self.assert_deterministic()
        self.assertEqual(report["created"], {"capability": 1, "decision": 1, "feature": 2, "requirement": 14,
                                             "task": 14})

    def test_json_dry_run_gives_the_ops_for_spec_edit(self):
        out = self.imp("--dry-run", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(out.stdout)
        self.assertEqual(data["ops"], importers.build("speckit", self.repo, self.repo, "speckit")[0])
        self.assertIn("speckit/photo-albums/_node.md", data["files"])
        self.assertFalse(data["up_to_date"])
        self.assertEqual(snapshot(self.repo), self.source)


class KiroTest(Project):
    fmt = "kiro"
    IDS = ["kiro", "kiro/csv-export", "kiro/csv-export/r1-1", "kiro/csv-export/r1-1/t1", "kiro/csv-export/r1-1/t2",
           "kiro/csv-export/r1-2", "kiro/task-reminders", "kiro/task-reminders/r1-1", "kiro/task-reminders/r1-1/t2-1",
           "kiro/task-reminders/r1-2", "kiro/task-reminders/r1-2/t2-2", "kiro/task-reminders/r1-2/t2-3",
           "kiro/task-reminders/r1-3", "kiro/task-reminders/r2-1", "kiro/task-reminders/r2-1/t3",
           "kiro/task-reminders/r2-2", "kiro/task-reminders/r2-3", "kiro/task-reminders/unlinked"]

    def test_import(self):
        out = self.assert_imports(self.IDS)
        tr = "kiro/task-reminders"
        self.assertEqual(self.rec(tr + "/r1-3")["sections"]["statement"],
                         "If the reminder time is in the past, then the system shall reject the reminder with a"
                         " validation error.")
        self.assertEqual(self.rec(tr + "/r2-2")["sections"]["statement"],
                         "While a task is marked done, the system shall not send its reminders.")
        self.assertEqual(self.rec(tr + "/r2-3")["sections"]["statement"], "The system should make snoozing feel quick")
        self.assertIn("As a user, I want to snooze a reminder", self.rec(tr + "/r2-1")["sections"]["rationale"])
        t21 = self.rec(tr + "/r1-1/t2-1")
        self.assertEqual(t21["paths"], ["src/todo/reminder_store.py"])
        self.assertIn("Also covers kiro/task-reminders/r1-3.", t21["sections"]["notes"])
        self.assertIn("Part of 2 Implement reminder scheduling.", t21["sections"]["context"])
        self.assertNotIn("needs", t21)  # task 1 is done: nothing to wait for
        self.assertEqual(self.rec(tr + "/r1-2/t2-3")["tags"], ["optional"])
        self.assertEqual(self.rec(tr + "/r2-1/t3")["needs"], [tr + "/r1-2/t2-3"])  # file order
        self.assertIn("Review the notification wording", self.rec(tr + "/unlinked")["sections"]["notes"])
        cap = self.rec("kiro")
        self.assertIn("A to-do list app for small teams.", cap["sections"]["intent"])
        self.assertIn("Steering: .kiro/steering/tech.md", cap["sections"]["design"])
        self.assertIn("Scheduler", self.rec(tr)["sections"]["design"])
        self.assertIn("non-ears", out.stdout)

    def test_story_granularity(self):
        out = self.imp("--granularity", "story")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.check_ok()
        reqs = sorted(n for n, x in self.tree().nodes.items() if x.kind == "requirement")
        self.assertEqual(reqs, ["kiro/csv-export/r1", "kiro/task-reminders/r1", "kiro/task-reminders/r2",
                                "kiro/task-reminders/unlinked"])
        r1 = self.rec("kiro/task-reminders/r1")
        self.assertIn("- 1.3 If the reminder time is in the past, then the system shall reject", r1["sections"]["notes"])

    def test_deterministic(self):
        self.assert_deterministic()


class OpenSpecTest(Project):
    fmt = "openspec"
    IDS = ["openspec", "openspec/changes", "openspec/changes/add-note-tags", "openspec/changes/add-note-tags/t1-1",
           "openspec/changes/add-note-tags/t1-2", "openspec/changes/add-note-tags/t2-1",
           "openspec/changes/add-note-tags/t2-2", "openspec/notes", "openspec/notes/create-note",
           "openspec/notes/delete-note", "openspec/notes/note-size-limit", "openspec/notes/note-tags",
           "openspec/search", "openspec/search/fuzzy-matching", "openspec/search/tag-filter",
           "openspec/search/word-search"]

    def test_import(self):
        out = self.assert_imports(self.IDS)
        notes = "openspec/notes"
        self.assertEqual(self.rec(notes + "/create-note")["sections"]["statement"],
                         "The system shall store a new note with a title, a body and optional tags.")  # MODIFIED
        self.assertEqual(self.rec(notes + "/delete-note")["sections"]["scenarios"][0]["steps"],
                         [["Given", "a stored note"], ["When", "its owner deletes it"], ["Then", "reading it answers 404"]])
        size = self.rec(notes + "/note-size-limit")
        self.assertEqual(size["sections"]["statement"], "The system shall limit a note body to 100 KB.")
        self.assertIn("Larger bodies are refused with 413.", size["sections"]["notes"])
        fuzzy = self.rec("openspec/search/fuzzy-matching")
        self.assertEqual(fuzzy["status"], "deprecated")  # REMOVED: deprecated, never deleted
        self.assertIn("Too many false hits", fuzzy["sections"]["notes"])
        self.assertIn("Renamed from 'Full-text search'", self.rec("openspec/search/word-search")["sections"]["notes"])
        self.assertEqual(self.rec("openspec/changes/add-note-tags/t2-2")["needs"],
                         ["openspec/changes/add-note-tags/t2-1"])
        self.assertIn("2.3 Announce the change to users",
                      self.rec("openspec/changes/add-note-tags")["sections"]["notes"])
        self.assertIn("skipped 1 archived change(s)", out.stdout)
        self.assertIn("Ranking notes", self.rec("openspec/search")["sections"]["design"])

    def test_deterministic(self):
        self.assert_deterministic()


class UnitTest(unittest.TestCase):
    def test_to_ears(self):
        cases = [
            ("System MUST allow users to create an album.", "The system shall allow users to create an album."),
            ("Users MUST be able to reset their password", "The system shall let users reset their password."),
            ("WHEN a user saves THEN the system SHALL store the draft",
             "When a user saves, the system shall store the draft."),
            ("WHEN a user saves THE SYSTEM SHALL store the draft", "When a user saves, the system shall store the draft."),
            ("IF the token is revoked THEN the API SHALL answer 401", "If the token is revoked, then the API shall"
                                                                       " answer 401."),
            ("WHILE offline WHEN the user saves THE SYSTEM SHALL queue the change",
             "While offline, when the user saves, the system shall queue the change."),
            ("WHERE SSO is enabled THE SYSTEM SHALL show the SSO button",
             "Where SSO is enabled, the system shall show the SSO button."),
            ("The service SHALL NOT log tokens.", "The service shall not log tokens."),
            ("WHEN a user deletes a note, the system SHALL remove it.", "When a user deletes a note, the system shall"
                                                                        " remove it."),
        ]
        for src, want in cases:
            with self.subTest(src=src):
                got, ok = to_ears(src)
                self.assertTrue(ok, got)
                self.assertEqual(got, want)
                self.assertIsNotNone(ears_class(got))
        for src in ("The system should be fast.", "System MUST do A and MUST do B.",
                    "System MUST notify via [NEEDS CLARIFICATION: channel?]", "Make it nice"):
            with self.subTest(src=src):
                got, ok = to_ears(src)
                self.assertFalse(ok)
                self.assertEqual(got, src)

    def test_find_paths(self):
        self.assertEqual(find_paths("Create the model in src/models/user.py and tests/test_user.py"),
                         ["src/models/user.py", "tests/test_user.py"])
        self.assertEqual(find_paths("Add `ReminderStore.add` to `src/app` (see https://x.example/a.py)"), ["src/app"])
        self.assertEqual(find_paths("Update docs/ and README.md, e.g. the intro, read/write"), ["docs/**", "README.md"])
        self.assertEqual(find_paths("Configure ./src//cfg.toml, not ../out.py or /etc/x.conf"), ["src/cfg.toml"])
        self.assertEqual(find_paths("Add logging for album operations"), [])

    def test_bad_into(self):
        for cap in ("Bad", "tasks", "x" * 21, ""):
            with self.assertRaises(importers.ImportFailed):
                importers.build("kiro", os.path.join(SDD, "kiro"), os.path.join(SDD, "kiro"), cap)

    def test_missing_source(self):
        with self.assertRaises(importers.ImportFailed):
            importers.build("openspec", os.path.join(SDD, "kiro"), os.path.join(SDD, "kiro"), "openspec")


if __name__ == "__main__":
    unittest.main()
