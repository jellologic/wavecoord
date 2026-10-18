# Changelog

All notable changes to wavecoord. Versions follow `plugin/.claude-plugin/plugin.json`; users stay on a version until it changes.

## 0.1.0 (unreleased)

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
