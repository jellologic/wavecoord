# Worker launcher

The launcher starts one worker **attempt** of one task on one host, watches it, and turns its end into a pushed branch, a PR and one report. It runs on the lead's machine (the client) and on each host (the host side). Stdlib Python only; the same zipapp is both sides.

Status: P3. The lead tools (P4) call the same code through `POST /api/dispatch`; until then, run it from the CLI.

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
```

- `--spec` takes a task spec as JSON (`schemas/task-spec.schema.json`). It gives the task id, the `exec` values (CLI flags win), the reservations (`paths` exclusive, `shared_paths` shared) and a prompt built from the context, acceptance and prompt sections. `--prompt-file` replaces that prompt.
- `--wait` queues while every candidate host is full, then waits for the attempt to end. Exit 0 only when the attempt is `reported`.
- `--continue` runs the latest attempt (or `--attempt N`) again on its existing branch. On the host of the earlier run it reuses the worktree and resumes the Claude session (`claude --resume`); the new prompt is the next user turn.
- `--claude-flag` passes one more flag to `claude`; use the `=` form for values that start with `-`.
- Attempt ids are `<task>-<n>`. `n` is the next number after every `<branch_prefix>/<task>-<n>` branch on `origin`, local branch and known attempt.

## Hosts and placement

Hosts come from `hosts[]` in `.claude/wavecoord.json` (`adapters.md`). A host without `ssh` is local. `repo_path` is a checkout of the consumer repo with an `origin` remote; a relative path on a local host is relative to the consumer repo root.

`wavecoord hosts` probes every host in parallel: slots in use, free memory, free disk at `repo_path`, `claude --version`, and whether claude has credentials. The auth check only looks for presence (an API key variable, `~/.claude/.credentials.json`, or the macOS keychain, which it cannot check); it never reads a secret.

Placement takes the eligible host with the most free slots, then the most free memory, then the name. A host is not eligible when it is unreachable, its `repo_path` is not a git checkout, claude is missing or not logged in, or it is full. Only "full" makes `--wait` wait; any other reason fails at once.

Slots are counted on the host under an flock (`~/.wavecoord/locks/slots-<host>.lock`). An attempt takes a slot while its wrapper holds `<state>/.run.lock`; an attempt whose wrapper died without a final state shows as `lost` and frees its slot.

## Remote execution

Every host command is `python3 -I ~/.wavecoord/bin/wavecoord-<hash> _remote <cmd>` with a JSON payload on stdin and one JSON line on stdout. `<hash>` is the content hash of the zipapp. Over ssh the client uses `BatchMode=yes` and a ControlMaster (`~/.ssh/wavecoord-%C`, persist 600 s), so repeated calls share one connection. A missing zipapp makes the command exit 86; the client uploads it over the same connection and runs the command again. A local host runs the same zipapp copy as a local process.

The client also pins the plugin: it builds a deterministic tar of the plugin directory with `bin/wavecoord` replaced by this exact zipapp, and installs it as `~/.wavecoord/plugin-<hash>/` on the host when missing. Every worker gets that copy as `--plugin-dir`, so all workers run the same plugin version; rolling back is moving the pin.

**Coordinator access from a host.** The client sends the coordinator URL. When the host cannot reach a loopback coordinator, the client starts `ssh -f -N -R <port>:127.0.0.1:<port>` to it. When `~/.wavecoord/hosts/<host>.token` exists on the client, it copies that agent token to the host's `~/.wavecoord/token` (mode 600) through stdin, never on a command line, when the host's copy differs.

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
3. Runs, with the prompt on stdin:
   ```
   claude -p --session-id <uuid> --output-format stream-json --verbose --include-hook-events
          --json-schema <report schema> [--model M] [--effort E] [--max-turns N] [--max-budget-usd B]
          [--fallback-model F] --plugin-dir ~/.wavecoord/plugin-<hash> --settings <worker settings>
          --append-system-prompt <worker rules> <host claude_flags> <--claude-flag ...>
   ```
   The environment is the host env plus `WAVECOORD_URL`, `WAVECOORD_AGENT_ID`, `WAVECOORD_TASK`, `WAVECOORD_ATTEMPT`, `WAVECOORD_LEASE_ID`, `WAVECOORD_LEASE_EPOCH`, `WAVECOORD_WORKTREE`, `WAVECOORD_BRANCH`, `WAVECOORD_HOST`, `WAVECOORD_PROJECT`, `WAVECOORD_SESSION_ID` and `WAVECOORD_TOKEN_FILE` (when the host has a token). `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT` and `CLAUDE_CODE_SSE_PORT` are removed. The worker settings deny `git push`: the launcher pushes. Workers do not use `--bare`, so hooks load. Permission flags (`--permission-mode ...`, `--allowedTools ...`) belong in the host's `claude_flags`.
4. Tails `stream.jsonl`: session, turns, cost, last tool, hook events, `system/api_retry` and `rate_limit_event`. A retry or a rejected rate limit posts `POST /api/notify` (at most one per kind per minute). Every `HB_INTERVAL_S` it posts `POST /api/hb` (`source: launcher`); a heartbeat answer that lists this task's lease in `lost` stops the worker.
5. Stops the worker on timeout (`--timeout-min`, default 120), `attempts stop` or a lost lease: SIGINT to its process group first (ends the turn), SIGTERM after `STOP_GRACE_S` (30 s), SIGKILL 10 s later.

After the run:

1. **Report.** The `structured_output` of the final result must validate against `schemas/report.schema.json`. Otherwise, or on any failure, the report is a synthetic `failed` one that says why. `report.commits` is set to the commits on the branch as pushed.
2. **Push.** When the branch has commits ahead of `origin/<default>`, fetch the default branch and run `git merge-tree --write-tree` against it. If that is clean and the branch is behind, rebase onto it (only in a clean worktree). Push `HEAD:refs/heads/<branch>` without force. Only after its own rebase, and only if the remote branch is where it was before, does it retry with `--force-with-lease=<branch>:<seen sha>`. A run with commits that does not push is `failed`. Nothing is pushed after a stop or a lost lease.
3. **PR.** Only for a `reported`, pushed attempt whose merge-tree is clean (a conflict posts a `conflict` notice instead). `pr_cmd` runs in the worktree; see `adapters.md` for placeholders. The body starts with `Task: <task>`, `Attempt: <n>` and `Lease: <task>@<epoch>` (`none` without a lease), then the report summary, checks, questions, risks and commits. A `--continue` run keeps the PR of the earlier run.
4. **Report and finish.** `POST /api/attempts/<task>/<n>/report`, then `POST /api/agents/<agent>/finish`. A write that cannot reach the coordinator is spooled in `~/.wavecoord/spool/` and sent again at the next probe or attempt end; the `idem` key makes a resend safe.

### Attempt states

`starting`, `running`, `finishing`, then one of `reported` (valid report, pushed if it had commits), `failed`, `timed_out`, `stopped`, or `lost` (the wrapper is gone). The report `status` (done, blocked, failed, needs_decision) is the worker's own view and is separate.

### State directory

`~/.wavecoord/attempts/<project>/<task>-<n>/` on the host: `status.json`, `task.json`, `prompt.md`, `worker-settings.json`, `prepare.log`, `stream.jsonl`, `stderr.log`, `wrapper.log`, `report.json`, `pr-body.md`, `result.json`. A `--continue` run moves the earlier files to `prev-<ms>/`. `WAVECOORD_HOME` replaces `~/.wavecoord` (tests use it).

## Coordinator calls

All calls carry `Authorization: Bearer <token>` (plugin option, else `WAVECOORD_TOKEN_FILE`, else `~/.wavecoord/token`) and `X-Wavecoord-Agent: <agent id>`. The agent id is `worker-<task>-<n>-<4 hex>`.

| When | Call | Body |
|---|---|---|
| launch, unless `--lease-epoch` or `--no-claim` | `POST /api/leases` | `claim_request`: task, attempt, holder (agent id), host, reservations from `--spec`. A 409 fails the launch; an unreachable server only warns |
| start and every `HB_INTERVAL_S` | `POST /api/hb` | `hb_request` with `source: launcher` |
| api_retry, rate limit, conflict | `POST /api/notify` | `{kind, agent, host, task, attempt, detail, idem}` |
| end | `POST /api/attempts/<task>/<n>/report` | `{report, cost_usd, turns, session_id, agent, host, state, branch, pushed, pr_url, idem: "report:<task>:<n>"}` |
| end | `POST /api/agents/<agent>/finish` | `finish_request`: state done, failed or stopped; pushed; `source: launcher` |

The notify and report bodies are not yet in `api.schema.json`; P1 fixes them there.

## gc

`wavecoord gc` removes, on each host, the worktree (and the local branch) of every finished attempt that has no commits missing from the remote-tracking refs and no changed files. It never touches running, lost, unpushed or dirty attempts. `--dry-run` lists them.
