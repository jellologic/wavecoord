#!/usr/bin/env python3
"""Demo projects for the wavecoord course: each one a small git repo with a local bare origin.

    python3 scripts/course/demos/demos.py fresh /tmp/wavecoord-demo/bookmarks
    python3 scripts/course/demos/demos.py brownfield /tmp/wavecoord-demo/todo --state planned
    python3 scripts/course/demos/demos.py --list

| Demo | Project | What it is |
|---|---|---|
| fresh | bookmarks | An empty repo with a README that describes a tiny bookmarks CLI |
| brownfield | todo | A small stdlib Python to-do CLI with tests and no specs |
| speckit | recipes | A recipe tool with a GitHub Spec Kit feature (.specify/, specs/001-...) |
| kiro | habits | A habit tracker with an Amazon Kiro spec (.kiro/specs, .kiro/steering) |
| openspec | glossary | A glossary tool with OpenSpec specs and one open change |

States of `brownfield` (the spec tree is merged on main, as after a plan PR):

- `plain`: no specs (the default);
- `planned`: the feature todo/due-dates with two approved requirements and three planned tasks in two waves;
- `watch`: `planned` with sonnet workers, so they still run while the lesson steers them;
- `flawed`: `planned` with mistakes to fix: overlapping paths, a weak Acceptance, a vague draft requirement,
  and a requirement changed after planning (a suspect task);
- `triage`: `planned`, plus a CHANGELOG.md that the two wave-1 tasks share and that is a protected path:
  both PRs wait for your yes, and the second one conflicts after the first merges.

The origin is `<parent>/origin/<name>.git`; the course forge keeps its PRs next to it. Every build wipes
the repo, the origin, the forge state and the worktrees folder `<parent>/wt/<project>` first, and only
under a parent folder named `wavecoord-demo*` (or with allow_any_parent in tests). Commits use a fixed
author and date, so a demo builds the same each time. Stdlib only.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
WAVECOORD = os.path.join(ROOT, "plugin", "bin", "wavecoord")
DEFAULT_PORT = 18793
GIT_DATE = "2026-10-01T09:00:00+00:00"
WORKER_TOOLS = ("Bash(git add *),Bash(git commit *),Bash(git status *),Bash(git diff *),Bash(git log *),"
                "Bash(python3 *)")
HAIKU = {"model": "haiku", "effort": "low", "max_turns": 30, "budget_usd": 1, "timeout_min": 20}
# workers that are still running a minute later, for the lesson that steers them
SONNET = {"model": "sonnet", "effort": "medium", "max_turns": 30, "budget_usd": 2, "timeout_min": 20}
CHECK_CMD = "python3 -m unittest discover -s tests"


class DemoError(Exception):
    pass


# ---------------------------------------------------------------- git and files

def git_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="Course Demo", GIT_AUTHOR_EMAIL="demo@example.invalid",
               GIT_COMMITTER_NAME="Course Demo", GIT_COMMITTER_EMAIL="demo@example.invalid",
               GIT_AUTHOR_DATE=GIT_DATE, GIT_COMMITTER_DATE=GIT_DATE)
    return env


def run(args, cwd, input=None):
    p = subprocess.run(args, cwd=cwd, env=git_env(), input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True)
    if p.returncode != 0:
        raise DemoError("%s: exit %d\n%s%s" % (" ".join(args), p.returncode, p.stdout[-1500:], p.stderr[-1500:]))
    return p.stdout


def git(repo, *args):
    return run(["git"] + list(args), repo)


def write(repo, files):
    for rel, text in sorted(files.items()):
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text.lstrip("\n") if rel.endswith(".md") else text)


def commit(repo, files, message):
    write(repo, files)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def config(project, port, slots=2, **extra):
    cfg = {
        "coord_url": "http://127.0.0.1:%d" % port,
        "project": project,
        "hosts": [{"name": "local", "slots": slots, "repo_path": ".",
                   "claude_flags": ["--permission-mode", "acceptEdits", "--allowedTools", WORKER_TOOLS]}],
        "wip_cap": slots,
        "fence": "advisory",
        "check_cmd": CHECK_CMD,
    }
    cfg.update(extra)
    return {".claude/wavecoord.json": json.dumps(cfg, indent=2) + "\n"}


GITIGNORE = {".gitignore": "__pycache__/\n*.pyc\n.claude/settings.local.json\n"}


def spec_apply(repo, ops, message):
    """Write spec records through the op API (`wavecoord spec apply`), then commit them."""
    run([sys.executable, WAVECOORD, "spec", "apply", "-"], repo, input=json.dumps({"ops": ops}))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def edit(repo, rel, old, new, count=1):
    path = os.path.join(repo, rel)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if old not in text:
        raise DemoError("%s: %r not found" % (rel, old))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text.replace(old, new, count))


# ---------------------------------------------------------------- fresh: bookmarks

FRESH_README = """
# bookmarks

