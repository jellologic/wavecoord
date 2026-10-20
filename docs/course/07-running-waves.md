# Lesson 7: Running waves

**Video:** [course-07-running-waves.mp4](https://github.com/jellologic/wavecoord/releases/download/v0.3.0/course-07-running-waves.mp4) (about 5 minutes)

## Goals

- Run a wave with `/wavecoord:wave-run` and know what it picks and why.
- Set `wip_cap`, budgets and models.
- Know what a worker does, and what it may not do.
- Roll out the edit fence from `advisory` to `enforce`; use protected paths and `check_cmd`.

## Why it matters

A wave runs unattended, so its limits must be set before it starts: how many workers at once, how much each may spend, what each may run, and what must hold before anything merges. These settings are what makes parallel agents safe to leave alone.

## The settings

In `.claude/wavecoord.json` (on the default branch; a task PR can never change them):

```json
{
  "hosts": [{"name": "local", "slots": 2, "repo_path": ".",
             "claude_flags": ["--permission-mode", "acceptEdits", "--allowedTools",
                              "Bash(git add *),Bash(git commit *),Bash(python3 *)"]}],
  "wip_cap": 2,
  "fence": "advisory",
  "check_cmd": "python3 -m unittest discover -s tests"
}
```

| Setting | What it does | Start with |
|---|---|---|
| `wip_cap` | The most attempts in flight (default: the sum of the host slots) | 2 or 3; raise it after a clean wave |
| `hosts[].slots` | Workers per machine | What the machine's memory and your rate limits allow |
| `hosts[].claude_flags` | What an unattended worker may do | Edits, `git add`, `git commit` and your test command. Never `git push`: the launcher pushes |
| `check_cmd` | Runs before each merge, on the PR's head | Your test command |
| `protected_paths_cmd` | Prints globs; a PR that touches one waits for your yes | `["printf", "%s\n", "migrations/**", ".github/**", "CLAUDE.md"]` |
| `fence` | The edit fence mode for every point | `advisory` |

Each task carries its own budget in its `exec` block:

```yaml
exec:
  model: haiku        # or sonnet, opus; fallback_model one step down
  effort: low
  max_turns: 30       # the loop cap
  budget_usd: 1       # a hard stop
  timeout_min: 20     # the attempt is killed after this
```

The wave-plan skill sets these by task size (XS: 25 turns, $1, 20 min; S: 50, $2, 40 min; M: 90, $5, 75 min); you can ask for other values, as the course does with haiku.

## Steps

### 1. Read the config and a task

```
! cat .claude/wavecoord.json
! wavecoord spec show todo/due-dates/overdue/dates | sed -n '/^paths:/,/^---/p'
```

### 2. Run the wave

```
/wavecoord:wave-run
```

What you see:

1. `wavecoord status` must print `ok:`. Then `collect` (to free slots), the config, the attempts in flight, parked work and conflicts.
2. `plan_ready` and `spec_waves`: the ready tasks. It skips a task whose paths overlap one already picked, one with an open conflict, and stops at the free slots (`wip_cap` minus attempts in flight). It shows the set as a table with the skipped tasks and why.
3. `dispatch_wave`: each task gets a lease with its `paths` reserved, and a worker on a host slot.
4. A Monitor on `wavecoord events --follow --for lead`, so events reach the session.

In the demo, two tasks start; the third needs both and waits.

Task ids: the tree path `todo/due-dates/overdue/dates` is the coordinator task `todo.due-dates.overdue.dates` (dots for slashes) in leases, attempts, branches and the `hint` and `steer` commands.

### 3. What a worker does

```
! wavecoord attempts ls
! git worktree list
```

Each attempt is a `claude -p` in its own worktree (`../wt/<project>/<task>-<n>`) and branch (`wave/<task>-<n>`), with the task's prompt, its requirement and the Verify commands. It works, commits, runs its Acceptance and ends with one structured report (`done`, `blocked`, `failed` or `needs_decision`). It cannot push or open a PR: the launcher pushes the branch, and the lead opens the PR in `collect`.

### 4. The fence

```
! wavecoord fence status
```

The edit fence checks that a worker writes only paths its lease covers, at five points: E1 the edit hook, E2 pre-commit, E3 the push check, E4 the report, E5 gate-check and the refinery. Each is `off`, `advisory` (warn) or `enforce` (block). Roll it out in config PRs:

1. All `advisory` (the default). Run at least 3 waves and read the warnings: each is a lease that was too small or a worker that strayed.
2. `merge` and `push` to `enforce`.
3. `edit` to `enforce` (each host needs Claude Code 2.1.295 or later).
4. `commit` to `enforce`.

```json
{"fence": {"edit": "advisory", "commit": "advisory", "push": "enforce", "merge": "enforce", "lead": "ask"}}
```

In every mode but `off`, the refinery refuses a task PR that changes the lead config (`config_change`) or the spec tree (`spec_change`).

### 5. Wait for the reports

```
! wavecoord attempts ls
```

Each attempt shows its state (`reported`, `failed`, ...), turns and cost. Merging is triage: lesson 9.

## Common mistakes

- **A high `wip_cap` on the first wave.** Watch one small wave end to end first.
- **No test command in `claude_flags`.** The worker cannot run its Acceptance and waits on a permission prompt (`waiting_on_you`).
- **`git push` in `claude_flags`.** Not needed and not wanted: the launcher pushes, after its own push check.
- **Raising budgets to make a task pass.** A task that needs more is usually too big: split it (`/wavecoord:revise`).
- **`enforce` everywhere on day one.** Start advisory and learn what your leases miss.

## Exercise

On the practice repo with a plan (`python3 scripts/course/demos/demos.py brownfield /tmp/wavecoord-demo/todo --state planned --port 18741`):

1. Run `/wavecoord:wave-run --dry-run`. Which tasks would start, and why does the third wait?
2. Run the wave. While it runs, follow one worker with `wavecoord attempts logs <task>-1 -f`, and look at its worktree with `git worktree list`.
3. Add `"protected_paths_cmd": ["printf", "%s\n", "todo/store.py"]` in a config PR and run `/wavecoord:triage`. Which PR waits for you?

Next: [Lesson 8: Watching and steering](08-watching-steering.md).
