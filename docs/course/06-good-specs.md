# Lesson 6: Writing good specs and tasks

**Video:** [course-06-good-specs.mp4](https://github.com/jellologic/wavecoord/releases/download/v0.3.0/course-06-good-specs.mp4) (about 5 minutes)

## Goals

- Size requirements and tasks so they run well in parallel.
- Choose `paths`, `needs` and `conflicts_with` on purpose.
- Use the `reviewer` agent, and fix `spec check` errors and warnings.
- Handle suspect tasks with `/wavecoord:revise`.

## Why it matters

Workers do what the spec says, in parallel, without talking to each other. A wide `paths` glob serializes the wave; a missing contract makes two workers guess the same interface differently; an Acceptance that always passes lets broken work merge. Every one of these is cheap to fix in the spec and expensive after a wave.

## Good requirements

- **One testable rule each.** "The service shall rotate tokens and log the rotation" is two requirements.
- **A measurable response**: a value, a status code, a file, an output line.
- **Verify that fails without the work.** Run it on main before you approve.
- **A contract on every shared output.** When other work builds on an interface, write it in one line: `contract: is_overdue(item, today) -> bool`.
- **Shared work is its own requirement** (a schema, a migration) that the others `need`.

## Good tasks

| Field | Good | Bad | Why |
|---|---|---|---|
| size | `XS` to `L`, one worker session, 10 to 40 turns | `XL` "due dates, reminders and export" | Too big for one session; no parallelism |
| `paths` | `[todo/dates.py, tests/test_dates.py]` | `[todo/**]` | A wide glob blocks every task in `todo/` and lets the worker edit anything there |
| `needs` | the CLI task needs the store and dates tasks | every task needs the previous one | Each `needs` makes the critical path longer; use it only for a real order |
| `conflicts_with` | two tasks that migrate the same database | two tasks that edit the same file | Same file: give it to one task, or add a `needs`. `conflicts_with` is for what paths cannot show |
| `shared_paths` | `[CHANGELOG.md]` for an append-only file | a source file | Shared files can still conflict at merge; prefer one owner |
| Acceptance | `python3 -m unittest tests.test_dates` | `python3 -c "print('ok')"` | It must fail without the work |
| Prompt | names the files to change and not to change, the tests to write, when to stop | "implement due dates" | The worker sees only the prompt, the spec and the repo |

`wavecoord spec waves` shows the result: tasks run together when neither needs the other, their paths do not overlap, and they do not `conflicts_with` each other.

## Steps

The demo plan for due dates has three mistakes on purpose.

### 1. Check the tree

```
! wavecoord spec check
```

What you see:

```
error P todo/due-dates/overdue/tasks/dates.md: todo/due-dates/overdue/dates and todo/due-dates/store-due/store may run at the same time but both write todo/** / todo/store.py: add a needs or split the paths
warning suspect todo/due-dates/store-due/tasks/store.md: the requirement changed since basis ... review the task, then `wavecoord spec rebase-basis todo/due-dates/store-due/store`
invalid: 8 records, 1 errors, 1 warnings
```

Errors block: a plan PR with one cannot be made with `spec_edit`, and CI should refuse it (`wavecoord spec check --base origin/main`). Read every warning too. The ones you will meet most:

| Rule | Meaning | Fix |
|---|---|---|
| `P` (error) | Two tasks may run together but write the same path | Narrow `paths`, split the file, or add a `needs` |
| `A1`, `A3` (warning) | A promised output has no task that produces it | Expected before wave-plan; after it, a task is missing |
| `A6` (warning) | A path with no output saying what the task hands over | Add the file as an output of the task |
| `suspect` (warning) | The requirement changed after the task was planned | Review the task, then rebase its basis |
| `ears` (error) | An approved requirement's statement is not one EARS sentence with `shall` once | Rewrite it ([ears.md](../../plugin/skills/spec/ears.md)) |

### 2. Suspect tasks

```
! wavecoord spec suspect
```

Each task records a fingerprint (`basis`) of its requirement. When the statement, scenarios, Verify or contracts change, the task becomes **suspect**: it may no longer fit. Read the change, then either keep the task (`wavecoord spec rebase-basis <task>`) or change it to fit, in a plan PR. `"spec": {"suspect": "refuse"}` in the config keeps suspect tasks from being dispatched.

### 3. Ask the reviewer

```
Use the wavecoord:reviewer agent in spec mode to review the requirements and tasks under todo/due-dates
on main. Show me its findings. Do not change anything yet.
```

What you see: findings marked `must` or `should`, each with the record, the problem and the fix. It catches what a script cannot judge: the Acceptance that always passes, the vague "Reminders should be quick and friendly" draft. The spec and wave-plan skills already run it before they open a PR; asking yourself is useful after hand edits.

### 4. Fix it with revise

```
/wavecoord:revise fix the plan under todo/due-dates: narrow the paths of the dates task to todo/dates.py
and tests/test_dates.py, make its Acceptance python3 -m unittest tests.test_dates, review the suspect
store task against the changed requirement and rebase its basis, and leave the reminders draft as it is.
Do not ask me anything.
```

What you see: one plan PR with `spec_edit` ops (`set`, `set_section`, `rebase_basis`) and the check result in its body. Merge it and check again:

```
! gh pr merge 1 --merge && git pull -q
! wavecoord spec check && wavecoord spec waves
```

The two wave-1 tasks run side by side again. The vague draft stays a draft: it never runs until someone rewrites and approves it.

## Common mistakes

- **Fixing a spec with a hint to the worker.** Hints are guidance; the spec is the authority. Change the spec in a plan PR.
- **Adding `needs` to silence error P.** It works, but it serializes the wave. First try to split the file or narrow the paths.
- **Rebasing a suspect task without reading the change.** That hides exactly the drift the warning is for.
- **Copying the requirement's Verify into every task's Acceptance.** The refinery runs Verify with the last task of the requirement; earlier tasks cannot pass it yet.

## Exercise

Build the flawed demo: `python3 scripts/course/demos/demos.py brownfield /tmp/wavecoord-demo/todo --state flawed --port 18741`.

1. Run `wavecoord spec check` and `wavecoord spec suspect`. Explain each line in your own words.
2. Ask the reviewer for findings. Which ones would `spec check` never find?
3. Rewrite the reminders draft as one EARS statement with a scenario and a Verify, with `/wavecoord:spec`.
4. Fix the rest with `/wavecoord:revise`, merge, and confirm `0 errors`.

Next: [Lesson 7: Running waves](07-running-waves.md).
