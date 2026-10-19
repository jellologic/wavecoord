# Consumer config and adapters

A consumer repo configures wavecoord in `.claude/wavecoord.json` (git-tracked, no secrets). Schema: `schemas/config.schema.json`; examples: `tests/fixtures/config/`.

| Key | Default | Meaning |
|---|---|---|
| `coord_url` | none | Server URL for the terminal CLI and the hooks. `WAVECOORD_URL` and the plugin option `coord_url` take priority; with none of the three set, `wavecoord status` prints `not configured`. The server itself does not need it: it gives workers its own address. A non-loopback URL here gets no user token: set it in the plugin option or `WAVECOORD_URL` (`security.md`) |
| `project` | required | Project name on the server |
| `log_branch` | `coord/log` | Orphan branch for events |
| `plan` | the spec tree when `spec.dir` on the default branch holds at least one valid record (a `_node.md` or a task that parses), else `{"adapter": "builtin", "dir": "plan/tasks"}` | Plan store (below). `{"adapter": "specs"}` names the spec tree (`specs.md`). Other files in that folder (an `openapi.yaml`, a README) never select it, and the working tree never counts. The `config` tool shows the store in use and why (`selected_by`). When the spec tree on the default branch cannot be read, the lead fails closed (`unavailable`) rather than fall back to `plan/tasks` (a repo whose origin has no default branch plans from `plan/tasks`); name an adapter to choose without reading it. The first plan PR that adds spec records switches the store when it merges: `spec_edit` warns (`switch:`), and `wavecoord spec migrate-plan` in that PR keeps the flat tasks and their attempts. Any other adapter (`builtin`, `commands`) ignores the spec tree: `spec_tree` and `spec_waves` say so (`plan_note`) |
| `hosts[]` | one `local` host, 2 slots, `repo_path` `.` | `name`, `ssh` (destination or alias; absent = local), `slots`, `repo_path`, `env`, `prepare_cmd`, `claude_flags`, `providers`, `labels`, `dispatch`. See `launcher.md` |
| `merge_cmd` | `gh pr merge {pr} --merge --match-head-commit {sha}` | How the refinery merges a PR (below) |
| `pr_cmd` | `gh pr create --head {branch} --base {base} --title {title} --body-file {body_file}` | How the lead machine opens a PR for a pushed attempt branch or a plan branch (below) |
| `pr_state_cmd` | `gh pr view {pr} --json state,mergedAt,url` | How `collect` reads the state of a parked attempt's PR (below, "PR state command") |
| `check_cmd` | none | Command the refinery runs before a merge |
| `protected_paths_cmd` | none | Prints protected globs, one per line. A PR that touches one stops in the refinery until the user approves it in the lead session |
| `fence` | `advisory` for every point, `ask` for `lead` | The edit fence modes: a mode word (`off`, `advisory`, `enforce`) for every point, or `{"edit", "commit", "push", "merge": <mode>, "lead": "warn" \| "ask" \| "deny"}`. Read from the default branch only (`hooks.md`, "The edit fence"; `guide.md`, section 4) |
| `providers` | none | Provider profiles for workers: `{<name>: {env_file, models?, cost_basis?}}`. See `providers.md` |
| `default_provider` | `anthropic` | Profile when the task and the launch name none |
| `wip_cap` | number of slots | Maximum attempts in flight |
| `branch_prefix` | `wave` | Attempt branches are `<prefix>/<task>-<attempt>` |
| `spec.dir` | `specs` | The spec tree's folder, relative to the repo root (`specs.md`) |
| `spec.require_approved` | `true` | A task is ready only when its requirement is approved and its ancestors are active |
| `spec.suspect` | `warn` | `refuse`: a task whose requirement changed since its `basis` is not ready |
| `spec.verify_at_merge` | `true` | The refinery runs the task's Acceptance and its requirement's Verify, both from the default branch, on the merge result, before a merge; a failure blocks with `verify_failed`. Read from the default branch only |
| `spec.verify_env` | `[]` | More environment variable names that Verify and Acceptance get (the rest is scrubbed; `security.md`) |
| `spec.status_commits` | `true` | The coordinator writes task status into the spec tree (`specs.md`, Status) |
| `spec.status_flush_s` | `60` | Seconds between status commits; a merge flushes at once |
| `spec.status_author` | `wavecoord <wavecoord@localhost>` | Author of the status commits |
| `auto_resolve` | `false` | On a merge-tree conflict of a finished attempt, the collector or the refinery dispatches a resolver by itself (at most `MAX_RESOLVES` per attempt). Off: the conflict is recorded and the lead calls `resolve` |
| `auto_steer` | `draft` | What `/wavecoord:triage` does with a `stuck` notice of a running worker: `off` nothing; `draft` the `steerer` subagent drafts a hint and the lead decides; `hints` triage sends plain-text hints itself (at most one per attempt every 10 minutes). Interrupts and model switches always need the lead's decision. A task PR cannot change it (`config_change`) |