A tiny command-line tool to keep web bookmarks. Nothing is built yet: this repo is the starting point of the
wavecoord course.

## The idea

- `python3 -m bookmarks add <url> --title <title>` saves a bookmark in a JSON file.
- `python3 -m bookmarks list` lists the bookmarks, newest first.
- `python3 -m bookmarks find <word>` lists the bookmarks whose title or URL contains the word.
- The file is `~/.bookmarks.json`, or the path in `BOOKMARKS_FILE`.

## Rules

- Python 3.9 or later, standard library only.
- Tests live in `tests/` and run with `python3 -m unittest discover -s tests`.
"""

FRESH_TEST = '''"""A placeholder test, so the test command has something to run in an empty project."""

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ReadmeTest(unittest.TestCase):
    def test_readme_exists(self):
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "README.md")))


if __name__ == "__main__":
    unittest.main()
'''


def build_fresh(repo, port, state, with_config):
    files = {"README.md": FRESH_README, "tests/test_readme.py": FRESH_TEST}
    files.update(GITIGNORE)
    commit(repo, files, "Describe the bookmarks idea")
    if with_config:
        commit(repo, config("bookmarks", port, slots=3), "Add the wavecoord config")


# ---------------------------------------------------------------- brownfield: todo

TODO_STORE = '''"""A tiny JSON file store for to-do items."""

import json
import os


class Store(object):
    """The items live in one JSON file: a list of {"id", "text", "done"}."""

    def __init__(self, path):
        self.path = path

    def load(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def save(self, items):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=1)
        os.replace(tmp, self.path)

    def add(self, text):
        items = self.load()
        item = {"id": max([i["id"] for i in items], default=0) + 1, "text": text, "done": False}
        items.append(item)
        self.save(items)
        return item

    def done(self, item_id):
        items = self.load()
        for item in items:
            if item["id"] == item_id:
                item["done"] = True
                self.save(items)
                return item
        raise KeyError(item_id)

    def open_items(self):
        return [i for i in self.load() if not i["done"]]
'''

TODO_CLI = '''"""The todo command line: add, list and done."""

import argparse
import os
import sys

from todo.store import Store

DEFAULT_FILE = os.path.join(os.path.expanduser("~"), ".todo.json")


def main(argv=None, out=None):
    out = out or sys.stdout
    p = argparse.ArgumentParser(prog="todo")
    p.add_argument("--file", default=os.environ.get("TODO_FILE", DEFAULT_FILE))
    sub = p.add_subparsers(dest="cmd")
    sub.required = True
    a = sub.add_parser("add", help="add an item")
    a.add_argument("text")
    ls = sub.add_parser("list", help="list the open items")
    ls.add_argument("--all", action="store_true", help="also the done ones")
    d = sub.add_parser("done", help="mark an item done")
    d.add_argument("id", type=int)
    args = p.parse_args(argv)
    store = Store(args.file)
    if args.cmd == "add":
        item = store.add(args.text)
        out.write("added %d: %s\\n" % (item["id"], item["text"]))
    elif args.cmd == "list":
        for item in (store.load() if args.all else store.open_items()):
            out.write("%s %d  %s\\n" % ("x" if item["done"] else "-", item["id"], item["text"]))
    else:
        try:
            item = store.done(args.id)
        except KeyError:
            out.write("no item %d\\n" % args.id)
            return 1
        out.write("done %d: %s\\n" % (item["id"], item["text"]))
    return 0
'''

TODO_MAIN = '''import sys

from todo.cli import main

sys.exit(main())
'''

TODO_TEST_STORE = '''import os
import tempfile
import unittest

from todo.store import Store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "todo.json"))

    def test_add_numbers_items(self):
        self.assertEqual(self.store.add("milk")["id"], 1)
        self.assertEqual(self.store.add("bread")["id"], 2)

    def test_done_hides_an_item(self):
        self.store.add("milk")
        self.store.add("bread")
        self.store.done(1)
        self.assertEqual([i["text"] for i in self.store.open_items()], ["bread"])

    def test_done_of_unknown_id(self):
        with self.assertRaises(KeyError):
            self.store.done(7)


if __name__ == "__main__":
    unittest.main()
'''

TODO_TEST_CLI = '''import io
import os
import tempfile
import unittest

from todo.cli import main


class CliTest(unittest.TestCase):
    def setUp(self):
        self.file = os.path.join(tempfile.mkdtemp(), "todo.json")

    def run_cli(self, *args):
        out = io.StringIO()
        rc = main(["--file", self.file] + list(args), out=out)
        return rc, out.getvalue()

    def test_add_then_list(self):
        self.run_cli("add", "milk")
        rc, out = self.run_cli("list")
        self.assertEqual(rc, 0)
        self.assertIn("- 1  milk", out)

    def test_done_of_unknown_id_fails(self):
        rc, out = self.run_cli("done", "3")
        self.assertEqual(rc, 1)
        self.assertIn("no item 3", out)


if __name__ == "__main__":
    unittest.main()
'''

TODO_README = """
# todo

