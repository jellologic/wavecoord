# Worker launcher

The launcher starts one worker **attempt** of one task on one host, watches it, and turns its end into a pushed branch and one report. The PR is opened on the lead's machine, never on the host. It runs on the lead's machine (the client) and on each host (the host side). Stdlib Python only; the same zipapp is both sides.

Status: P4. The lead tools (`dispatch`, `dispatch_wave`, `resolve`, `collect`; contract section 8) call the same code in the server process. The CLI below still works on its own.

## Commands

```
wavecoord launch --task T --prompt-file F [--host H] [--model M] [--effort E] [--max-turns N]
                 [--max-budget-usd B] [--fallback-model F] [--timeout-min M] [--attempt N]
                 [--continue] [--wait] [--json] [--spec SPEC.json]
                 [--lease-id ID --lease-epoch E | --no-claim] [--claude-flag=--X ...]
wavecoord hosts [--host H] [--json]
wavecoord attempts ls [--active] [--host H] [--json]
wavecoord attempts logs <task>-<n> [-f] [--tail N] [--raw]
wavecoord attempts result <task>-<n> [--json]
wavecoord attempts stop <task>-<n>
wavecoord gc [--host H] [--dry-run]
wavecoord hint <task> <text|->
wavecoord steer <task> interrupt|model <M>|stop [--note TEXT]
```

- `--spec` takes a task spec as JSON (`schemas/task-spec.schema.json`). It gives the task id, the `exec` values (CLI flags win), the reservations (`paths` exclusive, `shared_paths` shared) and a prompt built from the context, acceptance and prompt sections. `--prompt-file` replaces that prompt. A task of the spec tree (`specs.md`) also puts its requirement (statement and scenarios), its inputs and outputs with their contracts, and the requirement's Verify commands into the prompt, and ends the PR body with the trailers `Spec-Task: <tree id>` and `Spec-Basis: <fingerprint>` (a squash merge keeps them in the commit).
- `--wait` queues while every candidate host is full, then waits for the attempt to end. Exit 0 only when the attempt is `reported`.
- `--continue` runs the latest attempt (or `--attempt N`) again on its existing branch. On the host of the earlier run it reuses the worktree and resumes the Claude session (`claude --resume <id>`, never together with `--session-id`); the new prompt is the next user turn. `--resume` keeps writing into the transcript of the resumed id, so the launcher resumes the attempt's lineage, not the fresh id of the last run: the session whose transcript the last run wrote (`transcript_session` in `task.json`), then the first run's id, then the ids of the runs in `prev-*/`, oldest first. The wrapper resumes the first of these whose transcript file exists in `$CLAUDE_CONFIG_DIR/projects/<encoded worktree path>/<id>.jsonl` (default `~/.claude`; the encoded path has each character that is not a letter or a digit replaced by `-`), with the provider's environment. When no transcript exists, or claude ends with `No conversation found with session ID`, the wrapper starts a fresh session in the same worktree (one time). Its prompt is a short note with the branch's commits since the base, then the first run's full prompt, then the continue prompt (once, when they are the same, as with `--continue --spec`). The result, `status.json` and `task.json` record it in `session_fallback` (`reason`: `no_transcript`, `no_session` or `no_conversation`), and the files of the failed resume stay as `resume-failed-stream.jsonl` and `resume-failed-stderr.log`. The next `--continue` resumes the fresh session. A run with or without `--spec` keeps the same lineage. The run gets a new agent id and a new lease: it claims with `resume`, so it takes over the parked lease of the attempt (a failed run that pushed commits parks its lease) with its reservations. Without `--spec`, the claim names the reservations of the earlier run (from the client index). The wrapper runs `guard install` again, so the git guard checks as the new agent, lease and epoch.
- `--provider P` runs the worker with provider profile `P` (default: `exec.provider` of `--spec`, then `default_provider`, then `anthropic`); `--provider-fallback A,B` records the profiles to use next. See `providers.md`.
- `--claude-flag` passes one more flag to `claude`; use the `=` form for values that start with `-`.
- Attempt ids are `<task>-<n>`. `n` is the next number after every `<branch_prefix>/<task>-<n>` branch on `origin`, local branch and known attempt.

## Hosts and placement

Hosts come from `hosts[]` in `.claude/wavecoord.json` (`adapters.md`). A host without `ssh` is local. `repo_path` is a checkout of the consumer repo with an `origin` remote; a relative path on a local host is relative to the consumer repo root.

