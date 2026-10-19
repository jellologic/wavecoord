# Changelog

All notable changes to wavecoord. Versions follow `plugin/.claude-plugin/plugin.json`; users stay on a version until it changes.

## 0.2.0

The spec tree: requirements and plans as typed Markdown records in your repo, merge-friendly, checked by scripts, planned in waves, with live status in git (`docs/specs.md`, `docs/design/spec-layer.md`). Phases S0 to S5.

### Added

- **Spec tree, phase S1** (`docs/design/spec-layer.md`, `docs/specs.md`): typed Markdown records for capabilities, features, requirements, tasks and decisions under `specs/` (`spec.dir`).
  - `wavecoord spec tree|show|query|check|lint|fmt|fp`: read the tree from the working tree or a git ref (`--ref`, two git processes for any number of files), query it (`kind=task status=planned outputs~src/*`), check each record (layout, frame, schema, sections, artifacts, EARS, canonical form), format it canonically, and print a requirement's fingerprint.
  - `schemas/spec-record.schema.json`, and the `spec.dir` config key.
- **Spec tree, phase S2**: the rules across records and the writes.
  - `check` now also proves references (with aliases), blockers (`needs` on any node expands to its subtree and is inherited; no cycles; no needs on an ancestor or descendant), alignment rules A1-A7 between inputs, outputs and `needs`, and predicate P (two tasks that may run at the same time do not write the same paths); suspect tasks are warnings.
  - New reads: `waves` (what can run together, the critical path, the width), `ready`, `trace`, `artifacts`, `suspect`.
  - Writes through one op API (`schemas/spec-ops.schema.json`): `new`, `set`, `status`, `section`, `add`, `rm`, `artifact`, `artifact-rm`, `scenario`, `mv` (rewrites references, keeps the old id as an alias), `rebase-basis`, `supersede`, `delete`, and `apply` for a JSON list of ops. A change that would add an error writes nothing; `--dry-run` prints the diff.
  - `migrate-plan` moves `plan/tasks` into the tree.
  - Tests: real `git merge` of random edits on two branches (no conflict, the result checks clean), sorted inserts, genuine conflicts, status against other keys, races; parallel safety of the computed waves; injected misalignments; seeded defects S01-S06 and S09-S11.
- **Spec tree, phase S3**: the tree in the lead.
  - The spec tree is the plan store when the config has no `plan` key and `spec.dir` has records on the default branch, or with `"plan": {"adapter": "specs"}`: `plan_ready`, `plan_get`, `dispatch`, `dispatch_wave` and the dispatcher loop read tasks from it. A task's coordinator id is its path with `.` for `/`. The coordinator's states (live attempts, parked leases, merges) count over the status in the files.
  - The worker's prompt gets the requirement (statement, scenarios), the task's inputs and outputs with their contracts, and the requirement's Verify commands.
  - MCP tools `spec_tree`, `spec_get`, `spec_query`, `spec_waves`, `spec_trace`, `spec_check` and `spec_edit` (ops on a `plan/<n>` branch, moves as `git mv`, one PR; several calls with one branch build one PR), and the REST routes under `/api/spec/`.
  - `plan/edit.py` split into `plan_branch`, `commit_files` and `read_at`, shared by `plan_edit` and `spec_edit`.
- **Spec tree, phase S4**: status commits, the merge rule, verify at merge.
  - The coordinator writes task status into the spec tree: `in_progress`, `in_review`, `done`, and back to `planned`, by reconciling the files with its state and committing only the `status:` lines that differ (`wavecoord: status (<n> tasks)`, `Wavecoord-Status:` lines), every `spec.status_flush_s` and at once after a merge. When the default branch refuses the push, the commit goes through a `wavecoord/status` PR merged with `merge_cmd`. Events `spec.status` and `spec.status_blocked`; `wavecoord spec status-sync` and the `spec_status_sync` tool.
  - A task PR may not change the spec tree: gate-check reports `spec_change`, the refinery blocks (edit-fence.md section 8.1, predicate S, seeded defect D25). `spec` and `plan` joined the config keys that a task PR may not change.
  - `spec.verify_at_merge` (on by default): the refinery runs the task's Acceptance and its requirement's Verify, from the default branch, after `check_cmd`; a failure blocks with `verify_failed`; `refinery.merge` records the requirement and its fingerprint.
  - The PR body of a tree task ends with the trailers `Spec-Task:` and `Spec-Basis:`.
  - Config keys `spec.require_approved`, `suspect`, `verify_at_merge`, `status_commits`, `status_flush_s`, `status_author`.
  - Seeded defects S07 and S08.
