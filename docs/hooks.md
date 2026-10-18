# Hooks

All hooks are command hooks that run `${CLAUDE_PLUGIN_ROOT}/bin/wavecoord hook <event>` in exec form (`args`, no shell). The hook reads the event JSON on stdin.

**Rule 1: nothing happens unless coord is configured.** Without a coordinator URL (or with an unreachable one, except for the fence), every hook prints nothing and exits 0 within `CLIENT_TIMEOUT_S`.
**Rule 2: a hook never breaks a session**, except the fence's deliberate denial. Bad input, a timeout or a server error exit 0 silently.
**Rule 3: messages are data.** Injected text uses the frame in `contract.md` section 4.

P0 ships only a `SessionStart` no-op. The planned set:

| Event | Matcher | Mode | Behavior | Milestone |
|---|---|---|---|---|
| `SessionStart` | | sync | Worker: spec summary, lease and epoch, paths, inbox, peers, hot files, as `additionalContext`. Lead: wave state | P2 |
| `UserPromptSubmit` | | sync | Deliver undelivered inbox messages as `additionalContext`; heartbeat | P2 |
| `PostToolBatch` | (none) | sync | Same as `UserPromptSubmit`, between tool batches of long runs; heartbeat | P2 |
| `PreToolUse` | `Edit\|Write\|NotebookEdit` | sync | **Fence**: deny (`permissionDecision: "deny"`) an edit inside another lease's exclusive glob. Fails closed when coord is configured but unreachable. An override exists only as a user-set environment variable and is logged | P6 (advisory in P2) |
| `PostToolUse` | `Edit\|Write\|NotebookEdit` | async | Hot-file marks (who touched what, for peers and conflicts) | P2 |
| `Stop` | | sync | If `stop_hook_active` is false, block once (`decision: "block"`) while: work is uncommitted or unpushed, the diff leaves the lease's paths, acceptance commands were not run, or the inbox has unread messages. Never blocks twice | P2 |
| `SubagentStart`, `SubagentStop` | | async | Track child agents of a worker | P2 |
| `StopFailure` | | async | Report rate limits and API errors; the launcher backs off | P2 |
| `Notification` | | async | Mark the agent `waiting_on_you` for the lead | P2 |
| `PreCompact` | | sync | Write a handoff note (task, progress, next step) to the attempt | P2 |
| `SessionEnd` | | async | Finish the agent: park or release the lease | P2 |

**Bash writes.** The fence sees only edit tools. Writes through Bash are covered by git `pre-commit` and `pre-push` guards that the launcher installs in each worktree (P2): they refuse a commit or push that changes a path outside the lease, or with a stale epoch.

**Fence design.** The fence is fail-closed: a bug stops every worker. It ships only with a design note that states its invariant as a checkable predicate, a randomized invariant test (coord killed at random points) and seeded defects that the test must catch.

**Worker settings.** Plugin subagents ignore hooks in their frontmatter, so worker hooks come from the plugin itself, loaded by the launcher with `--plugin-dir <pinned copy>` and `--settings <worker settings>`. Workers do not use `--bare`, so hooks load.