`wavecoord hosts` probes every host in parallel: slots in use, free memory, free disk at `repo_path`, `claude --version`, and whether the host can authenticate a worker of `default_provider`. AUTH is `provider:<name>` when that profile's env file is on the host with mode 600 (no claude login is needed), else the claude login check: `logged-in`, `env`, `keychain?` or `no`. The login check only looks for presence (an API key variable, `~/.claude/.credentials.json`, or the macOS keychain, which it cannot check); it never reads a secret. `--json` adds `auth` (the same label) and `dispatch` to each host; `claude.auth` stays the login check.

Placement works per attempt. The attempt's providers are its provider (`exec.provider`, else `default_provider`), then its `provider_fallback` in order, less any the lead has backed off after a rate limit. A host can take the attempt when its `providers` list (`adapters.md`) has one of them and the host can authenticate it: for built-in `anthropic` a claude login; for any other profile the profile's env file, present with mode 600, and no login. The host runs the attempt with the first such provider; the providers after it stay the attempt's fallbacks.

Placement takes the primary provider whenever an eligible host can run it: hosts are ranked by provider first (the attempt's provider, then each fallback in order), and only within one provider rank by the most free slots, then the most free memory, then the name. So a fallback provider is used only when no eligible host can run an earlier one (no file, no login, unreachable or full). A host is not eligible when it is unreachable, its `repo_path` is not a git checkout, claude is missing, it cannot authenticate any of the attempt's providers, or it is full. Only "full" makes `--wait` wait; any other reason fails at once. A host with `dispatch: false` is left out of automatic placement; `--host <name>` (or the `host` of the `dispatch` tool) still places an attempt there. The `dispatch.decide` reason names the hosts that were skipped and why, so an unreachable host shows in `coord/log`.

Slots are counted on the host under an flock (`~/.wavecoord/locks/slots-<host>.lock`). An attempt takes a slot while its wrapper holds `<state>/.run.lock`; an attempt whose wrapper died without a final state shows as `lost` and frees its slot.

## Remote execution

Every host command is `python3 -I ~/.wavecoord/bin/wavecoord-<hash> _remote <cmd>` with a JSON payload on stdin and one JSON line on stdout. `<hash>` is the content hash of the zipapp. Over ssh the client uses `BatchMode=yes` and a ControlMaster (`~/.ssh/wavecoord-%C`, persist 600 s), so repeated calls share one connection. A missing zipapp makes the command exit 86; the client uploads it over the same connection and runs the command again. A local host runs the same zipapp copy as a local process.

The client also pins the plugin: it builds a deterministic tar of the plugin directory with `bin/wavecoord` replaced by this exact zipapp, and installs it as `~/.wavecoord/plugin-<hash>/` on the host when missing. Every worker gets that copy as `--plugin-dir`, so all workers run the same plugin version; rolling back is moving the pin.

**Coordinator access from a host.** The client sends the coordinator URL. When the host cannot reach a loopback coordinator, the client starts `ssh -f -N -R <port>:127.0.0.1:<port>` to it. When `~/.wavecoord/hosts/<host>.token` exists on the client, it copies that agent token to the host's `~/.wavecoord/token` (mode 600) through stdin, never on a command line, when the host's copy differs. A local host shares `~/.wavecoord` with the client, so nothing is copied: the wrapper uses `~/.wavecoord/hosts/<host>.token` directly (`WAVECOORD_TOKEN_FILE`), so local workers do not use the lead's `~/.wavecoord/token` (`security.md`). When that file is missing and the tokens file is on the client, the launcher provisions an agent token for the host first (0.2.1). On a remote host the probe asks the coordinator to prove itself (`security.md`, "Remote hosts"); when another process answers on the port, the launch on that host is refused.

## Attempt lifecycle (host side)

`_remote launch` runs while the client waits:

1. Check the slot (under the slot lock) and write `status.json` (`starting`).
2. `git fetch origin --prune` under the worktree-admin lock (`<git-common-dir>/wavecoord-wt-admin.lock`), with two retries for transient errors.
3. Refuse a protected (`main`, `master`, `trunk`, `develop`, `HEAD`), default, log or malformed branch name; refuse a branch that is already checked out; refuse an existing branch unless `--continue`.
4. Create the worktree `<repo_path>/../wt/<task>-<n>` on `<branch_prefix>/<task>-<n>` from `origin/<default branch>`.
5. Start the detached wrapper (`_remote wrap`, its own session, output to `wrapper.log`) and return when it runs. It survives the end of the ssh connection.

The wrapper then:

1. Sends a first heartbeat, then runs `prepare_cmd` in the worktree. A non-zero exit fails the attempt before any model call.
2. Runs `wavecoord guard install` in the worktree (the git guards, P2). A build without the command skips it; a guard that fails to install fails the attempt.
3. Runs, with the prompt as the first stream-json user line on stdin (stdin stays open; "Listen and steer" below):
   ```
   claude -p --session-id <uuid> --input-format stream-json --output-format stream-json --verbose --include-hook-events
          --json-schema <report schema> [--model M] [--effort E] [--max-turns N] [--max-budget-usd B]
          [--fallback-model F] --plugin-dir ~/.wavecoord/plugin-<hash> --settings <worker settings>
          --append-system-prompt <worker rules> <host claude_flags> <--claude-flag ...>
   ```
   The `claude` child alone also gets the provider profile (`providers.md`): the parent's `ANTHROPIC_*` variables are removed and the host's env file is loaded. The environment is the host env plus `WAVECOORD_URL`, `WAVECOORD_AGENT_ID`, `WAVECOORD_TASK`, `WAVECOORD_ATTEMPT`, `WAVECOORD_LEASE_ID`, `WAVECOORD_LEASE_EPOCH`, `WAVECOORD_WORKTREE`, `WAVECOORD_BRANCH`, `WAVECOORD_HOST`, `WAVECOORD_PROJECT`, `WAVECOORD_SESSION_ID`, `WAVECOORD_ATTEMPT_DIR`, `CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS=1`, `WAVECOORD_FENCE` (the fence modes of `origin/<default>`) and `WAVECOORD_TOKEN_FILE` (only the host's agent token: `<wavecoord home>/hosts/<host>.token`, or on a remote host the installed token; never the lead's token). `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_SSE_PORT`, `CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_CODE_MESSAGING_TOKEN` (the inbox of a Claude session that started the launch), `CLAUDE_PLUGIN_OPTION_TOKEN` and the other token variables are removed. The worker settings deny `git push` (the launcher pushes) and pin `disableAllHooks: false` and the fence env (`hooks.md`, "The edit fence"). In fence enforce mode the wrapper refuses a run with no lease, and for `edit` enforce a claude older than 2.1.295 or a user mod that handles `tool.check` (the attempt fails with a `fence: refused` note). Workers do not use `--bare`, so hooks load. Permission flags (`--permission-mode ...`, `--allowedTools ...`) belong in the host's `claude_flags`.
4. Tails `stream.jsonl`: session, turns, cost, last tool, hook events, `system/api_retry` and `rate_limit_event`. A retry or a rejected rate limit posts `POST /api/notify` (at most one per kind per minute). Every `HB_INTERVAL_S` it posts `POST /api/hb` (`source: launcher`); a heartbeat answer that lists this task's lease in `lost` stops the worker. It also listens and steers (below).
5. Stops the worker on timeout (`--timeout-min`, default 120), `attempts stop`, a lead `steer stop` or a lost lease: SIGINT to its process group first (ends the turn), SIGTERM after `STOP_GRACE_S` (30 s), SIGKILL 10 s later.

After the run:

1. **Report.** The `structured_output` of the final result must validate against `schemas/report.schema.json`. Otherwise, or on any failure, the report is a synthetic `failed` one that says why. `report.commits` is set to the commits on the branch as pushed. When the run hit its turn limit (result `error_max_turns`) or its token budget (`token_budget`) and the launcher pushed commits, the synthetic report lists the commits and the changed files, has `checks_run` empty and `plan_feedback: "continue: <n> commits pushed"`, and the report request (and the attempt) gets `continue_suggested: true`. The lead then continues the run with `--continue` (`/wavecoord:triage`). The worker rules also tell the worker to stop, commit and write the report when fewer than 15% of its turns remain.
2. **Push.** When the branch has commits ahead of `origin/<default>`, fetch the default branch and run `git merge-tree --write-tree` against it. If that is clean and the branch is behind, rebase onto it (only in a clean worktree). Then the fence push check (E3): the net diff `merge-base(origin/<default>, HEAD)..HEAD` (`--no-renames`), checked with the attempt's agent, task and epoch; in `push` enforce mode a violation, an unavailable coordinator or an unsent override journal means no push (`push failed: fence: <kind>`). The diff also goes in the report as `changed_paths` (E4). Push `HEAD:refs/heads/<branch>` without force, with the worker env (so the pre-push guard sees the attempt's identity). Only after its own rebase, and only if the remote branch is where it was before, does it retry with `--force-with-lease=<branch>:<seen sha>`. A run with commits that does not push is `failed`. Nothing is pushed after a stop or a lost lease.
3. **PR readiness.** The host never runs `pr_cmd`. For a `reported`, pushed attempt whose merge-tree is clean, the result has `pr_ready: true`, `pr_title` and `pr_body` (also in `pr-body.md`). The body starts with `Task: <task>`, `Attempt: <n>` and `Lease: <task>@<epoch>` (`none` without a lease), then the report summary, checks, questions, risks and commits. A conflict posts a `conflict` notice and sets `pr_error` instead. The client then opens the PR (below).
4. **Report and finish.** `POST /api/attempts/<task>/<n>/report`, then `POST /api/agents/<agent>/finish`. A write that cannot reach the coordinator is spooled in `~/.wavecoord/spool/` and sent again at the next probe or attempt end; its `Idempotency-Key` makes a resend safe.

