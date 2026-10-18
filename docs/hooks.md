# Hooks

All hooks are command hooks that run `${CLAUDE_PLUGIN_ROOT}/bin/wavecoord hook <Event>` in exec form (`args`, no shell). The hook reads the event JSON on stdin and prints at most one JSON object on stdout. The code is `src/wavecoord/hooks.py`; the REST client is `src/wavecoord/client.py`.

**Rule 1: nothing happens unless coord is configured.** Without a coordinator URL, every hook prints nothing and exits 0. An unreachable coordinator also gives no output, except that writes go to the spool (below).
**Rule 2: a hook never breaks a session**, except the fence's deliberate denial (P6). Bad input, a timeout or a server error exit 0 silently. Each event has a hard time budget below its hook timeout: when the budget ends, the hook prints nothing and exits 0.
**Rule 3: messages are data.** Injected text uses the frame in `contract.md` section 4. The body is a JSON string, so a message cannot break out of its line.

## Configuration

| Item | Source, in order |
|---|---|
| Coordinator URL | `WAVECOORD_URL`, then the plugin option `coord_url` (`CLAUDE_PLUGIN_OPTION_COORD_URL`), then `coord_url` in `.claude/wavecoord.json`, found from the hook's `cwd` upward |
| Token | The plugin option `token` (`CLAUDE_PLUGIN_OPTION_TOKEN`), then the file `WAVECOORD_TOKEN_FILE`, then `~/.wavecoord/token` |
| Agent id | `lead` when `WAVECOORD_ROLE=lead`, else `WAVECOORD_AGENT_ID`, else `claude:<session_id>` |
| Agent kind | `lead` when `WAVECOORD_ROLE=lead`, else `WAVECOORD_AGENT_KIND`, else `worker` when `WAVECOORD_TASK` is set, else `other` |
| Host, task, attempt | `WAVECOORD_HOST` (default `local`), `WAVECOORD_TASK`, `WAVECOORD_ATTEMPT` |
| State and spool | `WAVECOORD_STATE_DIR`, else `${CLAUDE_PLUGIN_DATA}/state`, else `~/.wavecoord/state` |

Every request has `Authorization: Bearer <token>`, `X-Wavecoord-Agent: <agent id>` and, for a write that must not repeat, `Idempotency-Key: <key>`. A request waits at most `CLIENT_TIMEOUT_S` and never past the event's budget.

**State.** One small JSON file per agent keeps the heartbeat `seq`, the time of the last heartbeat, the last lease from a heartbeat, the ids of messages already shown, the hot files already marked and the running subagents.

**Spool.** A write that cannot reach the coordinator (ack, reservation, notice, finish) goes to `spool.jsonl` in the state directory. Heartbeats and reads are not spooled. The next hook run that reaches the coordinator sends up to 20 spooled writes, oldest first, with their original agent and idempotency key. An entry leaves the spool when the server answers it with any status.

## Events

| Event | Matcher | Mode | Timeout (s) | Budget (s) | Behavior | Milestone |
|---|---|---|---|---|---|---|
| `SessionStart` | | sync | 10 | 8 | Heartbeat (registers the agent). Worker: task, spec title, lease and epoch, own paths, peers, paths reserved by others, hot files, inbox, as `additionalContext`. Lead: wave state from `GET /api/live` | P2 |
| `UserPromptSubmit` | | sync | 10 | 6 | Deliver messages not yet shown, and lost-lease notices, as `additionalContext`; ack them; heartbeat at most once per `HOOK_MIN_INTERVAL_S` (else `GET /api/inbox`) | P2 |
| `PostToolBatch` | | async | 10 | 6 | Same as `UserPromptSubmit`, at most once per `HOOK_MIN_INTERVAL_S`. The output arrives on the next turn | P2 |
| `PreToolUse` | `Edit\|Write\|MultiEdit\|NotebookEdit` | sync | 6 | 4 | **Advisory in P2**: `POST /api/check` with the repo-relative path. On a violation, `additionalContext` and `systemMessage` with a warning. Never `permissionDecision`. Unreachable: no output | P2 (fence: P6) |
| `PostToolUse` | `Edit\|Write\|MultiEdit\|NotebookEdit` | async | 10 | 6 | Heartbeat with `last_tool`. When the agent holds an active lease: a hot-file mark, `POST /api/reservations` with the path as a `shared` glob, once per path | P2 |
| `Stop` | | sync | 15 | 10 | If `stop_hook_active` is false, block once (`decision: "block"`, `reason`) while the agent holds an active lease and the worktree has uncommitted changes, unpushed commits, or changes that `/api/check` puts outside the lease; or while the inbox has messages not yet shown (the reason holds them). If `stop_hook_active` is true: no output | P2 |
| `SubagentStart`, `SubagentStop` | | async | 6 | 4 | Track child agents in the state file; heartbeat with `last_tool` (rate-limited) | P2 |
| `StopFailure` | | sync | 10 | 6 | `POST /api/notify` kind `rate_limit` (for `rate_limit` and `overloaded`) or `api_error`, so the launcher can back off | P2 |
| `Notification` | `permission_prompt\|idle_prompt\|elicitation_dialog\|agent_needs_input` | async | 6 | 4 | `POST /api/notify` kind `waiting_on_you` | P2 |
| `PreCompact` | | sync | 10 | 8 | `POST /api/notify` kind `handoff` with the branch, HEAD, lease, uncommitted files and the trigger | P2 |
| `SessionEnd` | | sync | 2 | 1.2 | `POST /api/agents/<id>/finish` with `state: "stopped"` and `pushed`; the server parks the lease when `pushed` is true, else releases it. No finish on `clear` or `resume` when the agent id is explicit, because the same agent goes on | P2 |

