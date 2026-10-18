# Changelog

All notable changes to wavecoord. Versions follow `plugin/.claude-plugin/plugin.json`; users stay on a version until it changes.

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