A small to-do list for the terminal. Python 3.9 or later, standard library only.

```
python3 -m todo add "buy milk"
python3 -m todo list
python3 -m todo done 1
```

Items are kept in `~/.todo.json` (or `TODO_FILE`, or `--file`).
Run the tests with `python3 -m unittest discover -s tests`.
"""

TODO_CHANGELOG = """
# Changelog

## Unreleased

## 0.1.0

- Add, list and finish to-do items.
"""


def todo_ops(shared_changelog=False, exec_plan=None):
    """The due-dates feature: two requirements, three tasks in two waves (store and dates, then the CLI)."""
    due_api = {"id": "todo.store.due", "kind": "interface",
               "contract": "Store.add(text, due=None) -> dict; item['due'] is an ISO date string (YYYY-MM-DD) or None"}
    overdue_api = {"id": "todo.dates.overdue", "kind": "interface",
                   "contract": "is_overdue(item, today) -> bool; True when the item is open and its due date is"
                               " before today"}

    def files(*paths):
        return [{"id": f, "kind": "file"} for f in paths]

    def task(tid, title, paths, outputs, context, acceptance, prompt, needs=(), inputs=(), size="S"):
        fields = {"title": title, "status": "planned", "paths": list(paths), "class": "C", "size": size,
                  "exec": dict(exec_plan or HAIKU), "outputs": list(outputs) + files(*paths)}
        if shared_changelog and not needs:
            fields["shared_paths"] = ["CHANGELOG.md"]
            prompt = prompt + ["Then add one line under '## Unreleased' in CHANGELOG.md that says what you added."]
        if needs:
            fields["needs"] = list(needs)
        if inputs:
            fields["inputs"] = list(inputs)
        return {"op": "create", "id": tid, "fields": fields, "sections": {
            "Context": context,
            "Risks": [{"risk": "The worker edits files outside its paths.", "kind": "llm",
                       "mitigation": "The prompt names the only files to change."}],
            "Acceptance": acceptance, "Prompt": "\n".join(prompt)}}

    return [
        {"op": "create", "id": "todo", "fields": {"title": "To-do list", "status": "active"},
         "sections": {"Intent": "People keep their to-do items in the terminal and see what to do next."}},
        {"op": "create", "id": "todo/due-dates", "fields": {"title": "Due dates", "status": "active"},
         "sections": {"Intent": "An item can have a due date, and the user can list the items that are overdue.",
                      "Non-goals": ["Reminders or notifications.", "Times of day: a due date is a date only."]}},
        {"op": "create", "id": "todo/due-dates/store-due",
         "fields": {"title": "Store a due date", "status": "approved", "outputs": [due_api]},
         "sections": {
             "Statement": "When an item is added with a due date, the to-do store shall save the due date with the"
                          " item.",
             "Scenarios": [{"name": "An item with a due date", "steps": [
                 ["Given", "an empty store"], ["When", "the item 'pay rent' is added with the due date 2026-11-01"],
                 ["Then", "the stored item has due 2026-11-01"]]},
                 {"name": "An item without a due date", "steps": [
                     ["Given", "an empty store"], ["When", "the item 'milk' is added with no due date"],
                     ["Then", "the stored item has due None"]]}],
             "Verify": ["python3 -m unittest tests.test_due_store"]}},
        {"op": "create", "id": "todo/due-dates/overdue",
         "fields": {"title": "List overdue items", "status": "approved", "outputs": [overdue_api]},
         "sections": {
             "Statement": "When the user runs todo list --overdue, the CLI shall print only the open items whose"
                          " due date is before today.",
             "Scenarios": [{"name": "One item is overdue", "steps": [
                 ["Given", "an open item due yesterday and an open item due tomorrow"],
                 ["When", "the user runs todo list --overdue"], ["Then", "only the item due yesterday is printed"]]},
                 {"name": "A done item is never overdue", "steps": [
                     ["Given", "a done item due last week"], ["When", "the user runs todo list --overdue"],
                     ["Then", "nothing is printed"]]}],
             "Verify": ["python3 -m unittest tests.test_overdue"]}},
        task("todo/due-dates/store-due/store", "Save a due date in the store",
             ["todo/store.py", "tests/test_due_store.py"], [due_api],
             "Store.add takes only the text today. Items are dicts in one JSON file.",
             ["python3 -m unittest tests.test_due_store", "python3 -m unittest tests.test_store"],
             ["Add an optional due date to to-do items in todo/store.py.",
              "`Store.add(text, due=None)` stores `item['due']` as the given ISO date string (YYYY-MM-DD) or None.",
              "Old items without a 'due' key must still load.",
              "Write tests/test_due_store.py first, with an item with and an item without a due date.",
              "Change only todo/store.py and tests/test_due_store.py.",
              "Run `python3 -m unittest tests.test_due_store tests.test_store`, commit, and finish with your report."]),
        task("todo/due-dates/overdue/dates", "Decide whether an item is overdue",
             ["todo/dates.py", "tests/test_dates.py"], [overdue_api],
             "There is no date logic yet.",
             ["python3 -m unittest tests.test_dates"],
             ["Create todo/dates.py with `is_overdue(item, today)`, where `today` is a datetime.date.",
              "It is True when the item is not done and item.get('due') is an ISO date (YYYY-MM-DD) before today.",
              "An item without 'due' is never overdue.",
              "Write tests/test_dates.py first.",
              "Change only todo/dates.py and tests/test_dates.py.",
              "Run `python3 -m unittest tests.test_dates`, commit, and finish with your report."],
             size="XS"),
        task("todo/due-dates/overdue/cli", "Add --due and --overdue to the CLI",
             ["todo/cli.py", "tests/test_overdue.py"], [],
             "The CLI has add, list and done. The store saves due dates and todo/dates.py decides what is overdue.",
             ["python3 -m unittest tests.test_overdue", "python3 -m unittest tests.test_cli"],
             ["In todo/cli.py, add `--due YYYY-MM-DD` to `add` and pass it to Store.add.",
              "Add `--overdue` to `list`: print only the items for which"
              " todo.dates.is_overdue(item, datetime.date.today()) is True.",
              "Write tests/test_overdue.py first; it calls main() with --file in a temp dir.",
              "Change only todo/cli.py and tests/test_overdue.py.",
              "Run `python3 -m unittest tests.test_overdue tests.test_cli`, commit, and finish with your report."],
             needs=["todo/due-dates/store-due/store", "todo/due-dates/overdue/dates"],
             inputs=[dict(due_api, **{"from": "todo/due-dates/store-due/store"}),
                     dict(overdue_api, **{"from": "todo/due-dates/overdue/dates"})]),
    ]


def build_brownfield(repo, port, state, with_config):
    state = state or "plain"
    commit(repo, dict({"todo/__init__.py": '"""A small to-do list for the terminal."""\n',
                       "todo/store.py": TODO_STORE, "tests/test_store.py": TODO_TEST_STORE}, **GITIGNORE),
           "Store to-do items in a JSON file")
    commit(repo, {"todo/cli.py": TODO_CLI, "todo/__main__.py": TODO_MAIN, "tests/test_cli.py": TODO_TEST_CLI,
                  "README.md": TODO_README}, "Add the todo command line")
    extra = {}
    if state == "triage":
        extra["protected_paths_cmd"] = ["printf", "%s\\n", "CHANGELOG.md"]
        commit(repo, {"CHANGELOG.md": TODO_CHANGELOG}, "Start a changelog")
    if with_config:
        commit(repo, config("todo", port, **extra), "Add the wavecoord config")
    if state == "plain":
        return
    spec_apply(repo, todo_ops(shared_changelog=(state == "triage"), exec_plan=SONNET if state == "watch" else None),
               "Plan: due dates (todo/due-dates)")
    if state == "flawed":
        base = "specs/todo/due-dates"
        # a vague draft: no EARS form, no scenario, no Verify (check allows it while it is a draft)
        spec_apply(repo, [{"op": "create", "id": "todo/due-dates/reminders",
                           "fields": {"title": "Reminders", "status": "draft"},
                           "sections": {"Statement": "Reminders should be quick and friendly and not annoying."}}],
                   "Draft: reminders")
        # a wide glob: the dates task now overlaps the store task in the same wave (check error P), and an
        # Acceptance that passes without the work
        edit(repo, base + "/overdue/tasks/dates.md", "  - todo/dates.py\n", "  - todo/**\n")
        edit(repo, base + "/overdue/tasks/dates.md", "- `python3 -m unittest tests.test_dates`",
             "- `python3 -c \"print('ok')\"`")
        git(repo, "commit", "-q", "-am", "Plan: widen the dates task")
        # the requirement changes after its tasks were planned: the store task becomes suspect
        edit(repo, base + "/store-due/_node.md",
             "the to-do store shall save the due date with the item.",
             "the to-do store shall save the due date with the item and refuse a date that is not YYYY-MM-DD.")
        git(repo, "commit", "-q", "-am", "Require valid due dates")


# ---------------------------------------------------------------- speckit: recipes

RECIPES_MODEL = '''"""Recipes: a name, a number of servings and ingredients."""


class Ingredient(object):
    def __init__(self, name, qty, unit):
        self.name, self.qty, self.unit = name, float(qty), unit

    def __repr__(self):
        return "Ingredient(%r, %r, %r)" % (self.name, self.qty, self.unit)


class Recipe(object):
    def __init__(self, name, servings, ingredients):
        self.name, self.servings, self.ingredients = name, int(servings), list(ingredients)


def from_dict(data):
    return Recipe(data["name"], data["servings"],
                  [Ingredient(i["name"], i["qty"], i["unit"]) for i in data["ingredients"]])
'''

RECIPES_CLI = '''"""`python3 -m recipes show FILE` prints a recipe."""

import argparse
import json
import sys

from recipes.model import from_dict


def main(argv=None, out=None):
    out = out or sys.stdout
    p = argparse.ArgumentParser(prog="recipes")
    sub = p.add_subparsers(dest="cmd")
    sub.required = True
    s = sub.add_parser("show")
    s.add_argument("file")
    args = p.parse_args(argv)
    with open(args.file, encoding="utf-8") as f:
        recipe = from_dict(json.load(f))
    out.write("%s (serves %d)\\n" % (recipe.name, recipe.servings))
    for i in recipe.ingredients:
        out.write("- %g %s %s\\n" % (i.qty, i.unit, i.name))
    return 0
'''

RECIPES_MAIN = TODO_MAIN.replace("todo", "recipes")

RECIPES_TEST = '''import unittest

from recipes.model import from_dict


class ModelTest(unittest.TestCase):
    def test_from_dict(self):
        r = from_dict({"name": "Pancakes", "servings": 2,
                       "ingredients": [{"name": "flour", "qty": 200, "unit": "g"}]})
        self.assertEqual(r.servings, 2)
        self.assertEqual(r.ingredients[0].qty, 200.0)


if __name__ == "__main__":
    unittest.main()
'''

SPECKIT_CONSTITUTION = """
# Recipes Constitution