- **Spec tree, phase S5**: the workflow.
  - `/wavecoord:spec` builds and revises the tree with the user (references `ears.md`, `spec-templates.md`) and opens one plan PR with `spec_edit`.
  - `wave-plan` plans tasks under requirements (outputs cover the requirement's promises, inputs from needs, Acceptance includes Verify, `spec_waves` in the PR body); `revise` handles suspect tasks and the proposal kinds `requirement_gap` and `spec_change`; `wave-run` uses `spec_waves`; `triage` handles `verify_failed`, `spec_change` and `spec.status_blocked`; `coord-status` shows the tree's progress.
  - The planner and reviewer subagents read the tree (`spec_get`, `spec_query`, `spec_trace`, `spec_check`); the reviewer has a spec mode.
  - Report proposals `requirement_gap` and `spec_change`.
  - `scripts/smoke-live.py --checks spec`: a two-task requirement through the coordinator with a real `claude`.
- **Block lists in the YAML subset**: `key:` followed by `- item` lines, in task specs too, and block lists of flat maps for the spec tree's inputs and outputs. Every file that parsed before parses to the same value.

### Fixed

- **A launched worker's `SessionEnd` hook released its own lease before the launcher pushed.** Real Claude Code runs `SessionEnd` when a worker ends; the hook sent `finish` with `pushed: false`, so the server released the lease, the attempt went to the refinery with no parked lease (blocked as `stale_lease`), and a task never showed as in review. The fake claude runs no `SessionEnd` hook, so only the live smoke test found it. The hook now leaves the finish to the launcher when `WAVECOORD_ATTEMPT_DIR` is set.
- **Verify at merge on a requirement with several tasks.** The requirement's Verify describes the finished requirement, so it now runs with the merge of the last open task under it; earlier merges run the task's Acceptance and record the tasks still pending (`requirement_pending`).

## 0.1.6

### Added

- **Listen to and steer running workers.** Workers now run with `--input-format stream-json`, and the wrapper keeps their stdin open (`launcher/steer.py`).
  - Every `INBOX_POLL_S` (5 s), and with each heartbeat answer, the wrapper writes the worker's new messages into its stdin as one framed user line with `priority: "next"`. The worker reads it at its next step instead of after its next tool batch. The wrapper then acks the messages with the hooks' key, so the hooks do not show them again.
  - A lead message `wavecoord:{"action": ...}` is a steer action: `interrupt` (a `control_request` `interrupt`, then the note as the next turn), `model` (`set_model`) or `stop`. From any other sender it stays plain text.
  - After a `result`, new messages get one more turn, at most 3 per run. Otherwise the wrapper closes stdin and the run ends.
  - New lead commands: `wavecoord hint <task> <text>` and `wavecoord steer <task> interrupt|model <m>|stop [--note]`.
- **Progress and stuck notices.** The wrapper keeps a digest of the stream: turns, tokens, the last tool calls and text, edited files, failing commands, and the session state (`CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS=1`). It posts a `progress` notice every `PROGRESS_S` (300 s), with the context use from a `get_context_usage` control request. It posts a `stuck` notice, once per reason, for:
  - `loop`: the same tool call 3 times in a row
  - `failing`: the same command failed 3 times
  - `waiting`: `requires_action`
  - `budget`: 75% of turns or tokens used
  - `quiet`: no output for `QUIET_S`
- **Inbox relay for sessions that wavecoord did not launch.** A worker session that a person started (`WAVECOORD_TASK` set, no launcher) gets its messages through its Claude Code inbox socket (`CLAUDE_CODE_MESSAGING_SOCKET`).
  - `SessionStart` starts `wavecoord inbox-relay`, which pushes the framed messages with the session's token and acks them only after the socket took them. The hooks skip the pushed ids.
  - The socket's line format is not documented by Claude Code. It was taken from Claude Code 2.1.302 (`hooks.md`, "Claude Code facts"). A failed push leaves the message to the hooks.
- **`steerer` subagent and `auto_steer`.** `/wavecoord:triage` handles `progress` and `stuck` lines. With `auto_steer: "draft"` (the default), the `steerer` drafts a hint and the user decides. With `"hints"`, triage sends plain-text hints itself, at most one per attempt every 10 minutes. Interrupts, model switches and stops always need the user. `auto_steer` is lead config: a task PR that changes it fails the merge check (`config_change`).
- New constants `INBOX_POLL_S`, `PROGRESS_S`, `QUIET_S` (contract section 5).
- **`scripts/smoke-live.py`**: a live check with a real `claude` (hint, steer, listen and relay; this host and `--ssh` hosts). A manual release check, never in CI (CONTRIBUTING.md).

### Fixed

- **`wavecoord tokens provision --out` into a new directory failed** (the quick start's `~/.wavecoord/hosts/local.token` on a fresh machine), after it had already added the token to the tokens file. It now creates the directory (mode 700) first.
- **A worker launched from inside a Claude session inherited that session's inbox.** The launcher now also removes `CLAUDE_CODE_MESSAGING_SOCKET` and `CLAUDE_CODE_MESSAGING_TOKEN` from the worker's environment.

## 0.1.5

### Fixed

- **`--continue` resumed a session that does not exist.** Each run of an attempt got a fresh session id in `task.json`. A `--continue` run passes only `--resume <id>`, and Claude Code keeps writing into the transcript of the resumed id (only `--fork-session` makes a new one), so the fresh id of a resumed run never had a transcript. The next `--continue` resumed that id: claude printed `No conversation found with session ID: <id>`, the result was `error_during_execution` with 0 turns, and every later `--continue` of the attempt failed the same way (seen with Claude Code 2.1.302 on Linux, after a `--continue --spec` run). Now:
  - The launcher resumes the attempt's lineage: the session whose transcript the last run wrote (`transcript_session`), then the first run's id, then the ids of the earlier runs in `prev-*/`, oldest first. So an attempt left by 0.1.4 resumes its first run's session again.
  - The wrapper checks that the transcript file exists (`$CLAUDE_CONFIG_DIR` or `~/.claude`, `projects/<encoded worktree path>/<id>.jsonl`, with the provider's environment) before it resumes. It never passes `--session-id` together with `--resume`.
  - When no transcript exists, or claude says `No conversation found`, the wrapper starts a fresh session in the same worktree, one time. The prompt is a short note with the branch's commits since the base, the first run's full prompt and the continue prompt. `session_fallback` in the result, `status.json` and `task.json` records the reason. The next `--continue` resumes the fresh session.
  - A `--continue` run with `--spec` keeps the same lineage. The result, the report request and `status.json` give the session id of the transcript.
  - The fake claude keeps transcripts in a fake `~/.claude/projects` layout (`FAKE_CLAUDE_TRANSCRIPTS=1`, on in the launcher and e2e tests), refuses `--session-id` with `--resume`, and fails a `--resume` of an unknown id as Claude Code does. New tests: two `--continue` runs (the second with the spec prompt) resume the first id, recovery of a 0.1.4 attempt dir, the fallback without a transcript, and the fallback after `No conversation found`.
- **`dispatch --wave` checked `wip_cap` against the same count for each task.** The dry run printed `wip 3/6` for all four tasks of a wave, because `decide` counted only the attempts in the snapshot. Now each task that the wave placed counts for the next one (`wip 3/6`, `4/6`, ...), in a dry run too, and a wave stops at the cap. New assertions in `test_wip_cap_and_wave`.

## 0.1.4

### Fixed

- **A task PR could change the fence and the lead config.** Section 8 of `docs/design/edit-fence.md` says that a fence mode change is a lead decision in a config PR, but nothing enforced it: a worker could commit `"fence": "off"` (or new `claude_flags`, `providers`, a `merge_cmd`, a settings hook) and the merge put it on the default branch. Now `gate-check` and the refinery refuse a task PR (a PR with `Task:`, `Attempt:` and `Lease:` lines) that changes `fence`, `merge_cmd`, `check_cmd`, `protected_paths_cmd`, `providers` or `hosts[].claude_flags`/`env` in `.claude/wavecoord.json`, the keys `hooks`, `disableAllHooks`, `enabledPlugins`, `extraKnownMarketplaces` or `env` in `.claude/settings.json` or `.claude/settings.local.json`, or any `hooks/hooks.json`. The violation kind is `config_change`, and the refinery reason is `config_change`, with a detail that tells the lead to make a plan or config PR. It blocks in the `merge` modes `advisory` and `enforce` (advisory mode measures the path fence; it does not let a worker turn the fence off), and is only reported in mode `off`. The mode comes from the config on the base side. Plan PRs and lead PRs have no such lines and are not affected. New seeded defect D24 with the table check `config_change_check`, and a refinery test.
- **Fence simulation flake: "journal not sent after a reachable call" under load.** Seed 631187689 failed on Python 3.9 under heavy parallel load. The fence was right: the check answered but used the whole hook budget (1.5 s in the tests), so the journal flush had no time left, and the journal stayed complete for the next reachable call. The design allows this (section 4.4: only an acknowledged entry leaves the journal, and E3 refuses while it is not empty). The flush does not get its own budget slice, because that would make the edit tool call wait longer than the E1 budget. Assertion 5 now accepts a journal that stays only when the hook run's client got no answer, a request of the flush got no answer, or the budget ended before the flush (`fencesim.flush_deferred`). A flush whose requests all got answers must still empty the journal, and the end of each run still needs one reachable call to send all of it. The hook now flushes the journal before the spool replay, because only the journal stops a push. New deterministic tests: a slow check that uses the budget (the journal stays whole, E3 refuses, the next call sends it), and a check that the tolerance does not cover a flush that kept acknowledged entries.

### Added

- **A report for a run that ran out of turns.** Workers on non-Anthropic models through Anthropic-compatible endpoints (for example GLM) used about 1.5 to 2 times the planned turns and hit `error_max_turns` before they wrote the report, so the lead got "report invalid". Now, when the result is `error_max_turns` or the run hit its token budget, and the launcher pushed commits, the launcher writes a valid `failed` report with the commits, the changed files, `checks_run` empty and `plan_feedback: "continue: <n> commits pushed"`, and sets `continue_suggested: true` on the report request, the result and the attempt.
- **Planning guidance for turn budgets.** `wave-plan` (`exec-plan.md`, `llm-risks.md`): non-Anthropic providers need about 2 times `max_turns`, and the prompt should tell the worker to batch shell commands and not to read a file again. `/wavecoord:triage`: on `continue_suggested`, continue the same attempt with `wavecoord launch --continue` and a short prompt that lists the acceptance commands. The worker rules (the appended system prompt) now say: when fewer than 15% of the turns remain, stop, commit and write the report.

## 0.1.3

### Fixed

- **The fence test oracle folded the wrong event log.** CI on `main` (macos-latest, Python 3.9) failed `test_fence_invariant` for seed 44 with six safety and integration failures and no fault. The fence and the server were right; the oracle was wrong. The oracle's fold cache (`tests/fenceworld.py`, `ofold`) was keyed by `id(events)`. A process of the test pool runs several seeds, and the event list of seed 44 got the id of the freed list of an earlier seed. The oracle then extended that seed's fold with the events of seed 44, so it saw leases of another run and said that correct writes were not `ok(W)`. The same alias can also hide a real violation. Now the cache holds the list it folded and is used only for that same object with the same last event. The invariant predicate (`docs/design/edit-fence.md`, section 2.2) did not change. PR #11 did not cause the failure: the code before #11 fails the same way when the id is reused. The new `OracleCacheTest` checks that a cached fold equals a fresh fold, that this check catches the old cache (seeded defect O1), and runs seed 44 after seed 5 in one process with a forced id reuse (the CI failure).
- **The override journal lost a write after a lost answer.** `fence.journal_flush` sends the journal with the idempotency key `fence-override:<nonce>:<first n>`. When the first send was logged but its answer was lost (a fault), and a write was added to the journal before the retry, the retry had the same key. The server returned the first notice, and the client removed all entries of the larger batch, so the new write never reached the lead. Now only the entries that the acknowledged notice holds leave the journal (`fence.acked_entries`), and the flush sends again with a new key while it makes progress, so one reachable call sends the whole journal. Found by a 300-seed soak run (seed 72857574); the code before PR #11 has the same bug. New seeded defect D23 with the table check `journal_retry_check` and the unit test `test_flush_retry_after_lost_answer_keeps_new_entries`.
- **Fence simulation: the agent's config trick reached the default branch.** The simulation's `trick` operation writes `fence: "off"` into the worktree copy of `.claude/wavecoord.json` (D13: the fence ignores it). A commit with `git add -A` staged it, and a merge put it on the default branch, so the lead's mode became `off` and the lead check expected `ask` (seed 289598702). The fence did what section 8 of the design says (the config on the default branch is the authority). The simulation keeps its config fixed, so it now keeps the trick file out of commits.

## 0.1.2

### Fixed

- **`--continue` lost the lease paths.** A `wavecoord launch --task <t> --continue` run without `--spec` claimed a new lease with no reservations, because the first run's lease was released on `finish: failed`. So the git guard and the edit fence reported every path as `outside_lease`, also the paths of the task. Now the launcher keeps the reservations of each run in the client index and claims them again on `--continue`; the claim sends `resume`; and a claim of the same attempt with no reservations keeps the paths of the attempt's current lease.
- **A failed run with pushed commits parks its lease** (reason `finish:failed`) instead of releasing it, so its reservations stay held until the `--continue` run takes them over (a `resume` claim: new agent, new epoch, same paths) and no other lease can take the paths in between. A newer attempt of the task takes over such a lease without a steal, and the lead does not count it as review. `attempt.end` with state `abandoned` releases the attempt's lease. A run without pushed commits, or a stopped run, still releases it.
- **`install-server` over a running launchd unit.** The reinstall ran `launchctl bootstrap` while the old job still exited, got `Bootstrap failed: 5: Input/output error` and left the server down. Now it runs `launchctl bootout` (a job that is not loaded is fine), waits until `launchctl print` no longer finds the job (at most 15 s), then bootstraps; after an exit 5 it boots out and waits again, and retries the bootstrap once. On Linux it runs `systemctl --user daemon-reload`, `enable` and `restart` as before; a failure of any of them is an error.
- **No dollar budget for a `cost_basis: none` provider.** `total_cost_usd` is an Anthropic-priced estimate there, so the launcher no longer passes `--max-budget-usd` for such a profile and `wrapper.log` says that the budget is not enforced. The new optional `exec.max_tokens` (`--max-tokens`) is a token budget: the wrapper counts input, output and cache-write tokens of the main conversation from the stream and interrupts the run at the limit (`reason: token_budget`). Without it, `max_turns` and `timeout_min` limit the run. The result has `tokens`. See `docs/providers.md`.
- **Guard and gate-check output never print `None`.** A violation line names the lease: `src/x: outside_lease (no reservation of your lease #7 of task t covers it)`. On an `outside_lease` the guard also prints the agent, lease and epoch it checked as. `identity.json` records the lease id.

## 0.1.1

### Fixed

- **Placement and AUTH by provider.** A host with no claude login but with the env file of a provider profile (mode 600) now counts as authenticated for an attempt of that profile. Placement is per attempt: a host can take it with the attempt's provider or with one of its `provider_fallback` profiles (less the ones backed off after a rate limit), limited to the host's `providers` list. The primary provider wins whenever an eligible host can run it; the most free slots, then the most free memory, rank hosts only within one provider rank. Before, only the first provider was looked at, so an attempt failed (or waited) when no host could run its first provider, although build hosts with free slots had the env file of its fallback. A host placed with a fallback runs that profile, and the rest of the chain stays its fallback.
- **`wavecoord hosts` AUTH column.** It shows `provider:<name>` when the host has the env file of `default_provider`, and the login check (`logged-in`, `env`, `keychain?`, `no`) otherwise. STATUS is checked against the host's `providers` list. `--json` adds `auth` and `dispatch` per host.
- **Report proposals are filed.** On `report.post` the server files each item of `report.proposals[]` as a `proposal.new` event (idempotent by `<report idem>#p<i>`; the task defaults to the attempt's task; `source` is the attempt id). Before, `GET /api/proposals`, `plan_get` and `/wavecoord:revise` did not see them.
- **PRs merged or closed outside the refinery.** `collect` (and so each dispatcher loop tick) now reads the PR state of every `parked` lease's attempt with the new `pr_state_cmd` (default `gh pr view {pr} --json state,mergedAt`). A merged PR logs `attempt.end` `merged` and releases the lease with reason `merged`; a PR closed without a merge logs `attempt.end` `abandoned` and releases the lease with reason `abandoned`. Before, the lease stayed `parked` and its exclusive reservations kept an overlapping task out of `ready`. The `collect` result has a new `parked` list.
- **Skipped hosts are visible.** The `dispatch.decide` reason now names each host that placement skipped and why (for example `build1: unreachable: Permission denied (publickey)`), and a fallback provider when one was used.

### Added

- **`install-server --env KEY=VALUE`** (repeatable) adds a variable to the unit's environment. The unit also carries this shell's `WAVECOORD_*` variables (for example `WAVECOORD_SSH`, `WAVECOORD_HOME`), except worker variables and names that look like a secret; `--no-carry-env` turns that off. Before, a launchd or systemd server had no `WAVECOORD_SSH`, so its launcher could reach an ssh host only through a ControlMaster socket that a CLI run had opened, and placement fell back to `local`.
- **`dispatch: false` on a host** (`docs/adapters.md`, `schemas/config.schema.json`): automatic placement never picks the host; `--host` and the `host` of the `dispatch` tool still do.

## 0.1.0

The first usable release: one lead session plans a wave in git, dispatches it to Claude Code workers on local or SSH hosts, and merges the results, with every coordination step in `coord/log`.

### Added

- **Plugin and marketplace.** `claude plugin marketplace add jellologic/wavecoord`, `claude plugin install wavecoord@wavecoord`. One stdlib Python zipapp (`plugin/bin/wavecoord`, Python 3.9+) is the server, the launcher, the hooks and the CLI.
- **Coordinator** (`wavecoord serve`): an append-only, hash-chained event log on the orphan branch `coord/log` (group commit before ack, async push), a sqlite cache rebuilt from the log, leases with fencing epochs, path reservations, REST, SSE and MCP over HTTP on `127.0.0.1:18741`, bearer tokens with the roles `agent`, `lead`, `ui` and `gate`.
- **`wavecoord install-server`**: copies the zipapp, pinned by its content hash, to `~/.wavecoord/bin/` and runs the server as a launchd agent (macOS) or a `systemd --user` unit (Linux). `--dry-run`, `--no-start`, `--uninstall`.
- **Plan store**: task specs as Markdown files with a small YAML frontmatter on the default branch (`docs/task-spec.md`), or a command adapter for an existing tracker. `plan_edit` makes atomic anchored edits on a `plan/<n>` branch and opens one PR.
- **Lead tools** (MCP): `plan_ready`, `plan_get`, `plan_graph`, `plan_validate`, `plan_edit`, `dispatch`, `dispatch_wave`, `collect`, `attempts`, `attempt_get`, `conflicts`, `resolve`, `refinery_status`, `refinery_merge`, `propose`, `proposals`, `proposal_decide`, `inbox`, `ack`, `send`, `config`.
- **Launcher**: attempts in their own worktree and branch on local or SSH hosts, slot placement, a pinned plugin copy per host, push without force, a structured report validated against `schemas/report.schema.json`, PRs opened on the lead machine with `pr_cmd`.
- **Provider profiles**: workers on Anthropic-compatible endpoints from a mode-600 env file on each host; rate-limit backoff and fallback to the next provider.
- **Conflicts and the refinery**: merge-tree checks, resolver agents that continue the branch, a serialized merge queue in funnel order with a fresh merge-tree re-check, protected paths, `check_cmd` and `merge_cmd` pinned to the checked sha.
- **Feedback into the lead**: `wavecoord events --follow --for lead` for a Monitor, inbox injection by the hooks, the experimental `lead-feed` channel, and a watchdog for idle workers.
- **Hooks and git guards**: heartbeats, inbox delivery, path checks before commit and push.
- **The edit fence** (`docs/design/edit-fence.md`): `fence: off|advisory|enforce` per point (default advisory). A fail-closed `PreToolUse` deny (`onFailure: "block"`), `guard check --enforce`, the launcher's own pre-push check, a fence verdict on each report, `wavecoord gate-check <pr|range>` for merge gates, the refinery's fence and stale-lease checks, the human-only offline override (`wavecoord fence offline|online|status`) with a logged journal, and reservation origins (a hot-file mark never widens a lease; an agent token may add only hot marks). Tested by a randomized invariant test with an independent oracle and 22 seeded defects.
- **Skills**: `wave-plan`, `wave-run`, `triage`, `revise`, `resolve`, `coord-status`; agents `planner` and `reviewer`.
- **Rollback**: `wavecoord revert <seq>` and `wavecoord rebuild --at <sha>`, with epochs that never go back.
- **Docs**: `README.md`, `docs/quickstart.md` and the reference pages in `docs/`.
- **Localhost end-to-end test** (`tests/e2e/test_wave.py`): a real server, MCP calls as Claude makes them, a fake `claude` with per-task modes, one wave from the plan PR to merged work, a resolved conflict, a provider fallback, a revert and a rollback, all checked in `coord/log` on the bare origin.

### Fixed

- Workers on a local host used the lead's `~/.wavecoord/token` (and an inherited plugin option token), so they acted with the lead role. They now use the host's agent token `~/.wavecoord/hosts/<host>.token` when it exists (`docs/security.md`).