A command is an array (run without a shell, preferred) or a string (run with `sh -c`). Commands run in the consumer repo root unless noted.

## Plan adapter

`builtin` reads `plan.dir/*.md` (see `task-spec.md`). `commands` delegates to the consumer's own tooling, so an existing tracker or ledger stays the source of truth. Each command reads JSON on stdin and writes JSON on stdout; a non-zero exit is an error and stderr is shown to the lead.

Each command gets the JSON of the `stdin` column and must print the JSON of the `stdout` column. The message shapes are the definitions of `schemas/plan-adapter.schema.json`; examples are in `tests/fixtures/plan-adapter/`, and `tests/fake-plan/plan` is a working fake adapter.

| Command | stdin | stdout | Fixtures |
|---|---|---|---|
| `ready` | `{}` | `{"tasks": [<task-spec>, ...]}` in priority order | `ready-in.json`, `ready-out.json` |
| `get` | `{"id": "<task>"}` | `<task-spec>`; exit non-zero for an unknown id | `get-in.json`, `get-out.json` |
| `graph` | `{}` | `{"nodes": [{"id", "state", "phase", "title"?, "size"?}], "edges": [{"from", "to"}]}`; `from` needs `to` | `graph-in.json`, `graph-out.json` |
| `validate` | `{}` (the whole plan) or `{"spec": <task-spec>}` | `{"ok": true}` or `{"ok": false, "errors": ["..."]}` | `validate-in.json`, `validate-out*.json` |
| `edit` | `{"branch": "plan/<name>" or null, "edits": [<edit>, ...]}` | `{"branch", "commit", "pr"?, "files"?}`. Must commit to `branch` (or a new `plan/<n>`), never the default branch | `edit-in.json`, `edit-out.json` |

`<task-spec>` is an object valid against `task-spec.schema.json`; `<edit>` is the `edit` definition of `plan-adapter.schema.json` (the same edits as `plan_edit`). wavecoord validates every output and rejects output that does not match. wavecoord adds what only the coordinator knows: it drops a `ready` task whose effective state is not `todo` or whose paths overlap a live reservation, and it replaces node states with effective states (`task-spec.md`, "Ready"). The critical path and the funnel heads of `plan_graph` are computed by wavecoord from the graph.

## Plan edits (built-in store)

`plan_edit` applies a list of anchored edits to the spec files and opens one PR:

| Field | Meaning |
|---|---|
| `op` | `replace` (the anchor by `text`), `insert_before`, `insert_after`, `delete` (the anchor), `create` (a new file with `text`; no anchor) |
| `id` or `path` | The file: `<plan.dir>/<id>.md`, or `path` relative to `plan.dir` |
| `anchor` | Exact text. It must occur exactly once, or `occurrence` (from 1) picks one; a missing or ambiguous anchor fails the call |
| `text` | The new text |

The edits apply in order, in memory. Every changed file must still be a valid spec with `id` equal to its file name. Any failure fails the whole call and writes nothing. Then wavecoord fetches `origin`, adds a detached worktree at `origin/<default>` (or at `origin/<branch>` for an existing plan branch), writes the files, commits once, pushes only `refs/heads/plan/<n>` (`n` is one more than any `plan/<number>` on origin or local), removes the worktree, and runs `pr_cmd` on the lead machine for a new branch. An edit on an existing branch adds a commit to its PR; the branch is refused when it is already merged into the default branch, or when `pr_state_cmd` (with the branch as `{pr}`) says its PR is merged or closed, and gets a PR when it has none. The commit does not run your git hooks. The default branch is never written.

## PR state command

A PR can be merged outside the refinery, for example by the consumer's own merge gate. Its lease then stays `parked` and keeps its exclusive reservations, so an overlapping task never becomes ready. So `collect` (and each tick of the dispatcher loop, which calls it) runs `pr_state_cmd` on the lead machine, in the consumer repo root, for each `parked` lease whose attempt has a PR URL. The placeholders are `{pr}` (the PR number from the URL, else the URL), `{pr_url}` and `{branch}`; an array command without `{pr}` or `{pr_url}` gets the PR as its last argument. The environment has `WAVECOORD_PR`, `WAVECOORD_PR_URL` and `WAVECOORD_BRANCH`. The last stdout line must be a JSON object with `state` (`OPEN`, `MERGED` or `CLOSED`, any case) and optionally `mergedAt` and `url`. The status writer also runs it with the branch `wavecoord/status` as `{pr}`, to find its open status PR after a restart (`specs.md`).