## Core Principles

### I. Standard Library Only
The tool runs on Python 3.9 or later with no third-party packages.

### II. Test-First (NON-NEGOTIABLE)
Tests are written and fail before the implementation.

### III. Small Modules
One module per concern, each with its own test module in `tests/`.

## Governance

This constitution supersedes other practices.

**Version**: 1.0.0 | **Ratified**: 2026-09-01 | **Last Amended**: 2026-09-01
"""

SPECKIT_SPEC = """
# Feature Specification: Scale a Recipe

**Feature Branch**: `001-scale-recipe`

**Created**: 2026-09-02

**Status**: Draft

**Input**: User description: "Scale a recipe to another number of servings."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Scale to N servings (Priority: P1)

A cook has a recipe for 2 and wants to cook for 6.

**Why this priority**: Scaling is the reason for this feature.

**Independent Test**: Scale a 2-serving recipe to 6 and check every quantity tripled.

**Acceptance Scenarios**:

1. **Given** a recipe for 2 with 200 g flour, **When** the cook scales it to 6 servings, **Then** it lists 600 g flour
2. **Given** any recipe, **When** the cook asks for 0 servings, **Then** the tool refuses with an error

### User Story 2 - Kitchen-friendly amounts (Priority: P2)

Scaled amounts are rounded so that a cook can measure them.

