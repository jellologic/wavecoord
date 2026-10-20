# Lesson 4: An existing project with no specs

**Video:** [course-04-existing-project.mp4](https://github.com/jellologic/wavecoord/releases/download/v0.3.0/course-04-existing-project.mp4) (about 5 minutes)

## Goals

- Start SDD in a codebase that has code and tests but no specs.
- Let `/wavecoord:spec` read the code, and specify only the next feature.
- Write Verify commands that live next to your existing tests.
- Check the `paths` the planner picks for each task.

## Why it matters

The common mistake is to "spec the world" first: describe all existing behaviour before building anything. It takes long, it goes stale, and it does not help the next change. SDD pays off on the change you are about to make. The spec tree then grows one feature at a time, with the work.

Existing code also changes what Verify means. Your current tests already pass, so they cannot tell whether the new work is done. They still matter: they guard against breaking what exists. They belong in `check_cmd`, which the refinery runs before each merge, not in Verify.

| Command | Fails today? | Where it goes |
|---|---|---|
| `python3 -m unittest tests.test_due_store` (new) | Yes, the test does not exist | the requirement's Verify, the task's Acceptance |
| `python3 -m unittest discover -s tests` (all) | No, it passes | `check_cmd` in `.claude/wavecoord.json` |

## Steps

The demo is `todo`: a small Python CLI (`todo/store.py`, `todo/cli.py`) with five tests and no `specs/`.

### 1. Look at what is there

```
! ls todo tests && python3 -m unittest discover -s tests 2>&1 | tail -3
```

### 2. Specify the next feature

```
/wavecoord:spec add due dates to to-do items: todo add --due YYYY-MM-DD, and todo list --overdue. Read
the code first, and specify only this feature. Users: the same terminal users. Done when add --due saves
the date with the item, and list --overdue prints only the open items due before today. Out of scope:
reminders and times of day. The existing tests must keep passing. Each Verify should run a new test
module with python3 -m unittest, such as tests.test_due_store. When the records are right, approve the
requirements and activate the capability and feature in this PR. Do not ask me anything else.
```

What you see: the skill reads the entry points, the tests and the test command first. The request already says who uses the feature, what done means, what is out of scope and what must keep passing; leave any of these out and the skill asks for it before it writes records.

Then one plan PR. Read the Verify lines:

```
! gh pr diff 1 | grep -A3 '^+## Verify'
```

Each names a new test module. Merge it:

```
! gh pr merge 1 --merge && git pull -q
```

### 3. Plan the tasks and check the paths

```
/wavecoord:wave-plan todo. Use model haiku, effort low, max_turns 30, budget_usd 1 and timeout_min 20 for
every task, and keep each task to one module and its test. Write each Acceptance command as one plain
python3 command. Do not ask me anything.
```

The planner reads the code to pick each task's `paths`: the files it may change. Look at them in the PR:

```
! gh pr diff 2 | grep -A3 '^+paths:'
```

Good paths for this feature look like:

```yaml
paths:            # task: save a due date in the store
  - todo/store.py
  - tests/test_due_store.py
```

- The module **and its test**: the task can then prove its own work.
- Files, not folders: `todo/**` would block every other task in `todo/` and let the worker change anything there.
- Tasks in the same wave never share a file. If two must change one file, one `needs` the other.

Merge and look at the waves:

```
! gh pr merge 2 --merge && git pull -q
! wavecoord spec waves
```

## Common mistakes

- **Specifying the existing code.** Describe only the behaviour you are adding or changing.
- **Putting the full suite in Verify.** It already passes. Put it in `check_cmd` (`wavecoord init --check-cmd "python3 -m unittest discover -s tests"` adds it).
- **Paths that miss the test file.** The worker cannot add its test, and the fence warns (or blocks) when it does.
- **A task that "refactors while it is there".** Scope creep makes tasks overlap. Plan the refactor as its own task.

## Exercise

On the practice repo (`python3 scripts/course/demos/demos.py brownfield /tmp/wavecoord-demo/todo --port 18741`) or your own project:

1. Pick the smallest useful next feature. Run `/wavecoord:spec` for it and say "read the code first".
2. Before merging, run each Verify on main and confirm it fails. Confirm your full suite passes and is in `check_cmd`.
3. Run `/wavecoord:wave-plan` and, for each task, explain why each path is in its list.

Next: [Lesson 6: Writing good specs and tasks](06-good-specs.md).