### Listen and steer

The wrapper is the worker's parent, so it reads the worker's stream and writes the worker's stdin (`--input-format stream-json`, the Agent SDK's streaming input). Nothing here calls a model.

**Listen.** From the stream it keeps a digest (turns, tokens, the last tool calls, the last text of the main conversation, the edited files, failing commands, the session state, the context use) and posts notices (`POST /api/notify`, kind `other`, `detail.event` `progress` or `stuck`, `detail.digest`):

| Notice | When |
|---|---|
| `progress` | every `PROGRESS_S` (300 s; 0 turns it off): `turns <n>/<max>, tokens, context <p>%, last: <tool>, says: <text>`. The wrapper asks for the context use with a `get_context_usage` control request |
| `stuck: loop` | the same tool call (name and command or path) 3 times in a row |
| `stuck: failing` | the same Bash command failed 3 times |
| `stuck: waiting` | `system/session_state_changed` is `requires_action`: the session waits for an answer nobody gives |
| `stuck: budget` | 75% of `max_turns` or `max_tokens` used |
| `stuck: quiet` | no stream line for `QUIET_S` (600 s) |

Each `stuck` reason is sent once per run. The lead sees the notices as event lines; `/wavecoord:triage` acts on them (`auto_steer`, `adapters.md`).