**Why this priority**: 133.333 g is hard to weigh.

**Acceptance Scenarios**:

1. **Given** a scaled amount of 133.333 g, **When** it is shown, **Then** it reads 133 g

### Edge Cases

- What happens with a recipe that has no ingredients?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST multiply every quantity by the new servings divided by the old servings.
- **FR-002**: System MUST refuse a number of servings below 1.
- **FR-003**: System MUST round grams to whole numbers.

### Key Entities

- **Recipe**: a name, servings and ingredients.
"""

SPECKIT_PLAN = """
# Implementation Plan: Scale a Recipe

**Branch**: `001-scale-recipe` | **Spec**: specs/001-scale-recipe/spec.md

## Summary

Add `scale(recipe, servings)` and a `scale` command.

## Technical Context

**Language/Version**: Python 3.9+
**Primary Dependencies**: none
**Testing**: unittest
"""

SPECKIT_TASKS = """
# Tasks: Scale a Recipe

**Input**: Design documents from `/specs/001-scale-recipe/`

## Format: `[ID] [P?] [Story] Description`

## Phase 1: User Story 1 - Scale to N servings (Priority: P1)

- [ ] T001 [P] [US1] Write scaling tests in tests/test_scale.py
- [ ] T002 [US1] Implement scale(recipe, servings) in recipes/scale.py
- [ ] T003 [US1] Add the scale command in recipes/cli.py