The request and response bodies are in `schemas/api.schema.json`: `hb_request`, `check_request`, `check_response`, `reservation_request`, `notify_request`, `finish_request`. The fixtures are in `tests/fixtures/api/`. The sample hook inputs are in `tests/fixtures/hooks/`.

**Why `StopFailure` and `SessionEnd` are sync.** In `-p` mode, Claude Code kills async hooks that still run at teardown. A worker ends in `-p` mode, so these two must finish before the process exits.

**Why the `SessionEnd` budget is 1.2 s.** Claude Code gives all `SessionEnd` hooks 1.5 s together. A plugin hook's own `timeout` does not raise that budget. `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` raises it, and the hook then uses that value less 0.3 s. A finish that does not complete in time goes to the spool when the connection fails, else the lease goes `stale` and the lead acts.

## Git guards

The fence sees only edit tools. Writes through Bash are covered by git guards that the launcher installs in each worktree:

```
wavecoord guard install [--worktree DIR]
wavecoord guard check --staged [--enforce]          # pre-commit
wavecoord guard check --push RANGE|- [--enforce]    # pre-push; "-" reads the pre-push stdin
```

- `install` sets `extensions.worktreeConfig` and then `core.hooksPath` with `git config --worktree`, so only that worktree gets the guards. The scripts are in `<git dir of the worktree>/wavecoord-hooks/`.
- Each script first runs the hook of the same name that was active before (the old `core.hooksPath`, else `.git/hooks`). A failure of that hook stops the commit or push. A re-install keeps the original chain.
- `check` sends the changed paths to `POST /api/check` with `source: "guard"`, the agent from `WAVECOORD_AGENT_ID`, the task and the branch.
- **Advisory in P2**: a violation prints a warning to stderr and exits 0. No URL, an unreachable coordinator, or a missing wavecoord build (for example after a plugin update) also exit 0. `--enforce` (or `WAVECOORD_GUARD_FLAGS=--enforce`) makes a reported violation exit 1. It is reserved for P6: the fail-closed rules for an unreachable coordinator and a stale epoch come with the fence.

**Fence design.** The fence is fail-closed: a bug stops every worker. It ships only with a design note that states its invariant as a checkable predicate, a randomized invariant test (coord killed at random points) and seeded defects that the test must catch.

**Worker settings.** Plugin subagents ignore hooks in their frontmatter, so worker hooks come from the plugin itself, loaded by the launcher with `--plugin-dir <pinned copy>` and `--settings <worker settings>`. Workers do not use `--bare`, so hooks load. The launcher sets `WAVECOORD_AGENT_ID`, `WAVECOORD_TASK`, `WAVECOORD_ATTEMPT` and `WAVECOORD_HOST` for the worker.

## Claude Code facts this design uses

From the Claude Code hooks reference:

- Exec form runs when `args` is present; `command` is spawned with no shell. `${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_DATA}` are expanded and also exported.
- `timeout` is in seconds; the default is 600, and 30 for `UserPromptSubmit`. Claude Code does not enforce `timeout` on an async hook.
- An async hook cannot block: `decision`, `permissionDecision` and `continue` have no effect. Its `additionalContext` and `systemMessage` arrive on the next turn.
- `additionalContext` is capped at 10,000 characters; the hooks clip at 9,000.
- `hookSpecificOutput` needs `hookEventName`. `Stop` blocks with top-level `decision: "block"` and `reason`; its input has `stop_hook_active`.
- `PostToolBatch` input has `tool_calls`. `StopFailure` input has `error` (`rate_limit`, `overloaded`, ...), `error_details` and `last_assistant_message`; its output is ignored. `Notification` input has `message`, `title` and `notification_type`; it has no `additionalContext`. `PreCompact` input has `trigger` and `custom_instructions`. `SessionEnd` input has `reason` (`clear`, `resume`, `logout`, `prompt_input_exit`, `other`); its output is discarded.
