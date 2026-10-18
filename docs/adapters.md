# Consumer config and adapters

A consumer repo configures wavecoord in `.claude/wavecoord.json` (git-tracked, no secrets). Schema: `schemas/config.schema.json`; examples: `tests/fixtures/config/`.

| Key | Default | Meaning |
|---|---|---|
| `coord_url` | `http://127.0.0.1:18741` | Server URL. `WAVECOORD_URL` and the plugin option `coord_url` take priority |
| `project` | required | Project name on the server |
| `log_branch` | `coord/log` | Orphan branch for events |
| `plan` | `{"adapter": "builtin", "dir": "plan/tasks"}` | Plan store (below) |
| `hosts[]` | one `local` host, 2 slots | `name`, `ssh` (destination or alias; absent = local), `slots`, `repo_path`, `env`, `prepare_cmd`, `claude_flags` |
| `merge_cmd` | `gh pr merge --squash` | How the refinery merges a PR. Receives the PR number as the last argument |
| `pr_cmd` | `gh pr create --fill` | How the launcher opens a PR for a pushed attempt branch |
| `check_cmd` | none | Command the refinery runs before a merge |
| `protected_paths_cmd` | none | Prints protected globs, one per line. A PR that touches one stops in the refinery until the user approves it in the lead session |
| `wip_cap` | number of slots | Maximum attempts in flight |
| `branch_prefix` | `wave` | Attempt branches are `<prefix>/<task>-<attempt>` |

A command is an array (run without a shell, preferred) or a string (run with `sh -c`). Commands run in the consumer repo root unless noted.

## Plan adapter

`builtin` reads `plan.dir/*.md` (see `task-spec.md`). `commands` delegates to the consumer's own tooling, so an existing tracker or ledger stays the source of truth. Each command reads JSON on stdin and writes JSON on stdout; a non-zero exit is an error and stderr is shown to the lead.

| Command | stdin | stdout |
|---|---|---|
| `ready` | `{}` | `{"tasks": [<task-spec>, ...]}` in priority order |
| `get` | `{"id": "<task>"}` | `<task-spec>` |
| `graph` | `{}` | `{"nodes": [{"id", "state", "phase"}], "edges": [{"from", "to"}]}` (`from` needs `to`) |
| `validate` | `{"spec": <task-spec>}` | `{"ok": true}` or `{"ok": false, "errors": ["..."]}` |
| `edit` | `{"id", "anchor", "replace", "branch"}` | `{"branch", "commit", "pr"}`. Must commit to `branch`, never the default branch |

`<task-spec>` is an object valid against `task-spec.schema.json`. wavecoord validates adapter output against the schema and rejects invalid output.

## Host prepare hook

`prepare_cmd` runs in the new worktree after checkout and before the worker starts, with `WAVECOORD_TASK`, `WAVECOORD_ATTEMPT`, `WAVECOORD_WORKTREE` and `WAVECOORD_HOST` set. Use it for build caches, symlinks to large directories, or toolchain setup. A non-zero exit fails the attempt before any model call.
