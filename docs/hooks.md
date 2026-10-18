# Hooks

All hooks are command hooks that run `${CLAUDE_PLUGIN_ROOT}/bin/wavecoord hook <Event>` in exec form (`args`, no shell). The hook reads the event JSON on stdin and prints at most one JSON object on stdout. The code is `src/wavecoord/hooks.py`; the REST client is `src/wavecoord/client.py`.

**Rule 1: nothing happens unless coord is configured.** Without a coordinator URL, every hook prints nothing and exits 0. An unreachable coordinator also gives no output, except that writes go to the spool (below).
**Rule 2: a hook never breaks a session**, except the fence's deliberate denial (P6). Bad input, a timeout or a server error exit 0 silently. Each event has a hard time budget below its hook timeout: when the budget ends, the hook prints nothing and exits 0.
**Rule 3: messages are data.** Injected text uses the frame in `contract.md` section 4. The body is a JSON string, so a message cannot break out of its line.

## Configuration

| Item | Source, in order |
|---|---|
| Coordinator URL | `WAVECOORD_URL`, then the plugin option `coord_url` (`CLAUDE_PLUGIN_OPTION_COORD_URL`), then `coord_url` in `.claude/wavecoord.json`, found from the hook's `cwd` upward |
| Token | The plugin option `token` (`CLAUDE_PLUGIN_OPTION_TOKEN`), then the file `WAVECOORD_TOKEN_FILE`, then `~/.wavecoord/token`. In a worker session (`WAVECOORD_TASK` or `WAVECOORD_AGENT_ID` set) only `WAVECOORD_TOKEN_FILE` (the host's agent token from the launcher); never the plugin option or `~/.wavecoord/token`, which can hold the lead's token |
| Lead | `WAVECOORD_ROLE=lead`; or the plugin option `lead` (`CLAUDE_PLUGIN_OPTION_LEAD`) when `WAVECOORD_ROLE`, `WAVECOORD_TASK` and `WAVECOORD_AGENT_ID` are all unset (`feedback.md`) |
| Agent id | `lead` when the session is the lead, else `WAVECOORD_AGENT_ID`, else `claude:<session_id>` |
| Agent kind | `lead` when the session is the lead, else `WAVECOORD_AGENT_KIND`, else `worker` when `WAVECOORD_TASK` is set, else `other` |
| Host, task, attempt | `WAVECOORD_HOST` (default `local`), `WAVECOORD_TASK`, `WAVECOORD_ATTEMPT` |
| State and spool | `WAVECOORD_STATE_DIR`, else `${CLAUDE_PLUGIN_DATA}/state`, else `~/.wavecoord/state` |

Every request has `Authorization: Bearer <token>`, `X-Wavecoord-Agent: <agent id>` and, for a write that must not repeat, `Idempotency-Key: <key>`. A request waits at most `CLIENT_TIMEOUT_S` and never past the event's budget.

**State.** One small JSON file per agent keeps the heartbeat `seq`, the time of the last heartbeat, the last lease from a heartbeat, the ids of messages already shown, the hot files already marked and the running subagents.

**Spool.** A write that cannot reach the coordinator (ack, reservation, notice, finish) goes to `spool.jsonl` in the state directory. Heartbeats and reads are not spooled. The next hook run that reaches the coordinator sends up to 20 spooled writes, oldest first, with their original agent and idempotency key. An entry leaves the spool when the server answers it with any status.

## Events

| Event | Matcher | Mode | Timeout (s) | Budget (s) | Behavior | Milestone |
|---|---|---|---|---|---|---|
| `SessionStart` | | sync | 10 | 8 | Heartbeat (registers the agent). Worker: task, spec title, lease and epoch, own paths, peers, paths reserved by others, hot files, inbox, as `additionalContext`. Lead: wave state from `GET /api/live`; on `startup` or `clear`, move the lead cursor to the head | P2, P5 |
| `UserPromptSubmit` | | sync | 10 | 6 | Deliver messages not yet shown, and lost-lease notices, as `additionalContext`; ack them; heartbeat at most once per `HOOK_MIN_INTERVAL_S` (else `GET /api/inbox`). Lead: also the event lines after the shared lead cursor, at most 40 (`feedback.md`) | P2, P5 |
| `PostToolBatch` | | async | 10 | 6 | Same as `UserPromptSubmit`, at most once per `HOOK_MIN_INTERVAL_S`. The output arrives on the next turn | P2 |
| `PreToolUse` | `Edit\|Write\|MultiEdit\|NotebookEdit` | sync | 6, `onFailure: "block"` | 2.5 | **The edit fence (E1)**, below. `POST /api/check` with the normalized repo-relative path. `advisory` (the default): a warning in `additionalContext` and `systemMessage`. `enforce`: a worker write that the fence cannot prove is in the lease gets `permissionDecision: "deny"`; a lead or other session gets `ask` for a foreign exclusive path | P6 |
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
- `install` also writes `identity.json` in the same directory: the agent, task, attempt, epoch and fence modes from the launcher env. The env of a `git commit` that a worker starts with Bash is not a reliable source, so `check` reads the identity from the file.
- `check` sends the changed paths (`git diff --name-only --no-renames`, so a rename is a delete and an add) to `POST /api/check` with `source: "guard"`, the agent, task and epoch of the identity file, and the branch.
- **Advisory** (the default): a violation prints a warning to stderr and exits 0. No URL, an unreachable coordinator, or a missing wavecoord build also exit 0.
- **Enforce** (`--enforce`, `WAVECOORD_GUARD_FLAGS=--enforce`, or `commit`/`push` enforce in the fence modes that `install` recorded): a violation and every failure exit 1 (fail closed). The script refuses when the wavecoord build is missing. The offline override (below) lets a commit pass when the coordinator is unavailable, and logs it.

## The edit fence (P6)

The design is `docs/design/edit-fence.md`; the test plan is `tests/fence/README.md`. The code is `src/wavecoord/fence.py` (client side), `src/wavecoord/fencecore.py` (the predicate on the server), `src/wavecoord/gatecheck.py` (E5) and `src/wavecoord/launcher/fencehost.py` (the launcher's part).

**Modes.** `fence` in `.claude/wavecoord.json` on the default branch: a mode word for every point (`"fence": "enforce"`) or one key per point: `{"edit", "commit", "push", "merge": "off" | "advisory" | "enforce", "lead": "warn" | "ask" | "deny"}`. The default is `advisory` for each point and `ask` for `lead`. The launcher reads the config of `origin/<default>` and gives it to the worker as `WAVECOORD_FENCE`. A worktree copy of the config can only make the fence stricter.

**The points.**

| Point | What | Enforce |
|---|---|---|
| E1 | `PreToolUse` on the edit tools | deny (exit 0 with `permissionDecision: "deny"`; exit 2 with the reason on stderr when stdout fails). A crash, a missing build or a hang past the hook timeout blocks through `onFailure: "block"` (Claude Code 2.1.295 or later) |
| E2 | the git `pre-commit` guard | exit 1 |
| E3 | the launcher's own check of the net diff before `git push` | no push; the attempt fails with `push failed: fence: <kind>` |
| E4 | `changed_paths` in the report | the server stores `fence: {verdict, violations, count, seq, epoch}` on the attempt; the refinery blocks a `fail` verdict |
| E5 | `wavecoord gate-check <pr \| BASE..HEAD>` | exit 0 pass, 1 violation or lease mismatch, 3 unavailable or error. The refinery runs it before `merge_cmd`. A task PR that changes the lead config (`config_change`) fails in `merge` modes `advisory` and `enforce` |

**Kinds.** From the server: `no_lease`, `stale_epoch`, `outside_lease`, `foreign_exclusive`. From `gate-check`: `config_change` (a task PR changes `fence`, `merge_cmd`, `check_cmd`, `protected_paths_cmd`, `providers` or `hosts[].claude_flags`/`env` in `.claude/wavecoord.json`, the hook keys of the `.claude` settings files, or a `hooks/hooks.json`; the lead makes such a change in a plan or config PR; design section 8). From the client: `foreign_worktree` (a target in another worktree of the repo), `protected_local` (`.git`, `$WAVECOORD_HOME`, `~/.wavecoord`, `~/.claude`, the plugin root), `unavailable`, `auth`, `bad_request`, `malformed`.

**Paths.** The hook makes the target absolute from `cwd`, resolves symlinks and `..` with `realpath`, finds the innermost worktree that holds it, and classifies it (design section 3.6). A git-ignored path in the own worktree and a path outside the repo pass with no call. On a case-insensitive file system it rebuilds each existing component with its case on disk and sends `fold_case: true`.

**Lead and other sessions.** Only `foreign_exclusive` applies. The action is `fence.lead` (`ask` by default). A failure of the coordinator never denies their edits: they get a `systemMessage`.

**The offline override (human only).** `wavecoord fence offline --for 1h --reason "<text>"` writes `$WAVECOORD_HOME/fence-override.json` (mode 600, at most 4 h). It refuses to run when `CLAUDECODE` is set, and while the coordinator answers. While it is valid, E1 and E2 let a worker write through when the coordinator is unavailable, and append it to `$WAVECOORD_HOME/state/fence-override.jsonl`. It never changes a deny that the server answered, and it has no effect on E3, E4 and E5. The next hook run that reaches the coordinator with budget left after its decision sends the journal as a `fence_override` notice (else a later one does); E3 refuses until the journal is sent. `wavecoord fence online` removes the file; `wavecoord fence status` shows the modes, the override and the journal. `WAVECOORD_FENCE_OFFLINE` has no effect (the hook says so in `systemMessage`).

**Workers.** The worker `--settings` pin `"disableAllHooks": false` and the fence env (`WAVECOORD_TASK`, `WAVECOORD_AGENT_ID`, `WAVECOORD_LEASE_EPOCH`, `WAVECOORD_WORKTREE`, `WAVECOORD_FENCE`, `WAVECOORD_ROLE=worker`), so the worktree's own settings files cannot turn the hooks off or change the identity. A worker gets only its host's agent token (`<wavecoord home>/hosts/<host>.token`, or the installed token on a remote host), never the lead's token or `CLAUDE_PLUGIN_OPTION_TOKEN`. In enforce mode the launcher refuses a run with no lease, and, for `edit` enforce, a host whose `claude --version` is older than 2.1.295 or that has a user mod that handles `tool.check` (a best-effort scan of `~/.claude/plugins`, `~/.claude/dev-mods`, `~/.claude/skills` and `CLAUDE_CODE_PLUGIN_DIRS`).

**Worker settings.** Plugin subagents ignore hooks in their frontmatter, so worker hooks come from the plugin itself, loaded by the launcher with `--plugin-dir <pinned copy>` and `--settings <worker settings>`. Workers do not use `--bare`, so hooks load. The launcher sets `WAVECOORD_AGENT_ID`, `WAVECOORD_TASK`, `WAVECOORD_ATTEMPT` and `WAVECOORD_HOST` for the worker.

## Claude Code facts this design uses

From the Claude Code hooks reference:

- Exec form runs when `args` is present; `command` is spawned with no shell. `${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_DATA}` are expanded and also exported.
- `timeout` is in seconds; the default is 600, and 30 for `UserPromptSubmit`. Claude Code does not enforce `timeout` on an async hook.
- An async hook cannot block: `decision`, `permissionDecision` and `continue` have no effect. Its `additionalContext` and `systemMessage` arrive on the next turn.
- `additionalContext` is capped at 10,000 characters; the hooks clip at 9,000.
- `hookSpecificOutput` needs `hookEventName`. `Stop` blocks with top-level `decision: "block"` and `reason`; its input has `stop_hook_active`.
- `PostToolBatch` input has `tool_calls`. `StopFailure` input has `error` (`rate_limit`, `overloaded`, ...), `error_details` and `last_assistant_message`; its output is ignored. `Notification` input has `message`, `title` and `notification_type`; it has no `additionalContext`. `PreCompact` input has `trigger` and `custom_instructions`. `SessionEnd` input has `reason` (`clear`, `resume`, `logout`, `prompt_input_exit`, `other`); its output is discarded.
