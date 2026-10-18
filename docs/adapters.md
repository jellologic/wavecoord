# Consumer config and adapters

A consumer repo configures wavecoord in `.claude/wavecoord.json` (git-tracked, no secrets). Schema: `schemas/config.schema.json`; examples: `tests/fixtures/config/`.

| Key | Default | Meaning |
|---|---|---|
| `coord_url` | `http://127.0.0.1:18741` | Server URL. `WAVECOORD_URL` and the plugin option `coord_url` take priority |
| `project` | required | Project name on the server |
| `log_branch` | `coord/log` | Orphan branch for events |
| `plan` | `{"adapter": "builtin", "dir": "plan/tasks"}` | Plan store (below) |
| `hosts[]` | one `local` host, 2 slots, `repo_path` `.` | `name`, `ssh` (destination or alias; absent = local), `slots`, `repo_path`, `env`, `prepare_cmd`, `claude_flags`. See `launcher.md` |
| `merge_cmd` | `gh pr merge --squash` | How the refinery merges a PR. Receives the PR number as the last argument |
| `pr_cmd` | `gh pr create --head {branch} --base {base} --title {title} --body-file {body_file}` | How the launcher opens a PR for a pushed attempt branch (below) |
| `check_cmd` | none | Command the refinery runs before a merge |
| `protected_paths_cmd` | none | Prints protected globs, one per line. A PR that touches one stops in the refinery until the user approves it in the lead session |
| `providers` | none | Provider profiles for workers: `{<name>: {env_file, models?, cost_basis?}}`. See `providers.md` |
| `default_provider` | `anthropic` | Profile when the task and the launch name none |
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

`prepare_cmd` runs in the new worktree after checkout and before the git guards and the worker start, with `WAVECOORD_TASK`, `WAVECOORD_ATTEMPT`, `WAVECOORD_WORKTREE`, `WAVECOORD_BRANCH` and `WAVECOORD_HOST` set (and the rest of the worker environment, `launcher.md`). Use it for build caches, symlinks to large directories, or toolchain setup. A non-zero exit fails the attempt before any model call.

## Hosts

- `repo_path`: a checkout of the consumer repo with an `origin` remote. On a local host a relative path is relative to the consumer repo root; on an ssh host use an absolute or `~/` path. Attempt worktrees go to `<repo_path>/../wt/<task>-<n>`.
- `env`: extra environment for `prepare_cmd`, the worker and `pr_cmd` on that host. Values expand `~` and `$VAR` on the host, so `"PATH": "$HOME/.local/bin:$PATH"` works.
- `providers`: the provider profiles this host has (default: all). Each profile's `env_file` must exist on the host with mode 600 (`providers.md`).
- `claude_flags`: appended to every `claude -p` on that host. Put the permission setup for unattended workers here, for example `["--permission-mode", "acceptEdits", "--allowedTools", "Bash(make *),Bash(git add *),Bash(git commit *)"]`.

## PR command

`pr_cmd` runs on the host, in the attempt worktree, after the launcher pushed the branch and found it merges cleanly. In an array command these placeholders are replaced: `{branch}`, `{base}` (the default branch), `{title}`, `{body_file}`, `{task}`, `{attempt}`. Every command (array or string) also gets `WAVECOORD_BRANCH`, `WAVECOORD_BASE`, `WAVECOORD_PR_TITLE` and `WAVECOORD_PR_BODY_FILE`. The body file starts with the `Task:`, `Attempt:` and `Lease: <task>@<epoch>` lines that the refinery and gate checks read. The last `http(s)://` line of stdout is the PR URL. A failure is recorded as `pr_error`; the attempt stays `reported`, and the branch stays pushed.