## Phase 2: User Story 2 - Kitchen-friendly amounts (Priority: P2)

- [ ] T004 [P] [US2] Round amounts in recipes/rounding.py
- [ ] T005 [US2] Use the rounding when showing a scaled recipe in recipes/cli.py
"""

RECIPE_JSON = json.dumps({"name": "Pancakes", "servings": 2, "ingredients": [
    {"name": "flour", "qty": 200, "unit": "g"}, {"name": "milk", "qty": 300, "unit": "ml"},
    {"name": "egg", "qty": 2, "unit": "pc"}]}, indent=1) + "\n"


def build_speckit(repo, port, state, with_config):
    commit(repo, dict({"recipes/__init__.py": '"""Recipes."""\n', "recipes/model.py": RECIPES_MODEL,
                       "recipes/cli.py": RECIPES_CLI, "recipes/__main__.py": RECIPES_MAIN,
                       "tests/test_model.py": RECIPES_TEST, "examples/pancakes.json": RECIPE_JSON,
                       "README.md": "# recipes\n\nShow and scale recipes in the terminal.\n"}, **GITIGNORE),
           "Show recipes")
    commit(repo, {".specify/memory/constitution.md": SPECKIT_CONSTITUTION,
                  "specs/001-scale-recipe/spec.md": SPECKIT_SPEC, "specs/001-scale-recipe/plan.md": SPECKIT_PLAN,
                  "specs/001-scale-recipe/tasks.md": SPECKIT_TASKS}, "Spec Kit: scale a recipe")
    if with_config:
        commit(repo, config("recipes", port), "Add the wavecoord config")


# ---------------------------------------------------------------- kiro: habits

HABITS_STORE = '''"""Habits and the days they were done, in one JSON file."""

import json
import os


class Habits(object):
    def __init__(self, path):
        self.path = path

    def load(self):
        if not os.path.exists(self.path):
            return {}
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def check_in(self, habit, day):
        """Record that `habit` was done on `day` (YYYY-MM-DD)."""
        data = self.load()
        days = set(data.get(habit, []))
        days.add(day)
        data[habit] = sorted(days)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
'''

HABITS_TEST = '''import os
import tempfile
import unittest

from habits.store import Habits


class HabitsTest(unittest.TestCase):
    def test_check_in_twice_keeps_one_day(self):
        h = Habits(os.path.join(tempfile.mkdtemp(), "h.json"))
        h.check_in("run", "2026-10-01")
        h.check_in("run", "2026-10-01")
        self.assertEqual(h.load(), {"run": ["2026-10-01"]})


if __name__ == "__main__":
    unittest.main()
'''

KIRO_PRODUCT = """
---
inclusion: always
---

# Product Overview

A habit tracker for the terminal.
Users check in each day they do a habit and see their streaks.
"""

KIRO_TECH = """
---
inclusion: always
---

# Technology Stack

- Python 3.9 or later, standard library only.
- One JSON file for storage.

## Common commands

```bash
python3 -m unittest discover -s tests
```
"""

KIRO_STRUCTURE = """
---
inclusion: always
---

# Project Structure

- `habits/`: the package, one module per concern.
- `tests/`: one test module per source module.
"""

KIRO_REQUIREMENTS = """
# Requirements Document

## Introduction

A streak is the number of days in a row a habit was done, ending today or yesterday.

## Requirements

### Requirement 1

**User Story:** As a user, I want to see my current streak, so that I keep going.

#### Acceptance Criteria

1. WHEN a habit was done today and on the 4 days before THEN the system SHALL report a streak of 5
2. IF a habit was not done today or yesterday THEN the system SHALL report a streak of 0

