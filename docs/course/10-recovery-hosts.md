# Lesson 10: Recovery and more hosts

**Video:** [course-10-recovery-hosts.mp4](https://github.com/jellologic/wavecoord/releases/download/v0.3.0/course-10-recovery-hosts.mp4) (about 4 minutes)

## Goals

- Stop a worker, and release a lease that nobody holds any more.
- Undo coordination state: revert one event, rebuild the cache, roll back to a commit of the log, and fix a task's status after a rollback.
- Use the fence's offline override when the coordinator is down.
- Add an SSH worker host, and know where providers fit.

## Why it matters

Machines die, networks drop and people make mistakes. Because every coordination event is a commit on `coord/log`, and the plan and the code are in git too, every one of these has a precise undo. Knowing them lets you run bigger waves without fear.

## Steps

### 1. See what happened

```bash
wavecoord log -n 4
```

`log` reads `coord/log` from git; it needs no server. Each event has a sequence number (`seq`).

### 2. Stop a worker, and release a lease

```bash
wavecoord dispatch todo.due-dates.overdue.dates
wavecoord attempts ls --active
wavecoord steer todo.due-dates.overdue.dates stop
```

`steer ... stop` (or `wavecoord attempts stop <task>-<n>`) ends the attempt as `stopped`. Nothing is pushed, the commits stay in the worktree, and the lease is released, so the task is ready again.

When a worker's machine dies, its attempt is `lost` and its lease turns `stale`. A stale lease still holds the task: release it, then dispatch again.

```bash
wavecoord lease release todo.due-dates.overdue.dates --reason "worker lost"
```

It releases only a `stale` or `parked` lease; an active one needs `--force` (prefer `steer ... stop`). In the lead session, triage does the same with the `lease_release` tool, after telling you.

| Lease state | Means | What to do |
|---|---|---|
| `active` | A worker holds it | Nothing; `steer stop` to end it |
| `parked` | The work waits in a PR; its paths stay reserved | Merge or close the PR; `collect` then releases it |
| `stale` | No activity for the TTL, or the watchdog marked it | Hint the worker; if it is gone, `lease release` |

### 3. Undo coordination state

| You want to | Command |
|---|---|
| Undo one event (a message, a notice, a proposal, a dispatch request, a conflict) | `wavecoord revert <seq> --reason "<why>"` |
| Rebuild the state cache from the log (changes nothing) | `wavecoord rebuild` |
| Roll all state back to a commit of `coord/log` | `wavecoord rebuild --at <sha> --reason "<why>"` |
| Put a task that the rollback un-merged back to `planned` | `wavecoord spec status-sync --reset <task>` |
| Write task statuses into the spec tree now | `wavecoord spec status-sync` |
| Undo a plan change | `git revert` the plan PR |
| Undo merged work | `git revert` the merge; the task stays `done`, so plan a new task to redo it |

`rebuild --at` keeps history: it appends a revert event, releases the leases granted after that commit (their workers stop), and never moves epochs back. The spec tree still says `done` for a task whose merge you rolled back; `status-sync --reset` fixes that, because the coordinator now has no merged attempt for it. Details: [rollback.md](../rollback.md).

### 4. The fence when the coordinator is down

With the fence in `enforce`, a worker cannot write when the coordinator is down. Only you can let it go on, from your own terminal:

```bash
wavecoord fence offline --for 1h --reason "coordinator maintenance"
wavecoord fence status
wavecoord fence online
```

`offline` refuses while the coordinator answers, and inside a Claude Code session. It lasts at most 4 hours; edit and commit checks that cannot reach the coordinator pass and are logged, and push and merge checks still need the coordinator. `online` ends it and sends the log.

### 5. Add an SSH host

1. Prepare the machine: `git`, `python3`, `claude` logged in, and a checkout of the repo with an `origin` remote. The lead machine must reach it with key-based `ssh`.
2. Add it to `hosts` in a config PR:
   ```json
   {"name": "build-1", "ssh": "builder@build-1.example.internal", "slots": 4, "repo_path": "~/src/myapp",
    "claude_flags": ["--permission-mode", "acceptEdits",
                     "--allowedTools", "Bash(git add *),Bash(git commit *),Bash(git merge *),Bash(python3 -m unittest *)"]}
   ```
3. Give it its own token:
   ```bash
   wavecoord tokens provision --role agent --host build-1 --out ~/.wavecoord/hosts/build-1.token
   ```
4. Probe it: `wavecoord hosts` shows slots, memory, disk, the Claude Code version and whether it can log in. `wavecoord doctor` checks its token and probes it too.

The launcher uploads the pinned program and plugin, copies the token over ssh, and opens an `ssh -R` tunnel so the worker reaches the coordinator. `labels` on a host and `exec.host_needs` on a task pin tasks to capable hosts; `"dispatch": false` keeps a host out of automatic placement. Details: [launcher.md](../launcher.md).

### Providers

A provider profile runs a host's workers against another Anthropic-compatible endpoint. The config names it and points to an env file that lives only on the host (mode 600); a task picks it with `exec.provider` and lists fallbacks in `exec.provider_fallback`, used after a rate limit. See [providers.md](../providers.md).

## Common mistakes

- **Releasing an active lease with `--force`** while its worker still runs. Two workers may then write the same paths. Stop it first.
- **Reverting a merge and dispatching the same task.** It stays `done`. Plan a new task.
- **Leaving the fence offline.** End it with `wavecoord fence online` as soon as the coordinator is back.
- **Sharing the lead token with a host.** Each host gets its own agent token; workers never get the lead token.

## Exercise

On the practice repo with a plan:

1. Dispatch one task, stop it, and find both events in `wavecoord log -n 10`.
2. Send a hint to a task that is not running, find its `seq`, and revert it with `wavecoord revert <seq> --reason "test"`.
3. Stop the coordinator, run `wavecoord fence offline --for 10m --reason test`, read `wavecoord fence status`, then start the coordinator and run `wavecoord fence online`.
4. Write the `hosts` entry for a second machine you could use, without merging it.

You finished the course. Keep [the guide](../guide.md) at hand: it has the details of every step.
