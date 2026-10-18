# wavecoord

A Claude Code plugin for plan-driven, parallel agent work.

1. A lead Claude Code session plans the work up front, in git: tasks, dependencies, file paths, and the execution plan of each task (model, effort, budget, risks, acceptance checks).
2. The lead dispatches every task that can run in parallel to Claude Code workers on local or SSH hosts.
3. Workers report back. The lead spawns conflict-resolution agents and revises the plan, all through tool calls.

All state is in git and can be rolled back: the plan on your default branch, coordination events on an append-only `coord/log` branch.

Status: early development (v0.x). See `docs/`.

## Install

```
/plugin marketplace add jellologic/wavecoord
/plugin install wavecoord@wavecoord
```

Or in one step on Claude Code 2.1.275 or later:

```
/plugin install wavecoord --marketplace jellologic/wavecoord
```

## Layout

| Path | What |
|---|---|
| `.claude-plugin/marketplace.json` | The `wavecoord` marketplace |
| `plugin/` | The plugin: manifest, hooks, skills, agents (`planner`, `reviewer`), the lead channel, `bin/wavecoord` (a stdlib Python zipapp) |
| `src/wavecoord/` | Source of `bin/wavecoord` |
| `schemas/` | JSON Schemas: task spec, worker report, event, consumer config, REST bodies |
| `docs/` | `contract.md`, `task-spec.md`, `adapters.md`, `launcher.md`, `providers.md`, `hooks.md`, `feedback.md`, `security.md`, `rollback.md` |
| `tests/` | Unit tests, fixtures, a fake `claude` and a fake `ssh` |

## Lead skills

| Skill | What |
|---|---|
| `/wavecoord:wave-plan <goal>` | Decompose a goal into task specs with an LLM execution plan each; planner and reviewer subagents draft and critique; one plan PR |
| `/wavecoord:wave-run` | Dispatch the ready, non-overlapping tasks up to `wip_cap`; arm the feedback Monitor |
| `/wavecoord:triage` | Turn reports, notices, conflicts and proposals into the next action |
| `/wavecoord:revise` | Turn proposals and plan feedback into anchored spec edits in one plan PR |
| `/wavecoord:resolve` | Inspect conflicts; merge in order or dispatch a resolver |
| `/wavecoord:coord-status` | Coordinator status and the current wave |

For unattended runs: `/loop 10m /wavecoord:triage`. How reports reach the lead: `docs/feedback.md`.

See `CONTRIBUTING.md` for the PR and CI flow.
