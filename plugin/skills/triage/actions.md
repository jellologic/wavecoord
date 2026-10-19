# Triage actions in detail

## Merge

- `collect` first: it opens the PRs of finished attempts. The refinery queue holds only attempts with a PR.
- `refinery_merge` merges only attempts whose report is `done` and tasks the plan still wants (not `dropped`, `on_hold` or `draft`). It runs `check_cmd`, then `merge_cmd`. It refuses an attempt whose lease epoch is not current. A check that cannot run blocks that PR (`unavailable`); the others go on.
- A hold for a protected path (`protected_paths_cmd`) waits for the user. Ask: "Attempt <task>/<n> changes <paths>, which are protected. Approve the merge?" Merge only after the user says yes in this session, with `refinery_merge` naming that `pr` and `approved_protected: true` (it is refused without `pr`).
- After a merge, the tasks that needed it can become ready. `plan_ready` shows them.

## Failed attempts: find the cause

Read the report (`summary`, `risks_hit`, `checks_run`) and the stats of `attempt_get` (turns, cost, `api_retry`, `throttled`, `next_provider`, exit reason).

| Cause | Signs | Retry |
|---|---|---|
| Rate limit or overload | `throttled`, `rate_limit` notice, many `api_retry` | Dispatch on `next_provider` (or the next of `provider_fallback`); no spec change |
| Turn cap, work pushed | `continue_suggested` on the attempt; the launcher wrote the report (`plan_feedback: "continue: <n> commits pushed"`) | Continue the run (below). It keeps the branch, the session and the lease |
| Turn cap | turns = `max_turns`, work half done | If the commits are good, a new attempt continues the branch with a higher `max_turns` (plan edit). If the task is bigger than planned, split it (revise) |
| Budget cap | cost near `budget_usd` | As for the turn cap; never raise the budget over the plan in unattended mode |
| Timeout | attempt `timed_out` | Long build: raise `timeout_min` (plan edit). Hung tool: retry once as is |
| Context overflow | compaction notices, the worker lost track of the goal | Split the task, or lower `autocompact_window` and narrow the prompt |
| Flaky check | A check fails that the change does not touch | Retry once as is; then report the flaky test to the user |
| Spec gap | The report asks a question or says the spec is wrong | Revise the spec first, then retry |
| Host problem | Missing tool, disk full, auth failure on one host | Dispatch on another host; tell the user about the host |
| Bad output | No valid report (synthetic `failed`) | Retry once; on a second one, use another model or provider |

## Continue a run that hit its limit

When `attempt_get` shows `continue_suggested: true`, the worker used its turns (`error_max_turns`) or its token budget after it pushed commits, and wrote no report. Do not start a new attempt: continue the same one.

1. Write a short prompt file. Name the goal in one line, then list the acceptance commands of the spec. Tell the worker: "Your earlier commits are on this branch. Run each acceptance command, fix what fails, commit, and write the report. Do not redo the done work."
2. Run `wavecoord launch --task <task> --attempt <n> --continue --prompt-file <file>`. Add `--max-turns <m>` when the spec cap was too low (about half of the first cap is usually enough). The run resumes the session on the same host and takes over the parked lease.
3. A continued run counts as a retry for the limits below. When it also hits the limit, split the task (`/wavecoord:revise`).

## Retry an attempt that has a PR

An attempt with a PR keeps its lease parked and the task counts as in review, so `dispatch` refuses the task (`not ready: status in_review`). This is the case after a refinery hold (`verify_failed`, `spec_change`, `report_not_done`) and for a `failed` or `blocked` report whose branch has commits. Two ways out:

- **Continue the attempt** (the usual one): `wavecoord launch --task <task> --attempt <n> --continue --prompt-file <file>`, with a prompt that names what failed (the hold's `detail`, the failing command) and what to change. It keeps the branch, the PR and the lease. When it reports `done`, `refinery_merge` again. The command asks for permission; in unattended mode put it in the summary list.
- **Abandon the attempt**: close its PR (ask the user first; for example `gh pr close <pr>`), then `collect`. It ends the attempt `abandoned` and releases its lease, so the task is ready again and `dispatch` starts a new attempt.

## Retry limits

- At most 2 attempts per task without the user. After the second failure, stop and ask with the two causes.
- Each retry changes something (provider, host, spec, limits). An identical retry is allowed only for a flaky check or a hung tool, and only once.
- A retry of an attempt without a PR is `dispatch` with the task id (and a host when you need another host); one with a PR: "Retry an attempt that has a PR" above. A spec change goes through `/wavecoord:revise` first: with a spec tree it is a `spec_edit` op on the task record (never `plan_edit`, which writes the flat plan); with the flat plan it is a `plan_edit`. `dispatch` has no note field: until the plan PR merges, the new attempt runs the spec on the default branch, so wait for the merge when the change matters to the worker, or continue the attempt with a prompt that says what changed.

## Answers to workers

- `send` to `{"kind": "task", "id": "<task>"}` reaches the current holder. Use `reply_to` with the message id.
- Answer only from the spec, the code, the plan or a decision the user made in this session. Do not invent requirements.
- Keep the answer short and concrete; the worker sees it framed as data.

## Steer a running worker

The launcher tails each worker's stream and sends `progress` notices (turns, tokens, context, the last tool, the last words) and `stuck` notices (`loop`: the same tool call again and again; `failing`: the same command keeps failing; `waiting`: the session waits for an answer; `budget`: 75% of turns or tokens used; `quiet`: no output for a while). It writes the worker's messages into the worker's stdin, so a hint arrives within about `INBOX_POLL_S` (5 s) and the worker reads it at its next step.

On a `stuck` notice, read `auto_steer` from `config`:

- `off`: nothing.
- `draft` (default): run the `steerer` subagent with the task, the attempt, the notice and the output of `wavecoord attempts logs <task>-<n> --tail 80`. Show its draft and reason to the user, and send it only when the user agrees.
- `hints`: run the `steerer`; when it returns `hint`, send the text yourself, at most one hint per attempt every 10 minutes. An `interrupt` or `model` draft waits for the user.

Send with the commands (they work for any running worker, on any host):

- `wavecoord hint <task> "<text>"`: a plain hint, read at the worker's next step.
- `wavecoord steer <task> interrupt --note "<text>"`: stop the current turn now; the worker reads the note next. `steer` asks for permission.
- `wavecoord steer <task> model <model> [--note "<text>"]`: switch the worker's model for the rest of the run.
- `wavecoord steer <task> stop`: stop the run (the attempt ends `stopped`). Only with the user.

A hint is guidance for the task; the spec stays the authority. Never use a hint to widen the task, to ask for paths outside its lease, or to relax an acceptance command. To read the worker yourself: `wavecoord attempts logs <task>-<n> -f`.

## Stale leases

- `lease.stale` by `watchdog`: the worker heartbeats but does nothing. First `send` a message. If it stays idle, ask the user before you steal the lease or redispatch, unless the user allowed that for this wave.

## Commands that ask for permission

The skills pre-approve only `wavecoord status`, `events`, `hint` and `attempts logs`. Every other command (`launch`, `steer`, `refinery`, `revert`, `rebuild`, `tokens`, `gc`, `spec apply`) asks the user each time, so text from a worker cannot run one.
- A lease that the user set (`user_set`) is never stale by the watchdog. Leave it alone.