### Requirement 2

**User Story:** As a user, I want the streak in the list, so that I see all habits at once.

#### Acceptance Criteria

1. WHEN the user lists habits THEN the system SHALL print each habit with its current streak
"""

KIRO_DESIGN = """
# Design Document

## Overview

`current_streak(days, today)` counts back from today, or from yesterday when today is missing.

## Components and Interfaces

- `current_streak(days, today) -> int` in `habits/streak.py`.
- The list command in `habits/cli.py` prints `<habit>  <streak>`.
"""

KIRO_TASKS = """
# Implementation Plan

- [ ] 1. Compute streaks
- [ ] 1.1 Implement current_streak
  - Add `current_streak(days, today)` in habits/streak.py
  - _Requirements: 1.1, 1.2_
- [ ] 1.2 Write streak tests
  - Cover a 5-day streak and a broken streak in tests/test_streak.py
  - _Requirements: 1.1, 1.2_

- [ ] 2. Show streaks in the list
  - Print each habit with its streak in habits/cli.py
  - _Requirements: 2.1_
"""


def build_kiro(repo, port, state, with_config):
    commit(repo, dict({"habits/__init__.py": '"""A habit tracker."""\n', "habits/store.py": HABITS_STORE,
                       "tests/test_store.py": HABITS_TEST,
                       "README.md": "# habits\n\nTrack daily habits in the terminal.\n"}, **GITIGNORE),
           "Check in habits")
    commit(repo, {".kiro/steering/product.md": KIRO_PRODUCT, ".kiro/steering/tech.md": KIRO_TECH,
                  ".kiro/steering/structure.md": KIRO_STRUCTURE,
                  ".kiro/specs/streaks/requirements.md": KIRO_REQUIREMENTS,
                  ".kiro/specs/streaks/design.md": KIRO_DESIGN, ".kiro/specs/streaks/tasks.md": KIRO_TASKS},
           "Kiro: streaks spec")
    if with_config:
        commit(repo, config("habits", port), "Add the wavecoord config")


# ---------------------------------------------------------------- openspec: glossary

GLOSSARY_STORE = '''"""Terms and their definitions, in one JSON file."""

import json
import os


class Glossary(object):
    def __init__(self, path):
        self.path = path

    def load(self):
        if not os.path.exists(self.path):
            return {}
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def define(self, term, text):
        if not term.strip():
            raise ValueError("a term needs a name")
        data = self.load()
        data[term.strip().lower()] = text
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
'''

GLOSSARY_TEST = '''import os
import tempfile
import unittest

from glossary.store import Glossary


class GlossaryTest(unittest.TestCase):
    def setUp(self):
        self.g = Glossary(os.path.join(tempfile.mkdtemp(), "g.json"))

    def test_define_lowercases_the_term(self):
        self.g.define("API", "An interface")
        self.assertEqual(self.g.load(), {"api": "An interface"})

    def test_empty_term_is_refused(self):
        with self.assertRaises(ValueError):
            self.g.define("  ", "x")


if __name__ == "__main__":
    unittest.main()
'''

OPENSPEC_PROJECT = """
# Project Context

## Purpose

A team glossary for the terminal: define terms and look them up.

## Tech Stack

- Python 3.9 or later, standard library only

## Project Conventions

Tests live in `tests/`, one module per source module.
"""

OPENSPEC_TERMS = """
# terms Specification

## Purpose

Users define terms and read their definitions.

## Requirements

### Requirement: Define a term

The system SHALL store a term in lowercase with its definition.

#### Scenario: A term is stored

- **WHEN** a user defines "API" as "An interface"
- **THEN** the glossary holds "api" with that definition

#### Scenario: An empty term is refused

- **WHEN** a user defines a term that is only spaces
- **THEN** the system refuses it with an error
"""

OPENSPEC_PROPOSAL = """
# Proposal

## Why

Users cannot find a term when they only remember part of it.

## What Changes

- Add a search that matches part of a term.

## Impact

- Affected specs: terms
- Affected code: glossary/
"""

OPENSPEC_DELTA = """
## ADDED Requirements

### Requirement: Search terms

WHEN a user searches for a word, the system SHALL return every term that contains it, in alphabetical order.

#### Scenario: Partial match

- **GIVEN** the terms "api", "rapid" and "cache"
- **WHEN** a user searches for "api"
- **THEN** the result is "api" and "rapid"
"""

OPENSPEC_TASKS = """
# Tasks

## 1. Search

