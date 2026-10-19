# wavecoord

A Claude Code plugin for plan-driven, parallel agent work.

1. A **lead** Claude Code session plans the work up front, in git: tasks, dependencies, the file paths each task owns, and an execution plan per task (model, effort, budget, risks, acceptance checks).
2. The lead dispatches every task that can run in parallel to Claude Code **workers** on local or SSH hosts. Each worker runs in its own git worktree and branch, with its paths reserved.
3. Workers report back. The lead opens the PRs, sends conflicts to resolver agents, merges in dependency order and revises the plan, all through MCP tool calls.

All state is in git and can be rolled back: the plan on your default branch, every coordination event on an append-only `coord/log` branch.

Status: v0.1 (early). No runtime dependencies: the coordinator, launcher and hooks are one stdlib Python zipapp (Python 3.9 or later).

## Install

```bash
claude plugin marketplace add jellologic/wavecoord
claude plugin install wavecoord@wavecoord
```

In a session, `/plugin marketplace add jellologic/wavecoord` and `/plugin install wavecoord@wavecoord` do the same.

The plugin needs a coordinator server on the lead machine. The plugin's `bin/wavecoord` is the same program for both; to use it in a terminal, put a copy on your PATH:

```bash
curl -fsSL -o ~/.local/bin/wavecoord \
  https://raw.githubusercontent.com/jellologic/wavecoord/v0.1.0/plugin/bin/wavecoord
chmod +x ~/.local/bin/wavecoord
wavecoord version
```

## Quick start

The full walk-through is [docs/quickstart.md](docs/quickstart.md). In short, in your repo:

1. **Config.** Commit `.claude/wavecoord.json`:
   ```json
   {
     "project": "myapp",
     "plan": {"adapter": "builtin", "dir": "plan/tasks"},
     "hosts": [{"name": "local", "slots": 3, "repo_path": ".",
                "claude_flags": ["--permission-mode", "acceptEdits",
                                 "--allowedTools", "Bash(git add *),Bash(git commit *)"]}],
     "wip_cap": 3
   }
   ```
2. **Tokens.** One lead token, one agent token per host:
   ```bash
   wavecoord tokens provision --role lead --out ~/.wavecoord/token
   wavecoord tokens provision --role agent --host local --out ~/.wavecoord/hosts/local.token
   ```
3. **Server.** Run the coordinator as a user service (launchd on macOS, `systemd --user` on Linux):
   ```bash
   wavecoord install-server        # add --dry-run to see the unit first
   wavecoord status                # ok: http://127.0.0.1:18741 ...
   ```
4. **Plugin options.** Set `token` to the lead token (`cat ~/.wavecoord/token`) and `lead` to true for the session you plan from. `coord_url` defaults to `http://127.0.0.1:18741`.
5. **Plan.** `/wavecoord:wave-plan add token rotation to the auth service`. Review and merge the plan PR.
6. **Run.** `/wavecoord:wave-run` dispatches the ready, non-overlapping tasks and arms a Monitor on `wavecoord events --follow --for lead`, so reports, conflicts and notices reach the session while you work.
7. **Triage.** `/wavecoord:triage` turns each event into the next action: open PRs, merge through the refinery, resolve conflicts, retry, revise the plan. For an unattended run: `/loop 10m /wavecoord:triage`.
8. **Steer.** Running workers send `progress` and `stuck` lines. `wavecoord hint <task> "<text>"` reaches a running worker within seconds, and `wavecoord steer <task> interrupt|model <m>|stop` changes its course ([launcher.md](docs/launcher.md), "Listen and steer").

## Lead skills

| Skill | What |
|---|---|
| `/wavecoord:wave-plan <goal>` | Decompose a goal into task specs with an LLM execution plan each; planner and reviewer subagents draft and critique; one plan PR |
| `/wavecoord:wave-run` | Dispatch the ready, non-overlapping tasks up to `wip_cap`; arm the feedback Monitor |
| `/wavecoord:triage` | Turn reports, notices, conflicts and proposals into the next action |
| `/wavecoord:revise` | Turn proposals and plan feedback into anchored spec edits in one plan PR |
| `/wavecoord:resolve` | Inspect conflicts; merge in order or dispatch a resolver |
| `/wavecoord:coord-status` | Coordinator status and the current wave |

## Docs

| Page | What |
|---|---|
| [quickstart.md](docs/quickstart.md) | From install to the first merged wave |
| [contract.md](docs/contract.md) | Server, REST, SSE and MCP contract; state machines; constants |
| [task-spec.md](docs/task-spec.md) | The task spec format and the "ready" rules |
| [adapters.md](docs/adapters.md) | Consumer config, plan adapters, `pr_cmd`, `merge_cmd` |
| [launcher.md](docs/launcher.md) | Worker hosts, attempts, worktrees, push and PR flow |
| [providers.md](docs/providers.md) | Provider profiles (Anthropic-compatible endpoints) and fallback |
| [feedback.md](docs/feedback.md) | How reports reach the lead: Monitor, hooks, channel; the watchdog |
| [hooks.md](docs/hooks.md) | The Claude Code hooks and git guards |
| [security.md](docs/security.md) | Network, tokens, and why coordination text is never consent |
| [rollback.md](docs/rollback.md) | Revert one event, roll back to a commit, rebuild the cache |
| [specs.md](docs/specs.md) | Requirements as Markdown in your repo, linked from tasks (planned; design in [design/spec-layer.md](docs/design/spec-layer.md)) |

## Layout

| Path | What |
|---|---|
| `.claude-plugin/marketplace.json` | The `wavecoord` marketplace |
| `plugin/` | The plugin: manifest, hooks, skills, agents (`planner`, `reviewer`), the lead channel, `bin/wavecoord` (a stdlib Python zipapp) |
| `src/wavecoord/` | Source of `bin/wavecoord` |
| `schemas/` | JSON Schemas: task spec, worker report, event, consumer config, REST bodies |
| `docs/` | The pages above |
| `tests/` | Unit tests, fixtures, a fake `claude`, `gh` and `ssh`, and the localhost end-to-end test (`tests/e2e/`) |

See [CONTRIBUTING.md](CONTRIBUTING.md) for the PR and CI flow and [CHANGELOG.md](CHANGELOG.md) for releases.