- `MERGED` (or a `mergedAt`): `attempt.end` with state `merged`, then `lease.release` with reason `merged`. The task counts as done.
- `CLOSED` without a merge: `attempt.end` with state `abandoned`, then `lease.release` with reason `abandoned`.
- `OPEN`, or an error: nothing changes; an error is in the `parked` list of the `collect` result.

Each change is logged once (idempotency keys `pr-merged:<task>/<n>`, `pr-abandoned:<task>/<n>`, `pr-release:<lease id>`). `collect` with `dry_run` only lists what it would do.

## Host prepare hook

`prepare_cmd` runs in the new worktree after checkout and before the git guards and the worker start, with `WAVECOORD_TASK`, `WAVECOORD_ATTEMPT`, `WAVECOORD_WORKTREE`, `WAVECOORD_BRANCH` and `WAVECOORD_HOST` set (and the rest of the worker environment, `launcher.md`). Use it for build caches, symlinks to large directories, or toolchain setup. A non-zero exit fails the attempt before any model call.

## Hosts

- `repo_path`: a checkout of the consumer repo with an `origin` remote. On a local host a relative path is relative to the consumer repo root; on an ssh host use an absolute or `~/` path. Attempt worktrees go to `<repo_path>/../wt/<project>/<task>-<n>`.
- `env`: extra environment for `prepare_cmd` and the worker on that host. Values expand `~` and `$VAR` on the host, so `"PATH": "$HOME/.local/bin:$PATH"` works.
- `labels`: capabilities of the host (for example `gpu`, `python3`). A task's `exec.host_needs` must all be in the host's labels or be its name; dispatch places the task only on such hosts.
- `providers`: the provider profiles this host has (default: all). Each profile's `env_file` must exist on the host with mode 600 (`providers.md`). That file is the host's credentials for that profile: a host with no claude login can take attempts of any profile it has (`launcher.md`, "Hosts and placement").
- `dispatch`: `true` by default. With `false`, automatic placement never picks the host: `dispatch_wave`, `dispatch` without a host, the dispatcher loop and `wavecoord launch` without `--host`. An explicit host (`--host <name>`, the `host` of the `dispatch` tool) still places an attempt there. Use it for a lead machine that must keep its slots free, or a host you want to drain.
- `claude_flags`: appended to every `claude -p` on that host. Put the permission setup for unattended workers here, for example `["--permission-mode", "acceptEdits", "--allowedTools", "Bash(make *),Bash(git add *),Bash(git commit *)"]`.

## PR command

`pr_cmd` runs on the lead machine (where the server and the dispatcher run), in the consumer repo root, never on a worker host. The host only pushes the attempt branch and reports `pr_ready` when the attempt is `reported`, pushed, and its merge-tree against the default branch is clean. `wavecoord collect`, the `collect` tool, the dispatcher loop and `wavecoord launch --wait` then run `pr_cmd`. On a merge-tree conflict no PR is opened: the collector logs `conflict.open` and, with `auto_resolve`, dispatches a resolver. In an array command these placeholders are replaced: `{branch}`, `{base}` (the default branch), `{title}`, `{body_file}`, `{task}`, `{attempt}`. Every command (array or string) also gets `WAVECOORD_BRANCH`, `WAVECOORD_BASE`, `WAVECOORD_PR_TITLE`, `WAVECOORD_PR_BODY_FILE`, `WAVECOORD_TASK` and `WAVECOORD_ATTEMPT`. The body file starts with the `Task:`, `Attempt:` and `Lease: <task>@<epoch>` lines that the refinery and gate checks read. The last `http(s)://` line of stdout is the PR URL. A failure is recorded as `pr_error` on the attempt; the branch stays pushed, and the next collect does not try again (open the PR by hand, or dispatch a `--continue` run).

## Merge command

`merge_cmd` runs on the lead machine in the consumer repo root, one PR at a time (the refinery loop). In an array command these placeholders are replaced: `{pr}` (the PR number, from the last `/pull/<n>` of the URL), `{pr_url}`, `{sha}` (the head the refinery checked), `{branch}`, `{base}`, `{task}`, `{attempt}`. An array with neither `{pr}` nor `{pr_url}` gets the PR number as the last argument. Every command also gets `WAVECOORD_PR`, `WAVECOORD_PR_URL`, `WAVECOORD_SHA`, `WAVECOORD_BRANCH`, `WAVECOORD_BASE`, `WAVECOORD_TASK` and `WAVECOORD_ATTEMPT`. Use the sha (for example `--match-head-commit`), so a push after the checks cannot merge unchecked. `check_cmd` runs before it in a temporary detached worktree of that sha, with the same variables. `protected_paths_cmd` runs in the repo root; the files that the PR changes are matched against its globs.