**Steer.** Every `INBOX_POLL_S` (5 s), and with each heartbeat answer, the wrapper reads the worker's inbox (`GET /api/inbox`). New messages go into stdin as one user line with `priority: "next"`: a header that says they are data, then each message in the frame of `contract.md` section 4. Claude Code reads it in the same turn once the running tools finish. Each message is then acked with the key `ack:<agent>:<id>`, the key of the hooks, so the hooks never show it again; a message that could not be written stays unacked, and the hooks deliver it. `steer.jsonl` in the state directory records what went in (ids, priority, control requests; never the text).

A message from the lead whose body is `wavecoord:` and a JSON object is a steer action (`wavecoord steer` writes them):

| Action | What the wrapper does |
|---|---|
| `{"action":"interrupt","note":"..."}` | a `control_request` `interrupt` (Claude Code stops the running tool call and ends the turn), then the note as a queued user line: it runs as the next turn. A user line with `priority: "now"` alone does not stop a running tool call (checked with Claude Code 2.1.303) |
| `{"action":"model","model":"M","note":"..."}` | a `control_request` `set_model`, then the note as a hint |
| `{"action":"stop","note":"..."}` | the stop path of `attempts stop`: the attempt ends `stopped`, note `stopped by the lead (steer #<id>)` |

The same body from any other sender is plain text: only the lead steers.

**End of the run.** Claude Code ends a stream-json run when stdin closes. After each `result` line the wrapper closes stdin, unless a turn is still coming: Claude Code has queued turns (`queued_turn_count`), or a user line the wrapper sent is not yet in any result's `user_message_uuids` (the note of an interrupt), or new messages arrived (they go in, and the worker gets one more turn, at most 3 per run, only after a `success` result and within the run's limits). When no stream line follows a result for `STEER_WAIT_S` (60 s), the wrapper closes stdin anyway. `wavecoord attempts logs <task>-<n> -f` shows the live stream.

**Sessions that the launcher did not start.** A worker session that a person started (`WAVECOORD_TASK` set, no `WAVECOORD_ATTEMPT_DIR`) gets its messages through Claude Code's inbox socket instead (`hooks.md`, "Inbox relay").

### Attempt states

`starting`, `running`, `finishing`, then one of `reported` (valid report, pushed if it had commits), `failed`, `timed_out`, `stopped`, or `lost` (the wrapper is gone). The report `status` (done, blocked, failed, needs_decision) is the worker's own view and is separate.