- [ ] 1.1 Add search(word) in glossary/search.py
- [ ] 1.2 Write search tests in tests/test_search.py
"""


def build_openspec(repo, port, state, with_config):
    commit(repo, dict({"glossary/__init__.py": '"""A team glossary."""\n', "glossary/store.py": GLOSSARY_STORE,
                       "tests/test_store.py": GLOSSARY_TEST,
                       "README.md": "# glossary\n\nA team glossary for the terminal.\n"}, **GITIGNORE),
           "Define terms")
    commit(repo, {"openspec/project.md": OPENSPEC_PROJECT, "openspec/specs/terms/spec.md": OPENSPEC_TERMS,
                  "openspec/changes/add-term-search/proposal.md": OPENSPEC_PROPOSAL,
                  "openspec/changes/add-term-search/specs/terms/spec.md": OPENSPEC_DELTA,
                  "openspec/changes/add-term-search/tasks.md": OPENSPEC_TASKS}, "OpenSpec: terms and a search change")
    if with_config:
        commit(repo, config("glossary", port), "Add the wavecoord config")


# ---------------------------------------------------------------- build

DEMOS = {
    "fresh": ("bookmarks", build_fresh, ("plain",)),
    "brownfield": ("todo", build_brownfield, ("plain", "planned", "watch", "flawed", "triage")),
    "speckit": ("recipes", build_speckit, ("plain",)),
    "kiro": ("habits", build_kiro, ("plain",)),
    "openspec": ("glossary", build_openspec, ("plain",)),
}


def paths(repo):
    """The origin, the forge state and the worktrees folder of a demo repo."""
    parent, name = os.path.dirname(os.path.abspath(repo)), os.path.basename(os.path.abspath(repo))
    origin = os.path.join(parent, "origin", name + ".git")
    return {"repo": os.path.abspath(repo), "origin": origin, "forge": origin + ".forge.json",
            "worktrees": os.path.join(parent, "wt")}


def build(name, repo, port=DEFAULT_PORT, state=None, with_config=True, allow_any_parent=False):
    """Build demo `name` in `repo` (wiped first) with a bare origin; -> paths(repo) plus the project."""
    if name not in DEMOS:
        raise DemoError("unknown demo %r (one of %s)" % (name, ", ".join(sorted(DEMOS))))
    project, fn, states = DEMOS[name]
    if state and state not in states:
        raise DemoError("demo %s has no state %r (one of %s)" % (name, state, ", ".join(states)))
    p = paths(repo)
    parent = os.path.dirname(p["repo"])
    if not allow_any_parent and not os.path.basename(parent).startswith("wavecoord-demo"):
        raise DemoError("refusing to wipe %s: demos live under a folder named wavecoord-demo*" % p["repo"])
    for d in (p["repo"], p["origin"], os.path.join(p["worktrees"], project)):
        shutil.rmtree(d, ignore_errors=True)
    if os.path.exists(p["forge"]):
        os.remove(p["forge"])
    os.makedirs(p["repo"])
    os.makedirs(os.path.dirname(p["origin"]), exist_ok=True)
    run(["git", "init", "-q", "--bare", "-b", "main", p["origin"]], parent)
    git(p["repo"], "init", "-q", "-b", "main")
    git(p["repo"], "remote", "add", "origin", p["origin"])
    fn(p["repo"], port, state, with_config)
    git(p["repo"], "push", "-q", "-u", "origin", "main")
    git(p["repo"], "remote", "set-head", "origin", "main")
    p["project"] = project
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("demo", nargs="?", choices=sorted(DEMOS))
    ap.add_argument("repo", nargs="?", help="where to build it, under a folder named wavecoord-demo*")
    ap.add_argument("--state", help="brownfield: plain, planned, flawed or triage")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="coordinator port in coord_url")
    ap.add_argument("--no-config", action="store_true", help="leave out .claude/wavecoord.json")
    ap.add_argument("--list", action="store_true", help="list the demos")
    a = ap.parse_args(argv)
    if a.list or not a.demo:
        for k in sorted(DEMOS):
            print("%-10s %-10s states: %s" % (k, DEMOS[k][0], ", ".join(DEMOS[k][2])))
        return 0
    if not a.repo:
        ap.error("give the folder to build it in, for example /tmp/wavecoord-demo/%s" % DEMOS[a.demo][0])
    try:
        p = build(a.demo, a.repo, port=a.port, state=a.state, with_config=not a.no_config)
    except DemoError as e:
        sys.stderr.write("demos: %s\n" % e)
        return 1
    print("built %s (%s) in %s, origin %s" % (a.demo, p["project"], p["repo"], p["origin"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
