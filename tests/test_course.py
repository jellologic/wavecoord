"""The course tooling (scripts/course/): the lesson format, the build's pure parts, the demos, the forge.

No model, no recording, no TTS: the lessons are checked against the rig's format, the cuts and subtitles
are pure functions, the demo repos and the forge run against temp folders, and one wave of the brownfield
demo runs end to end with the fake claude and the course forge standing in for GitHub."""

import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET

import _paths

COURSE = os.path.join(_paths.ROOT, "scripts", "course")
PY = sys.executable


def load(name, rel):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(COURSE, rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rig = load("course_rig", "rig.py")
demos = load("course_demos", os.path.join("demos", "demos.py"))
build = load("course_build", "build.py")
forge = load("course_forge", os.path.join("demos", "forge.py"))
wait = load("course_wait", "wait.py")


def git(args, cwd, check=True):
    p = subprocess.run(["git"] + args, cwd=cwd, env=demos.git_env(), stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True)
    if check and p.returncode != 0:
        raise AssertionError("git %s: %s" % (" ".join(args), p.stderr))
    return p.stdout.strip()


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wc-course-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


# ---------------------------------------------------------------- the lesson format

class ScreenTest(unittest.TestCase):
    def test_cursor_moves_and_erases(self):
        sc = rig.Screen(30, 4)
        sc.feed("hello\r\x1b[1B/wave\x1b[1mco")
        sc.feed("ord\x1b[0m:spec-im\xe2\x80"[:-2] + "port\x1b[1;1H\x1b[2KHI\x1b[3;3Hx")
        self.assertEqual(sc.text().splitlines(), ["HI", "/wavecoord:spec-import", "  x"])

    def test_split_escape_and_scroll(self):
        sc = rig.Screen(10, 2)
        sc.feed("a\r\nb\x1b[")
        sc.feed("2Kc\r\nd")
        self.assertEqual(sc.text().splitlines(), [" c", "d"])

    def test_replies_only(self):
        for t in ("\x1b[31;5R", "\x1b[?62;22c", "\x1b[?1u", "\x1bP>|Terminal\x1b\\", "\x1b]11;rgb:0/0/0\x07"):
            self.assertTrue(rig._REPLY.fullmatch(t), repr(t))
        for t in ("\x1b[<35;10;5M", "x", "\x1b[A"):
            self.assertFalse(rig._REPLY.search(t), repr(t))


class RigFormatTest(unittest.TestCase):
    def test_proof_take_is_valid(self):
        with open(os.path.join(COURSE, "proof-take.json")) as f:
            self.assertEqual(rig.check_lesson(json.load(f)), [])

    def test_problems_are_found(self):
        lesson = {"command": ["claude"], "steps": [
            {"say": "hi", "wait": "no"}, {"type": "x", "enter": True, "idle": 2}, {"key": "f13"},
            {"wait_for": "(", "timeout": 5}, {"nothing": 1}, {"pause": -1}, {"wait_exit": 5}],
            "setup": [{"say": "too early"}], "config": {"colour": 1}, "extra": 1}
        errs = "\n".join(rig.check_lesson(lesson))
        for needle in ("wait must be true or false", "type step: unknown key 'idle'", "unknown key 'f13'",
                       "not a valid regex", "no action", "pause must be a number", "narration runs only in steps",
                       "config: unknown key 'colour'", "unknown key 'extra'"):
            self.assertIn(needle, errs)

    def test_unknown_keys_are_fine_without_strict_lists(self):
        lesson = {"steps": [{"say": "hi", "cut": True}], "slug": "x"}
        self.assertEqual(rig.check_lesson(lesson, extra_keys=None, extra_step_keys=None), [])
        self.assertTrue(rig.check_lesson(lesson))

    def test_step_action_follows_run_step_order(self):
        self.assertEqual(rig.step_action({"wait_for": "x", "idle": 2}), "wait_for")
        self.assertEqual(rig.step_action({"idle": 2}), "idle")
        self.assertEqual(rig.step_action({"wait_shell": "true"}), "wait_shell")

    def test_usage_prices_each_model(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        proj = os.path.join(tmp, "projects", "-tmp-demo")
        os.makedirs(proj)
        rows = [{"type": "assistant", "message": {"id": "a", "model": "claude-sonnet-5-5", "usage": {
            "input_tokens": 1000000, "output_tokens": 0}}},
                {"type": "assistant", "message": {"id": "a", "model": "claude-sonnet-5-5", "usage": {
                    "input_tokens": 1000000, "output_tokens": 0}}},
                {"type": "assistant", "message": {"id": "b", "model": "claude-haiku-5-5", "usage": {
                    "input_tokens": 0, "output_tokens": 1000000}}}]
        with open(os.path.join(proj, "s.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
        u = rig.usage_of(rig.transcripts(os.path.join(tmp, "projects"), 0))
        self.assertEqual(u["requests"], 2)
        self.assertEqual(u["usd_at_api_list_price"], 2.5)
        self.assertEqual(rig.claude_usage("/tmp/demo", 0, {"CLAUDE_CONFIG_DIR": tmp})["requests"], 2)


class LessonsTest(unittest.TestCase):
    def test_every_lesson_is_valid(self):
        files = build.lesson_files()
        self.assertEqual([build.lesson_id(p)[:2] for p in files], ["%02d" % n for n in range(11)])
        for p in files:
            self.assertEqual(build.validate(p), [], p)

    def test_resolved_lessons_name_the_course_paths(self):
        for p in build.lesson_files():
            with open(p) as f:
                lesson = json.load(f)
            out, missing = build.resolve(lesson, home="/x/course", port=18999)
            self.assertEqual(missing, set())
            text = json.dumps(out)
            self.assertNotIn("${", text)
            self.assertEqual(out["env"]["CLAUDE_CONFIG_DIR"], "/x/course/claude")
            self.assertEqual(out["env"]["WAVECOORD_HOME"], "/x/course/wc")
            self.assertTrue(out["cwd"].startswith(build.DEMO_ROOT + "/"))
            self.assertNotIn("18741", text)
            if lesson["plugin"] == "dir" and out["command"][0] == "claude":
                self.assertIn(os.path.join(_paths.ROOT, "plugin"), out["command"])

    def test_each_lesson_has_a_page_with_its_video(self):
        for p in build.lesson_files():
            lid = build.lesson_id(p)
            page = os.path.join(_paths.ROOT, "docs", "course", lid + ".md")
            self.assertTrue(os.path.isfile(page), page)
            with open(page) as f:
                text = f.read()
            self.assertIn(build.VIDEO_URL % lid, text)
            for part in ("## Goals", "## Why", "## Steps", "## Common mistakes", "## Exercise"):
                self.assertIn(part, text, "%s: %s" % (lid, part))
        with open(os.path.join(_paths.ROOT, "docs", "course", "README.md")) as f:
            index = f.read()
        for p in build.lesson_files():
            self.assertIn(build.lesson_id(p) + ".md", index)

    def test_typed_skills_exist(self):
        for p in build.lesson_files():
            with open(p) as f:
                for st in json.load(f)["steps"]:
                    m = re.match(r"^/wavecoord:([a-z-]+)", st.get("type", ""))
                    if m:
                        self.assertIn(m.group(1), build.skill_names())

    def test_validate_finds_problems(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        bad = {"slug": "99-other", "demo": {"name": "nope"}, "plugin": "x", "command": ["claude"],
               "steps": [{"type": "/wavecoord:nosuch", "enter": True}, {"wait_for": "nosuch", "idle": 1},
                         {"say": "x", "cut": True}], "estimate": {}}
        path = os.path.join(tmp, "99-bad.json")
        with open(path, "w") as f:
            json.dump(bad, f)
        errs = "\n".join(build.validate(path))
        for needle in ("slug must be", "demo.name", "plugin must be", "no skill nosuch", "matches the text typed",
                       "give every wait a timeout", "cut is only for a wait", "last step must be wait_exit",
                       "folder-trust", "estimate.video_min"):
            self.assertIn(needle, errs)


class SubstituteTest(unittest.TestCase):
    def test_nested_and_missing(self):
        missing = set()
        out = build.substitute({"a": ["${X} and ${Y}", 3], "b": "${X}"}, {"X": "1"}, missing)
        self.assertEqual(out, {"a": ["1 and ${Y}", 3], "b": "1"})
        self.assertEqual(missing, {"Y"})


# ---------------------------------------------------------------- cuts, subtitles, cards

class CutsTest(unittest.TestCase):
    STEPS = [{"say": "a"}, {"wait_for": "x", "timeout": 600, "cut": True}, {"say": "b"},
             {"idle": 5, "timeout": 900, "cut": True}, {"wait_for": "y", "timeout": 600}]

    def rows(self):
        return [{"i": 0, "kind": "say", "at": 0.0, "end": 3.0}, {"i": 1, "kind": "wait_for", "at": 3.0, "end": 200.0},
                {"i": 2, "kind": "say", "at": 200.0, "end": 204.0}, {"i": 3, "kind": "idle", "at": 204.0,
                                                                     "end": 210.0},
                {"i": 4, "kind": "wait_for", "at": 210.0, "end": 400.0}, {"i": -1, "kind": "wait_for", "at": 0,
                                                                          "end": 1}]

    def test_cuts_skip_voice_and_short_waits(self):
        # a narration line plays into the wait (wait: false): the cut starts after it
        cuts = build.plan_cuts(self.rows(), self.STEPS, [(0.0, 3.0), (2.5, 6.0), (200.0, 3.5)])
        self.assertEqual(cuts, [(9.0, 198.0)])

    def test_voice_inside_a_wait_ends_the_cut_before_it(self):
        cuts = build.plan_cuts(self.rows(), self.STEPS, [(100.0, 2.0)])
        self.assertEqual(cuts, [(5.5, 99.5)])

    def test_pieces_and_time_map(self):
        parts = build.pieces(300.0, [(10.0, 110.0), (150.0, 250.0)])
        self.assertEqual([p[0] for p in parts], ["rig", "later", "rig", "later", "rig"])
        self.assertEqual(parts[1][1], 100.0)
        self.assertEqual(build.map_time(5.0, parts), 5.0)
        self.assertIsNone(build.map_time(50.0, parts))
        self.assertEqual(build.map_time(120.0, parts), 10.0 + build.LATER_S + 10.0)
        self.assertEqual(build.pieces_duration(parts), round(10 + 40 + 50 + 2 * build.LATER_S, 3))
        self.assertEqual(build.pieces(20.0, []), [("rig", 0.0, 20.0, 0.0)])

    def test_shift_cues(self):
        parts = build.pieces(300.0, [(10.0, 110.0)])
        cues = [(1.0, 2.0, "a"), (50.0, 51.0, "gone"), (120.0, 125.0, "b")]
        self.assertEqual(build.shift_cues(cues, parts, 30.0),
                         [(31.0, 32.0, "a"), (round(30 + 10 + build.LATER_S + 10, 3),
                                              round(30 + 10 + build.LATER_S + 15, 3), "b")])

    def test_later_text(self):
        self.assertEqual(build.later_text(40), "40 seconds later")
        self.assertEqual(build.later_text(70), "70 seconds later")
        self.assertEqual(build.later_text(120), "2 minutes later")
        self.assertEqual(build.later_text(400), "7 minutes later")


class SrtTest(unittest.TestCase):
    def test_roundtrip(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cues = [(0.6, 2.25, "Hello."), (3725.5, 3727.0, "two\nlines")]
        path = os.path.join(tmp, "x.srt")
        build.write_srt(cues, path)
        with open(path) as f:
            text = f.read()
        self.assertIn("01:02:05,500 --> 01:02:07,000", text)
        self.assertEqual(build.parse_srt(text), cues)


class CardsTest(unittest.TestCase):
    def test_every_card_is_svg(self):
        for p in build.lesson_files():
            with open(p) as f:
                lesson = json.load(f)
            for part in ("intro", "outro"):
                for card in lesson["cards"][part]:
                    root = ET.fromstring(build.card_svg(card, lesson["title"]))
                    self.assertEqual(root.get("width"), "1920")
                    texts = " ".join(t.text or "" for t in root.iter("{http://www.w3.org/2000/svg}text"))
                    self.assertIn(card["title"][:20], texts)

    def test_every_diagram_draws_and_has_mermaid(self):
        for name, d in build.DIAGRAMS.items():
            ids = [n for row in d["rows"] for n, _ in row]
            for a, b, _ in d["edges"]:
                self.assertIn(a, ids)
                self.assertIn(b, ids)
            self.assertTrue(d["mermaid"].startswith("flowchart"))
            ET.fromstring(build.card_svg({"title": name, "diagram": name, "say": "x"}))
            ET.fromstring(build.later_svg(build.later_text(300)))

    def test_image_card(self):
        root = ET.fromstring(build.card_svg({"title": "t", "diagram": "tree", "say": "x"},
                                            image="data:image/png;base64,AAAA"))
        self.assertEqual(len(list(root.iter("{http://www.w3.org/2000/svg}image"))), 1)


# ---------------------------------------------------------------- demos

class DemosTest(TempDir):
    def demo(self, name, state=None):
        repo = os.path.join(self.tmp, "wavecoord-demo", name + (state or ""))
        return demos.build(name, repo, state=state, allow_any_parent=True)

    def wc(self, *args, cwd=None):
        return subprocess.run([PY, demos.WAVECOORD] + list(args), cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True)

    def test_each_demo_builds_with_passing_tests_and_a_pushed_main(self):
        for name in sorted(demos.DEMOS):
            p = self.demo(name)
            self.assertEqual(git(["rev-parse", "HEAD"], p["repo"]), git(["rev-parse", "main"], p["origin"]))
            self.assertEqual(git(["status", "--porcelain"], p["repo"]), "")
            r = subprocess.run([PY, "-m", "unittest", "discover", "-s", "tests"], cwd=p["repo"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(r.returncode, 0, name + r.stderr)
            with open(os.path.join(p["repo"], ".claude", "wavecoord.json")) as f:
                cfg = json.load(f)
            self.assertEqual(cfg["coord_url"], "http://127.0.0.1:18793")
            self.assertEqual(cfg["project"], p["project"])

    def test_the_same_demo_builds_the_same_commits(self):
        a = git(["rev-parse", "HEAD"], self.demo("brownfield", "planned")["repo"])
        b = git(["rev-parse", "HEAD"], demos.build("brownfield", os.path.join(self.tmp, "wavecoord-demo", "x"),
                                                   state="planned", allow_any_parent=True)["repo"])
        self.assertEqual(a, b)

    def test_brownfield_states(self):
        planned = self.demo("brownfield", "planned")["repo"]
        r = self.wc("spec", "check", "--repo", planned)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("0 errors, 0 warnings", r.stdout)
        waves = self.wc("spec", "waves", "--repo", planned).stdout
        self.assertIn("wave 1 (2):  todo/due-dates/overdue/dates, todo/due-dates/store-due/store", waves)
        flawed = self.demo("brownfield", "flawed")["repo"]
        r = self.wc("spec", "check", "--repo", flawed)
        self.assertEqual(r.returncode, 1)
        self.assertIn("error P", r.stdout)
        self.assertIn("suspect", r.stdout)
        triage = self.demo("brownfield", "triage")["repo"]
        with open(os.path.join(triage, ".claude", "wavecoord.json")) as f:
            self.assertIn("protected_paths_cmd", json.load(f))
        with open(os.path.join(triage, "specs", "todo", "due-dates", "overdue", "tasks", "dates.md")) as f:
            self.assertIn("CHANGELOG.md", f.read())
        plain = self.demo("brownfield")["repo"]
        self.assertFalse(os.path.exists(os.path.join(plain, "specs")))

    def test_fresh_without_config(self):
        p = demos.build("fresh", os.path.join(self.tmp, "wavecoord-demo", "f"), with_config=False,
                        allow_any_parent=True)
        self.assertFalse(os.path.exists(os.path.join(p["repo"], ".claude")))

    def test_imports_of_the_sdd_demos(self):
        for name, cap in (("kiro", "kiro"), ("openspec", "openspec")):
            repo = self.demo(name)["repo"]
            r = self.wc("spec", "import", "--from", name, "--dry-run", "--repo", repo)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("import from %s into specs/%s" % (name, cap), r.stderr)
        repo = self.demo("speckit")["repo"]
        r = self.wc("spec", "import", "--from", "speckit", "--dry-run", "--repo", repo)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("inside the spec dir", r.stderr)  # the folder clash lesson 5 shows
        git(["mv", "specs", "speckit"], repo)
        r = self.wc("spec", "import", "--from", "speckit", "--src", "speckit", "--into", "recipes", "--dry-run",
                    cwd=repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("5 requirements, 5 tasks, 1 decision", r.stderr)

    def test_refuses_to_wipe_outside_a_demo_folder(self):
        with self.assertRaises(demos.DemoError):
            demos.build("fresh", os.path.join(self.tmp, "elsewhere", "x"))
        with self.assertRaises(demos.DemoError):
            demos.build("brownfield", os.path.join(self.tmp, "wavecoord-demo", "x"), state="nope")


# ---------------------------------------------------------------- the forge

class ForgeTest(TempDir):
    def setUp(self):
        super().setUp()
        self.p = demos.build("fresh", os.path.join(self.tmp, "wavecoord-demo", "repo"), allow_any_parent=True)
        self.repo = self.p["repo"]

    def gh(self, *args, cwd=None):
        p = subprocess.run([os.path.join(COURSE, "demos", "bin", "gh")] + list(args), cwd=cwd or self.repo,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        return p.returncode, p.stdout, p.stderr

    def branch(self, name, path, text):
        git(["checkout", "-q", "-b", name, "main"], self.repo)
        with open(os.path.join(self.repo, path), "w") as f:
            f.write(text)
        git(["add", "-A"], self.repo)
        git(["commit", "-q", "-m", "change " + path], self.repo)
        git(["push", "-q", "origin", name], self.repo)
        git(["checkout", "-q", "main"], self.repo)
        return git(["rev-parse", name], self.repo)

    def test_a_pr_from_create_to_merge(self):
        self.assertEqual(self.gh("auth", "status")[0], 0)
        self.assertIn("stand-in for GitHub", self.gh("--version")[1])
        sha = self.branch("feature/a", "a.txt", "a\n")
        body = os.path.join(self.tmp, "body.md")
        with open(body, "w") as f:
            f.write("Task: a\n")
        rc, out, _ = self.gh("pr", "create", "--head", "feature/a", "--base", "main", "--title", "Add a",
                             "--body-file", body)
        self.assertEqual((rc, out.strip()), (0, "http://forge.localhost/repo/pull/1"))
        rc, _, err = self.gh("pr", "create", "--head", "feature/a", "--base", "main", "--title", "again")
        self.assertEqual(rc, 1)
        self.assertIn("already exists", err)
        view = json.loads(self.gh("pr", "view", "feature/a", "--json", "state,headRefOid,body")[1])
        self.assertEqual(view, {"state": "OPEN", "headRefOid": sha, "body": "Task: a\n"})
        self.assertIn("a.txt", self.gh("pr", "view", "1")[1])
        self.assertIn("+a", self.gh("pr", "diff", "1")[1])
        with open(body, "w") as f:
            f.write("new body\n")
        self.gh("pr", "edit", "http://forge.localhost/repo/pull/1", "--body-file", body)
        self.assertIn("#1", self.gh("pr", "list")[1])
        rc, _, err = self.gh("pr", "merge", "1", "--merge", "--match-head-commit", "0" * 40)
        self.assertEqual(rc, 1)
        self.assertIn("not 000000000000", err)
        rc, out, _ = self.gh("pr", "merge", "1", "--merge", "--match-head-commit", sha)
        self.assertEqual(rc, 0)
        self.assertIn("Merged pull request #1", out)
        self.assertEqual(git(["show", "main:a.txt"], self.p["origin"]), "a")
        view = json.loads(self.gh("pr", "view", "1", "--json", "state,mergedAt,body,headRefOid")[1])
        self.assertEqual((view["state"], view["body"], view["headRefOid"]), ("MERGED", "new body\n", sha))
        self.assertTrue(view["mergedAt"])
        self.assertEqual(self.gh("pr", "merge", "1")[0], 1)
        self.assertIn("no open pull requests", self.gh("pr", "list")[1])

    def test_conflict_close_and_errors(self):
        self.branch("x", "same.txt", "x\n")
        self.branch("y", "same.txt", "y\n")
        for b in ("x", "y"):
            self.gh("pr", "create", "--head", b, "--base", "main", "--title", b, "--body", "")
        self.assertEqual(self.gh("pr", "merge", "1", "--merge")[0], 0)
        rc, _, err = self.gh("pr", "merge", "2", "--merge")
        self.assertEqual(rc, 1)
        self.assertIn("merge conflict", err)
        self.assertIn("Closed", self.gh("pr", "close", "2")[1])
        self.assertEqual(json.loads(self.gh("pr", "view", "2", "--json", "state")[1]), {"state": "CLOSED"})
        self.assertEqual(self.gh("pr", "view", "9")[0], 1)
        self.assertEqual(self.gh("pr", "create", "--head", "nope", "--base", "main")[0], 1)
        self.assertEqual(self.gh("issue", "list")[0], 2)
        self.assertEqual(self.gh("pr", "view", cwd=self.tmp)[0], 2)


class WaitTest(unittest.TestCase):
    def test_conditions(self):
        a = [{"state": "running"}, {"state": "reported"}]
        self.assertFalse(wait.holds("idle", 1, attempts=a))
        self.assertTrue(wait.holds("running", 1, attempts=a))
        self.assertTrue(wait.holds("idle", 2, attempts=[{"state": "reported"}, {"state": "failed"}]))
        self.assertFalse(wait.holds("idle", 3, attempts=[{"state": "reported"}, {"state": "failed"}]))
        self.assertFalse(wait.holds("idle", 1, attempts=[]))
        self.assertTrue(wait.holds("done", 2, done=2))


# ---------------------------------------------------------------- one wave through the course forge

class ForgeWaveE2E(TempDir):
    """The brownfield demo's wave 1 with the fake claude: dispatch, collect (PRs on the course forge), and
    the refinery merging both through `gh pr merge` of the forge, Verify and check_cmd included."""

    MODES = {
        "todo.due-dates.store-due.store": {"commit": {
            "tests/test_due_store.py": "import unittest\n\n\nclass T(unittest.TestCase):\n"
                                       "    def test_ok(self):\n        self.assertTrue(True)\n"}},
        "todo.due-dates.overdue.dates": {"commit": {
            "todo/dates.py": "def is_overdue(item, today):\n    return False\n",
            "tests/test_dates.py": "import unittest\n\nfrom todo.dates import is_overdue\n\n\n"
                                   "class T(unittest.TestCase):\n    def test_open(self):\n"
                                   "        self.assertFalse(is_overdue({}, None))\n"}},
    }

    def setUp(self):
        super().setUp()
        t = self.tmp
        home = os.path.join(t, "home")
        os.makedirs(os.path.join(home, ".claude"))
        with open(os.path.join(home, ".claude", ".credentials.json"), "w") as f:
            f.write("{}\n")
        gitcfg = os.path.join(t, "gitconfig")
        with open(gitcfg, "w") as f:
            f.write("[user]\n\tname = e2e\n\temail = e2e@example.invalid\n[commit]\n\tgpgsign = false\n")
        self.p = demos.build("brownfield", os.path.join(t, "wavecoord-demo", "todo"), state="planned",
                             allow_any_parent=True)
        self.repo = self.p["repo"]
        modes = os.path.join(t, "modes.json")
        with open(modes, "w") as f:
            json.dump(self.MODES, f)
        wch = os.path.join(t, "wc")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("WAVECOORD_", "CLAUDE", "ANTHROPIC_", "FAKE_", "GIT_"))}
        env.update({
            "HOME": home, "GIT_CONFIG_GLOBAL": gitcfg, "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": os.pathsep.join([os.path.join(_paths.TESTS, "fake-claude"), os.path.join(COURSE, "demos", "bin"),
                                     os.environ.get("PATH", "")]),
            "WAVECOORD_HOME": wch, "WAVECOORD_TOKENS_FILE": os.path.join(wch, "tokens.json"),
            "WAVECOORD_TOKEN_FILE": os.path.join(wch, "token"),
            "WAVECOORD_HB_INTERVAL_S": "0.5", "WAVECOORD_POLL_S": "0.2", "WAVECOORD_PLAN_FETCH_S": "0",
            "WAVECOORD_PUSH_RETRY_S": "1", "WAVECOORD_WATCHDOG_S": "0", "FAKE_CLAUDE_MODES": modes,
        })
        self.env = env
        self.wc("tokens", "provision", "--role", "lead", "--out", env["WAVECOORD_TOKEN_FILE"])
        self.wc("tokens", "provision", "--role", "agent", "--host", "local", "--out",
                os.path.join(wch, "hosts", "local.token"))
        self.srv = subprocess.Popen([PY, demos.WAVECOORD, "serve", "--repo", self.repo, "--home",
                                     os.path.join(wch, "server"), "--port", "0"], cwd=self.repo, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
        self.addCleanup(self.stop)
        line = self.srv.stdout.readline()
        self.assertTrue(line.startswith("wavecoord serve: http://"), line)
        self.env["WAVECOORD_URL"] = line.split()[2]

    def stop(self):
        if self.srv.poll() is None:
            self.srv.send_signal(signal.SIGTERM)
            try:
                self.srv.wait(20)
            except subprocess.TimeoutExpired:
                self.srv.kill()
        self.srv.stdout.close()

    def wc(self, *args):
        p = subprocess.run([PY, demos.WAVECOORD] + list(args), cwd=self.repo, env=self.env, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=300)
        if p.returncode != 0:
            raise AssertionError("wavecoord %s: %s%s" % (" ".join(args), p.stdout, p.stderr))
        return p.stdout

    def test_wave_one_merges_through_the_forge(self):
        out = json.loads(self.wc("dispatch", "--json"))
        self.assertEqual(sorted(d["task"] for d in out["dispatched"]),
                         ["todo.due-dates.overdue.dates", "todo.due-dates.store-due.store"])
        wenv = dict(self.env, WAVECOORD_BIN=demos.WAVECOORD)
        end = time.time() + 120
        while time.time() < end:
            if subprocess.run([PY, os.path.join(COURSE, "wait.py"), "idle", "--min", "2"], cwd=self.repo, env=wenv,
                              stdout=subprocess.DEVNULL).returncode == 0:
                break
            time.sleep(1)
        else:
            self.fail("the fake workers did not finish")
        self.wc("collect")
        st = forge.load(self.p["forge"])
        self.assertEqual(sorted(p["head"] for p in st["prs"]),
                         ["wave/todo.due-dates.overdue.dates-1", "wave/todo.due-dates.store-due.store-1"])
        res = json.loads(self.wc("refinery", "merge", "--json"))
        self.assertEqual(sorted(r["state"] for r in res["results"]), ["merged", "merged"], res)
        st = forge.load(self.p["forge"])
        self.assertEqual([p["state"] for p in st["prs"]], ["merged", "merged"])
        self.assertIn("is_overdue", git(["show", "main:todo/dates.py"], self.p["origin"]))
        r = subprocess.run([PY, os.path.join(COURSE, "wait.py"), "done", "--min", "2"], cwd=self.repo, env=wenv,
                           stdout=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == "__main__":
    unittest.main()
