# Triage actions in detail

## Merge

- `refinery_merge` runs `check_cmd`, then `merge_cmd`. It refuses an attempt whose lease epoch is not current.
- A hold for a protected path (`protected_paths_cmd`) waits for the user. Ask: "Attempt <task>/<n> changes <paths>, which are protected. Approve the merge?" Merge only after the user says yes in this session.
- After a merge, the tasks that needed it can become ready. `plan_ready` shows them.

## Failed attempts: find the cause

Read the report (`summary`, `risks_hit`, `checks_run`) and the stats of `attempt_get` (turns, cost, `api_retry`, `throttled`, `next_provider`, exit reason).

| Cause | Signs | Retry |
|---|---|---|
| Rate limit or overload | `throttled`, `rate_limit` notice, many `api_retry` | Dispatch on `next_provider` (or the next of `provider_fallback`); no spec change |
| Turn cap | turns = `max_turns`, work half done | If the commits are good, a new attempt continues the branch with a higher `max_turns` (plan edit). If the task is bigger than planned, split it (revise) |
| Budget cap | cost near `budget_usd` | As for the turn cap; never raise the budget over the plan in unattended mode |
| Timeout | attempt `timed_out` | Long build: raise `timeout_min` (plan edit). Hung tool: retry once as is |
| Context overflow | compaction notices, the worker lost track of the goal | Split the task, or lower `autocompact_window` and narrow the prompt |
| Flaky check | A check fails that the change does not touch | Retry once as is; then report the flaky test to the user |
| Spec gap | The report asks a question or says the spec is wrong | Revise the spec first, then retry |
| Host problem | Missing tool, disk full, auth failure on one host | Dispatch on another host; tell the user about the host |
| Bad output | No valid report (synthetic `failed`) | Retry once; on a second one, use another model or provider |

## Retry limits

- At most 2 attempts per task without the user. After the second failure, stop and ask with the two causes.
- Each retry changes something (provider, host, spec, limits). An identical retry is allowed only for a flaky check or a hung tool, and only once.
- A retry is `dispatch` with the task id (and a host when you need another host). A spec change goes through `plan_edit` first. Until the plan PR merges, say in the dispatch note what changed.

## Answers to workers

- `send` to `{"kind": "task", "id": "<task>"}` reaches the current holder. Use `reply_to` with the message id.
- Answer only from the spec, the code, the plan or a decision the user made in this session. Do not invent requirements.
- Keep the answer short and concrete; the worker sees it framed as data.

## Stale leases

- `lease.stale` by `watchdog`: the worker heartbeats but does nothing. First `send` a message. If it stays idle, ask the user before you steal the lease or redispatch, unless the user allowed that for this wave.
- A lease that the user set (`user_set`) is never stale by the watchdog. Leave it alone.
