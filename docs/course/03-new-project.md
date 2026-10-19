# Lesson 3: A new project from scratch

**Video:** [course-03-new-project.mp4](https://github.com/jellologic/wavecoord/releases/download/v0.3.0/course-03-new-project.mp4) (about 10 minutes; the waits are cut)

## Goals

- Go around the whole loop once: spec, wave-plan, wave-run, triage, merge.
- See a second wave start from the merges of the first.
- Know what each plan PR holds and what you check before merging it.

## Why it matters

Each step has one job and one review point. The spec PR fixes *what*; the plan PR fixes *who does what, with which files and budget*; triage merges only work that passes the checks. Doing the loop once end to end makes every later lesson a detail of it.

```mermaid
sequenceDiagram
  actor You
  participant L as Lead session
  participant C as Coordinator
  participant W as Workers
  You->>L: /wavecoord:spec
  L-->>You: plan PR 1 (requirements)
  You->>You: review, merge
  You->>L: /wavecoord:wave-plan
  L-->>You: plan PR 2 (tasks, waves)
  You->>You: review, merge
  You->>L: /wavecoord:wave-run
  L->>C: dispatch_wave
  C->>W: one worker per ready task
  W-->>C: reports, pushed branches
  You->>L: /wavecoord:triage
  L->>C: collect, refinery_merge
  C-->>You: merged; next tasks ready
  You->>L: /wavecoord:wave-run (wave 2)
```

## Steps

The demo starts from the `bookmarks` repo: a README with the idea and the wavecoord config.

### 1. Spec

```
/wavecoord:spec the bookmarks CLI from the README. Users: one person in a terminal. Out of scope: tags
and any web page. Make four requirements. One for the store and the entry point: bookmarks live in a JSON
file, and python3 -m bookmarks <command> runs bookmarks/cmd_<command>.py. Then one each for add, list and
find, each a command module. Each Verify runs one new test module with python3 -m unittest, and the
store's test calls the store directly, not the CLI. When the records are right, you may set the
requirements to approved and the capability and feature to active in this PR. Do not ask me anything
else.
```

Here the answers to the usual questions are in the request. What you see: a plan PR with a capability, a feature and a few requirements. Review it as in lesson 2, then merge:

```
! gh pr merge 1 --merge && git pull -q
```

### 2. Plan the tasks

```
/wavecoord:wave-plan bookmarks. Make two waves. Wave 1 is one task: the store module and the entry point,
which runs the command module bookmarks/cmd_<name>.py for each command name, so no later task edits a
shared file. Wave 2 is three parallel tasks, add, list and find, each one command module and its test,
needing only the first task. Use model haiku, effort low, max_turns 30, budget_usd 1 and timeout_min 20
for every task. Do not ask me anything; if the plan cannot take this shape, pick the closest one and say
why.
```

You may leave the shape to the planner. Asking for it here shows the point of waves: a small foundation first, then tasks that never touch the same file, so they can run side by side.

What you see: `planner` subagents draft tasks under the requirements, `reviewer` subagents critique them, and the skill fixes every `must` finding. It opens a second plan PR, with a table (id, size, model, budget, needs, paths) and the waves of the PR's commit:

```
wave 1 (1):  <task>
wave 2 (3):  <task>, <task>, <task>
critical path: <task> -> <task> (2)
max width: 3
```

The task names depend on what the planner writes; yours will differ.

Before you merge, check that tasks in the same wave have disjoint `paths`, that each Acceptance fails without the work, and that the budgets add up to what you want to spend. Without the model instruction, the planner picks models by task size (sonnet for most); the course uses haiku to keep it cheap and fast. Then:

```
! gh pr merge 2 --merge && git pull -q
! wavecoord spec waves
```

### 3. Run the wave

```
/wavecoord:wave-run
```

What you see: a table of the ready tasks it picks (and the ones it skips, with why), then `dispatch_wave`. Each task gets a lease on its paths and a worker in its own worktree and branch (`wave/<task>-1`). The skill arms a Monitor, so the workers' events arrive in your session.

```
! wavecoord attempts ls --active
```

The workers run on their own for a few minutes; you can keep using the session meanwhile.

### 4. Triage

When the reports are in:

```
/wavecoord:triage
```

What you see: `collect` opens one PR per finished attempt; `refinery_merge` merges them one at a time after its checks (lesson 9); a table of what merged and what waits for you. After each merge, the coordinator commits the task's status (`done`) into the spec tree, and the tasks that needed it become ready.

### 5. The next wave

```
/wavecoord:wave-run
/wavecoord:triage
! wavecoord spec tree
! git pull -q && git log --oneline -12
```

The tree shows every task `done`. The history shows the two plan PRs, one merge per task and the status commits.

To keep the loop going while you are away: `/loop 10m /wavecoord:triage`. In a loop, triage merges what passes, retries within limits and collects your questions; it never approves a protected path or raises a budget.

## Common mistakes

- **Merging the plan PR unread.** Overlapping paths, an Acceptance that always passes, or a budget ten times too high all start there.
- **Running `wave-run` before the plan PR is merged.** The lead plans only from the default branch: nothing is ready yet.
- **Waiting for a PR that never opens.** PRs open in `collect`, which triage runs first. Run `/wavecoord:triage`.
- **Starting a second lead session** to "watch". One lead per coordinator; use `wavecoord attempts ls` in a terminal instead.

## Exercise

On the practice `bookmarks` repo:

1. Run the loop to the end of wave 1. Write down the cost of each attempt (`wavecoord attempts ls`).
2. Before wave 2, read one task of wave 2 with `wavecoord spec show <task>`: which input does it take from wave 1?
3. Finish wave 2, then run the app's tests yourself: `python3 -m unittest discover -s tests`.

Next: [Lesson 6: Writing good specs and tasks](06-good-specs.md).
