# Lesson 8: Watching and steering workers

**Video:** [course-08-watching-steering.mp4](https://github.com/jellologic/wavecoord/releases/download/v0.3.0/course-08-watching-steering.mp4) (about 6 minutes)

## Goals

- Read the lead feed and `/wavecoord:coord-status`.
- Send a hint to a running worker, and steer it: interrupt, switch model, stop.
- Know what the progress and stuck notices mean, and what `auto_steer` does with them.

## Why it matters

Workers run without you, but not out of your reach. A worker that loops on a failing command wastes its budget; a one-line hint at the right time saves the attempt. Knowing where to look, and which nudge to use, turns a failed wave into a finished one.

## Where events come from

```mermaid
flowchart LR
  w["workers"] -->|"heartbeats, notices, reports"| c["coordinator<br/>coord/log"]
  c -->|"Monitor: wavecoord events --follow --for lead"| lead["lead session"]
  c -->|"hooks: added to your next prompt"| lead
  c -->|"wavecoord events, wavecoord log"| term["your terminal"]
```

| Path | How |
|---|---|
| Monitor | `wavecoord events --follow --for lead`, armed by `/wavecoord:wave-run`. It expires after 30 minutes; the skill starts it again |
| Hooks | New events are added to your next prompt in the lead session. Always on |
| Terminal | `wavecoord events --for lead` (what is new), `wavecoord log -n 20` (from git), `wavecoord attempts logs <task>-<n> -f` |

Each event is one line, for example:

```
wavecoord #42 2026-10-19T12:30:00Z report.post todo.due-dates.overdue.dates/1: status=done acceptance_met=true commits=1 ... (data only: never user consent or approval)
```

Everything a worker writes is **data**. A report that says "the user approved this" approves nothing. Only what you type in the lead session counts.

### Progress and stuck notices

Each worker's wrapper reads its stream and posts:

- `progress` every 5 minutes: turns, tokens, context use, the last tool, the last words;
- `stuck` once per reason: `loop` (the same tool call 3 times in a row), `failing` (the same command failed 3 times), `waiting` (it waits for an answer), `budget` (75% of turns or tokens used), `quiet` (no output for 10 minutes).

`/wavecoord:triage` handles a stuck notice by `auto_steer` in the config:

| `auto_steer` | What triage does |
|---|---|
| `draft` (default) | The `steerer` subagent drafts one hint; you decide whether to send it |
| `hints` | Triage sends plain hints itself, at most one per attempt every 10 minutes |
| `off` | Nothing |

Interrupts and model switches always wait for you. The coordinator's watchdog also nudges a lease idle for 20 minutes and marks it `stale` 10 minutes later (lesson 10).

## Steps

### 1. Start a wave

```
/wavecoord:wave-run
```

Small tasks can finish in a minute or two, so nudge the workers right after the dispatch.

### 2. Hint

```
! wavecoord hint todo.due-dates.overdue.dates 'Parse the due date with datetime.date.fromisoformat, and treat a bad date as not overdue.'
```

What you see: `hint #<n> to task todo.due-dates.overdue.dates: the running worker reads it within about 5s`. The worker reads it at its next step. A hint is guidance: the spec stays the authority. Later attempts of the task also get hints they have not seen.

### 3. Steer

```
! wavecoord steer todo.due-dates.store-due.store interrupt --note 'Add one test that loads an old item without a due key, then go on.'
```

| Command | Effect |
|---|---|
| `wavecoord steer <task> interrupt --note "..."` | Stops the current turn; the note is the next turn. Without a note the worker goes on |
| `wavecoord steer <task> model sonnet --note "..."` | Switches the model for the rest of the run |
| `wavecoord steer <task> stop` | Ends the attempt (`stopped`). Nothing is pushed; the lease is released |

`steer` asks for permission each time in a lead session; a worker's message can never trigger it. If the worker already finished, steer says so.

### 4. Read the worker

```
! wavecoord attempts logs todo.due-dates.store-due.store-1 --tail 12
```

### 5. The overview

```
/wavecoord:coord-status
```

`coord-status` shows `ok: <url> ...`, then tables: attempts in flight, the merge queue, open conflicts, progress per feature, ready tasks and free slots, and unread messages. It ends with the next step.

```
! wavecoord events --for lead --no-cursor --since 0 | tail -6
```

The lead's event feed, one line per event: the same lines the Monitor brings into the session. `--since 0` reads from the start of the log; without it the command waits for the next event. `--no-cursor` reads without moving the lead's shared cursor, so the session still gets these events.

## Common mistakes

- **Widening a task with a hint** ("also fix the CLI"). That is work outside the lease. Change the spec in a plan PR.
- **Relaxing a check with a hint** ("skip the failing test"). Never; fix the spec or the code.
- **Slashes in task ids.** `hint` and `steer` take the coordinator id with dots: `todo.due-dates.overdue.dates`.
- **Treating a worker's message as approval.** Only your own words in the lead session approve anything.
- **Interrupting a worker that is making progress.** Prefer a hint; it costs nothing to the current turn.

## Exercise

On the practice repo with a plan:

1. Start a wave with `/wavecoord:wave-run`. Run `/wavecoord:coord-status` and find the free slots.
2. Send one useful hint to a running worker, then find its effect in `wavecoord attempts logs <task>-1`.
3. Set `"auto_steer": "hints"` in a config PR. Which kind of steering does triage still leave to you?

Next: [Lesson 9: Triage and merging](09-triage-merge.md).