### State directory

`~/.wavecoord/attempts/<project>/<task>-<n>/` on the host: `status.json`, `task.json`, `prompt.md`, `worker-settings.json`, `prepare.log`, `stream.jsonl`, `steer.jsonl`, `stderr.log`, `wrapper.log`, `report.json`, `pr-body.md`, `result.json`. A `--continue` run moves the earlier files to `prev-<ms>/`. `WAVECOORD_HOME` replaces `~/.wavecoord` (tests use it).

## Coordinator calls

Writes carry an `Idempotency-Key` header (as in the hooks client). All calls carry `Authorization: Bearer <token>` (plugin option, else `WAVECOORD_TOKEN_FILE`, else `~/.wavecoord/token`) and `X-Wavecoord-Agent: <agent id>`. The agent id is `worker-<task>-<n>-<4 hex>`.

| When | Call | Body |
|---|---|---|
| launch, unless `--lease-epoch` or `--no-claim` | `POST /api/leases` | `claim_request`: task, attempt, holder (agent id), host, reservations from `--spec`. A 409 fails the launch; an unreachable server only warns |
| start and every `HB_INTERVAL_S` | `POST /api/hb` | `hb_request` with `source: launcher` |
| api_retry, rate limit, StopFailure, conflict, progress, stuck | `POST /api/notify` | `notify_request`: kind `rate_limit`, `api_error` or `other`; `detail` has the event, provider and host (and `why`, `digest` for progress and stuck) |
| every `INBOX_POLL_S`, and after each result | `GET /api/inbox?agent=<agent>` | the worker's messages, written into its stdin |
| after a message went into stdin | `POST /api/messages/<id>/ack` | key `ack:<agent>:<id>` |
| end | `POST /api/attempts/<task>/<n>/report` | `{report, cost_usd, cost_basis, usage, model_usage, turns, session_id, agent, host, provider, throttled, throttle, next_provider, state, branch, pushed, pr_url}`; key `report:<task>:<n>` |
| end | `POST /api/agents/<agent>/finish` | `finish_request`: state done, failed or stopped; pushed; `source: launcher` |

The report body is `report_request` in `api.schema.json`.

## Client side: PRs, the collector and the dispatcher

The lead's machine opens every PR with `pr_cmd` in the consumer repo root (`adapters.md`, "PR command"):

- `wavecoord launch --wait` opens the PR after the wait and tells the coordinator (`POST /api/attempts/<task>/<n>/end`, best effort; it needs a lead token).
- `wavecoord collect` (and the `collect` tool) reads the client index (`~/.wavecoord/client/<project>/<task>-<n>.json`), asks each host for its finished attempts, and per attempt: resends the report when the coordinator does not have it (same idempotency key), opens the PR or logs `conflict.open` for a merge-tree conflict, logs `attempt.end`, ends a resolver run (`resolve.end`, `conflict.close` when it is clean), dispatches a resolver when `auto_resolve` is on, and starts the next attempt after a rate-limited failure through the normal readiness check (`providers.md`). Each attempt is collected once; a `--continue` run makes it collectable again and keeps its PR.
- `wavecoord dispatch --loop [--interval S] [--dry-run]` repeats: collect, then dispatch the ready wave (`dispatch_wave`). `--dry-run` only prints what it would do.

Dispatch (`dispatch`, `dispatch_wave`) claims the lease on the coordinator first (the spec's `paths` exclusive, `shared_paths` shared, holder `worker-<task>-<n>-<4 hex>`), then launches with the spec's `exec` values on the hosts whose name or `labels` cover `exec.host_needs`. It respects `wip_cap` (attempts `dispatched` or `running`). `dispatch_wave` counts each task that it placed in the wave before the next one, also in a dry run. It picks the first provider of `[exec.provider] + exec.provider_fallback` that a candidate host has and that is not backed off: a provider is backed off for `PROVIDER_BACKOFF_S` after a throttled attempt or a `rate_limit` notice that names it. A launch failure releases the lease again. A resolver (`resolve`) is a `--continue` run of the same attempt: it steals the parked lease (a new epoch), and its report has its own idempotency key `report:<task>:<n>:<agent>`.

## gc

`wavecoord gc` removes, on each host, the worktree (and the local branch) of every finished attempt that has no commits missing from the remote-tracking refs and no changed files. It never touches running, lost, unpushed or dirty attempts. `--dry-run` lists them.
