# Changelog

All notable changes to wavecoord. Versions follow `plugin/.claude-plugin/plugin.json`; users stay on a version until it changes.

## 0.3.0

Two adversarial reviews of 0.2.0, fixed: the spec workflow (#27-#30) and the whole plugin (#32-#36). The trust boundary between workers and the lead is tighter, the refinery merges only work that should land, and the launcher, steering and hooks survive restarts, retries and stops. Some changes need action on upgrade.

### Upgrading

- Set the plugin option `coord_url` (it has no default now). A remote coordinator gets the token only through `coord_url` or `WAVECOORD_URL`, never from a repo's `.claude/wavecoord.json`; set one of them for `gate-check` in CI too.
- The fence mode and `spec.verify_at_merge` count only on the default branch's `.claude/wavecoord.json`, not in the lead's working copy.
- Task PRs may no longer change Claude Code config (any settings key, anything under `.claude/` or `.claude-plugin/`, `.mcp.json`, `CLAUDE.local.md`), in advisory mode too. Make these changes in a lead or plan PR.
- A submodule change is a changed path: its lease must cover the submodule path itself (`vendor/sub` or `vendor/**`).
- Agent tokens can no longer create leases, and their hot marks are literal paths. `watchdog` and `server` are reserved sender ids.
- The spool and the lead cursor are kept per coordinator. An old `spool.jsonl` is not replayed, and a lead session starts at the head of the log.
- `migrate-plan` needs the coordinator, `--states FILE` or `--no-coord`.
- Requirements whose inputs have `from` change fingerprint, so their tasks are suspect once (`wavecoord spec rebase-basis`).
- Plan changes can no longer set `in_progress`, `in_review` or `done`. In CI, `spec check --base` accepts the status writer's commits by their `Wavecoord-Status:` lines; give the `wavecoord/status` branch the same review rules as other bot branches.
- Skills pre-approve only read-only `wavecoord` commands; the others ask each time.
- The plugin option `project` is gone. Workers get the new `pre-merge-commit` guard in each new worktree.

### Security

- The refinery runs the fence check (the `Lease:` line, config and spec changes) before `check_cmd` and before a spec tree task's Acceptance and Verify, so a PR that is refused never runs its code on the lead.
- Verify and Acceptance run with a scrubbed environment: `PATH`, `HOME`, `LANG`, `LC_*`, `TMPDIR`, `TERM`, `TZ`, `USER`, `LOGNAME`, `SHELL` and the new `spec.verify_env` names; never `WAVECOORD_*`, `GIT_*`, `GH_TOKEN`, `GITHUB_TOKEN`, `SSH_AUTH_SOCK` or the Anthropic keys. This is not a sandbox; `docs/security.md` has the trust model.
- A task PR may not change any lead config key: `pr_cmd`, `pr_state_cmd`, `coord_url`, `project`, `log_branch`, `default_provider`, `auto_resolve`, `branch_prefix` and the hosts' `ssh`, `repo_path`, `prepare_cmd`, `providers`, `labels` and `dispatch` are now `config_change` too (seeded defect D27).
- `gate-check` runs the config and spec checks before it reads the `Lease:` line, so a PR body with no `Lease:` line, or with two, can no longer skip them (D26). The refinery refuses a missing or doubled `Lease:` line like a stale one (a PR body that `gh pr view` cannot read is an E5 problem instead: `enforce` blocks, `advisory` notes).
- The refinery takes the fence merge mode and `spec.verify_at_merge` from the config on the default branch, never from the lead's working tree.
- Predicate S (a task PR may not change the spec tree) normalizes `spec.dir` (D28), also uses the spec dir at the merge base, so a tree that moved meanwhile is still covered (D29), and compares paths without case (D30).
- **Claude Code config in task PRs** (`config_change`, design section 8): the settings files deny by default, so a change to any key but `$schema` (`permissions`, `statusLine`, `apiKeyHelper`, `enableAllProjectMcpServers` and every other) is refused, not only the five hook keys (D32). Any other file under a `.claude` or `.claude-plugin` folder at any depth (agents, commands, skills, a nested `pkg/.claude/settings.json`), every `.mcp.json` and `CLAUDE.local.md` are lead config too. Paths compare without case, so `.claude/Settings.json` or `plugin/Hooks/hooks.json` no longer pass on a case-insensitive checkout (D31).
- The lead's repo config lookup reads `.claude/wavecoord.json` only at the repo's top level: a nested `pkg/.claude/wavecoord.json` no longer becomes the config (and the `coord_url`) of a session started in `pkg`.
- Submodule (gitlink) changes are paths at E2, E3, E5, the Stop hook and the refinery: every fence diff uses `--ignore-submodules=none` and `-c diff.ignoreSubmodules=none`, so `ignore = all` in `.gitmodules` no longer hides them (D33). A lease must cover the gitlink path (`vendor/sub` or `vendor/**`).
- E1 checks a file in a nested repo of another repo (a submodule checkout, a vendored clone) by its path in the worker's worktree, not by the nested repo's own path (D34).
- E1 compares the protected local folders (`~/.claude`, `$WAVECOORD_HOME`, the plugin root) without case on a disk that folds case, and a `.GIT` segment is git internals (D35).
- The refinery's protected-path check lists both names of a rename (`--no-renames`), so moving a protected file out of its folder needs the approval; a git error there blocks instead of passing.
- The pre-push guard refuses in enforce mode when git cannot compute the pushed paths, and reads the new commits when the remote head is not in the local repo (a forced push over a moved branch) (D37).
- Schema patterns follow JSON Schema: `$` matches only at the end of the string, so `t1\n` is no longer a valid task id.
- **An agent token acts only within what the lead dispatched** (`docs/design/edit-fence.md` 2.3, `contract.md` section 4). A claim may only renew the lease the lead granted to its holder, or resume a parked lease of the same attempt on the same host with the same grants (seeded defect D39): a worker can no longer claim `**` for a task the lead never dispatched, or widen a resumed lease and pass E5 through the resolver lineage. Hot marks are one path, no `*` or `?` (D38). A report must come from the attempt's holder and is refused for a `merged` or `abandoned` attempt; `merged` stays final in the fold. An agent's `pr_url` is ignored: the PR of an attempt is the one the lead opened. `collect` no longer takes a PR URL from the host's `result.json` or `status.json` (the worker can write both), and opens the PR for the branch the lead launched, not the branch the host reports.
- Idempotency keys are scoped per principal, so a host can no longer take another host's report key (`report:<task>:<n>`) and drop its report. The lead's collect re-post still deduplicates against the host's report.
- Agent ids are checked: `lead`, `ui`, `watchdog` and `server` are reserved in any case and with surrounding white space, and an id must be a plain token (`[A-Za-z0-9][A-Za-z0-9._:@+/-]*`, at most 128), so a worker cannot send as the watchdog or put a fake frame in its sender.
- The hooks and the CLI send the user's token only to `WAVECOORD_URL`, the plugin option `coord_url` or a loopback URL, never to a `coord_url` that only a repo's `.claude/wavecoord.json` names.
- The lead's records are redacted before they are logged: secret env values, the coordinator token files and common token formats in `check_cmd`, Verify and `merge_cmd` output no longer reach `coord/log` (`redact.py`).
- A remote host's probe sends no token to `/api/health` and checks that the server proves it is the coordinator (a challenge answered with HMACs keyed by the host's token digests); the launcher refuses a host where another process holds the coordinator port.
- Local workers without `~/.wavecoord/hosts/<host>.token` get an agent token provisioned; the wrapper never falls back to the lead's token.
- `tokens provision --out` writes a mode-600 file also over an existing file with a looser mode.
- The HTTP front: a socket timeout (`REQ_TIMEOUT_S`), at most `MAX_CONNS` connections, the token checked before the body is read, and chunked bodies refused (411), so idle connections can no longer exhaust threads and file descriptors, and a chunked body never runs a request with its defaults.
- `tests/test_generic.py` also catches GitHub fine-grained, GitLab, Slack, OpenAI, Google and AWS STS keys.
- Launched workers get no tool of the plugin's MCP servers (the worker settings deny `mcp__plugin_wavecoord_wavecoord` and `mcp__plugin_wavecoord_lead-feed`): Claude Code fills their token from the plugin option, which can be the lead's.
- The plugin option `coord_url` has no default, and the hooks act only with `WAVECOORD_URL` set or in a repo with `.claude/wavecoord.json`, so sessions in other repos no longer heartbeat or post notices to the coordinator with the lead token.
- The PR body quotes a line of the worker's text that starts with `Task:`, `Attempt:` or `Lease:`, so a worker cannot add a second header and block its PR.
- When the worker pushed its branch itself, the launcher still runs the fetch, merge-tree and E3 on it, so the report has its E4 fields and an E3 refusal fails the attempt.
- A zipapp upload is checked against its hash before it takes the build's name; a cut transfer no longer leaves a broken build that is never uploaded again.

### Added

- **Setup in two commands.** `wavecoord init` sets up a repo: it writes `.claude/wavecoord.json` (or adds only the missing keys to an existing one), makes the lead token and the local host's agent token when their files are missing (never over an existing file), and on request installs the server service (`--server`), creates the spec folder (`--specs`), sets the plugin options with `claude plugin configure` (`--configure-plugin`; the token is never printed unless `--show-token`) and opens a config PR on `wavecoord/setup` (`--pr`). `--dry-run` writes nothing; `--interactive` asks on a terminal. It ends with `wavecoord doctor --quick`.
- `wavecoord doctor` checks every prerequisite, one line each (`ok`, `warn`, `fail`) with the command that fixes it: Python, git, origin and its default branch, `gh` and its login (or a custom `pr_cmd`), the claude version and login (`claude auth status`, no model call), the plugin and its options, the config (valid, committed on the default branch), `coord_url`, the coordinator (reachable, same version, the token accepted), the token files and their modes, the hosts (the `hosts` probe; `--quick` skips it), the server service and its pinned build, and the plan store with the reason. `--json` prints the checks; the exit code is 1 when one fails.
- The `/wavecoord:setup` skill runs doctor, explains each problem, runs `wavecoord init` after asking, re-checks and points to `/wavecoord:spec`. It pre-approves only `wavecoord doctor`, `wavecoord init`, `wavecoord status` and read-only git.
- The quickstart starts with `wavecoord init` and `/wavecoord:setup`; the manual steps moved to its end.
- `wavecoord lease release <task|id>` and the lead tool `lease_release`: release a stale or parked lease so the task can be dispatched again (a lost worker's lease used to hold its task for good). The triage skill uses it.
- `wavecoord spec import --from <speckit|kiro|openspec>`: reads GitHub Spec Kit, Amazon Kiro or OpenSpec files and writes them as draft records under one capability, through the op API, so the result passes `spec check`. Statements become EARS where the rewrite is mechanical; task items with file paths become tasks with `paths` and `needs`; each record names its source file, which is never changed. It reports what needs a person (statements that are not EARS, tasks without paths, unknown sections) and a Verify warning per requirement. `--dry-run` prints the diff, `--dry-run --json` the ops for `spec_edit`; a re-import changes nothing or is refused. `docs/import.md` has the mappings and their gaps.
- **The course** (`docs/course/`): spec-driven development with wavecoord for beginners, in eleven lessons (what SDD is, install, first spec, a new project, an existing project, importing Spec Kit, Kiro or OpenSpec, good specs, running, steering, triage and recovery), each a short page, a narrated video of a real Claude Code session and an exercise, with a choose-your-path map. `scripts/course/` has the demo projects (`demos/demos.py`: `fresh`, `brownfield` in four states, `speckit`, `kiro`, `openspec`, each with a local bare origin), a local stand-in for GitHub (`demos/bin/gh`), the lesson scripts and `build.py`, which builds the demo, records the lesson with the rig, adds title and diagram cards, cuts long waits and writes the MP4 and SRT to `dist/course/`. `build.py --validate` checks every lesson against the rig's format. The rig gains a `wait_shell` step and a `check` command, gives `shell` steps the lesson's environment, and reads token usage from the lesson's `CLAUDE_CONFIG_DIR`. The guide's triage table adds the `branch_missing` and `merge_failed` holds, and the `spec new` example in `docs/specs.md` gives the Statement a requirement needs.
- The skill `/wavecoord:spec-import`: finds Spec Kit, Kiro, OpenSpec or free-form specs (PRDs, BMAD documents, ADRs) in the repo, shows the mapping, asks about granularity and scope, and opens one plan PR with `spec_edit`. `/wavecoord:spec` points to it when it finds such files.

### Fixed

- Task prompts from `/wavecoord:wave-plan` no longer tell workers to push. A launched worker may not push (the launcher does), so such a prompt made it report `blocked`. The reviewer agent, the risk table and the watchdog nudge say the same.
- `wavecoord launch --continue` keeps the earlier run's model, effort, turn and budget limits when they are not given; it ran on Claude Code's default model before.
- `--continue` resumes the earlier session when the recorded worktree path and git's differ only by a symlink (`/tmp` and `/private/tmp` on macOS); it started a fresh session before.
- A usage warning from Claude Code (`rate_limit_event` with status `allowed_warning`) no longer counts as a rate limit, so it does not back off the provider and hold the next wave.
- `/wavecoord:spec` and `/wavecoord:wave-plan` write spec ops to a file for `wavecoord spec apply --dry-run`, instead of a heredoc that made Claude Code ask for permission.
- Verify and Acceptance run on the merge of the default branch and the PR head, not on the head alone, so work merged since the branch started is in the checked tree.
- Verification fails closed: a task or requirement record that is missing or does not parse at merge blocks with `verify_unavailable`. `verified` is recorded only when a Verify command ran.
- A `dropped` or `on_hold` sibling no longer defers a requirement's Verify: the last merging task runs it.
- A Verify or Acceptance command with a trailing `# comment` no longer fails every merge.
- Predicate S applies only when the repo uses the spec tree, so a flat-plan repo with its own `specs/` folder is not refused with `spec_change`.
- `spec.dir` values such as `./specs`, `specs/.` or `docs//specs` no longer cut characters off the record ids; the schema now rejects `.`, `..` and `//` in it.
- **Status commits** (`docs/specs.md`, Status; `docs/design/spec-layer.md` 7.2):
  - The writer reads the task files at the exact sha it commits on, never at a moving ref or the working tree, and refuses a write that changes more than the `status:` line. A plan PR merged during a flush is no longer reverted.
  - The status line is found in any form the parser reads: quotes, a trailing comment (kept), CRLF (kept).
  - The writer writes only over `planned`, `in_progress` and `in_review`; `draft`, `on_hold` and `dropped` are never overwritten, also while an attempt runs (listed under `held`).
  - A parked lease counts as `in_review` only when its attempt has a PR.
  - `spec status-sync --reset <task>` (and `spec_status_sync` `reset`) sets a rolled-back `done` back to `planned` when the coordinator has no merged attempt.
  - The status PR: the branch `wavecoord/status` is no longer deleted under its open PR; it is force-pushed with a lease and rebuilt only when the base or the changes move. A merged or closed status PR is replaced. `merge_cmd` gets `WAVECOORD_PR`, `WAVECOORD_SHA` and the other variables, as in the refinery.
  - A push failure is classified: a lost race is retried after a fetch with backoff, a refusal goes to the status PR, anything else is blocked.
  - A flush never raises, so `dispatch`, `collect` and `refinery_merge` no longer fail after their side effects. `spec.status_blocked` is logged once per distinct reason.
  - A non-forced flush never waits for a running one, and a forced one is queued behind it, so dispatch and the refinery do not stall. The status loop follows `spec.status_flush_s`.
- **Bot commits** (status, `spec_edit`, `plan_edit`) do not run the consumer's git hooks, and their worktree add and remove take the worktree admin lock.
- **`spec_edit`, `plan_edit`**: a reused branch that is merged, or whose PR is merged or closed, is refused; one without a PR gets one. A delete then a move into the freed id works. A move then a delete of the same record deletes it. Writes through a symlink under `spec.dir` are refused. The PR body reports the errors already on the base instead of "no errors".
- **Spec tree in the lead.**
  - The lead reads the spec tree only from the default branch, never from its working tree, so uncommitted spec files are never dispatched or pushed by a status commit.
  - With no `plan.adapter`, the spec tree is the plan only when `spec.dir` on the default branch holds a valid record; a stray `specs/openapi.yaml` keeps the flat plan. The `config` tool shows the store in use (`plan.selected_by`) and the `spec` defaults.
  - Coordinator state follows a task through its aliases: a moved task keeps its live or merged state (readiness, `plan_graph`, status commits) and is not dispatched again. `plan_get` and `dispatch` take a tree id or an alias.
  - `conflicts_with` is enforced: a task is not ready while a task it conflicts with runs or is in review, and `dispatch_wave` never starts both.
  - A task under a folder without `_node.md` no longer crashes `plan_graph`, `plan_get` or `plan_ready`; it is not ready, with a reason.
  - A task left `in_progress` or `in_review` in the files with no live attempt is ready again.
  - `attempt.start` records the task's `basis`; the worker prompt names the task that makes each input.
  - `spec_tree` progress and `spec_query` use the coordinator's state.
- **migrate-plan.**
  - Carries the coordinator's state (`--states FILE`, `--no-coord`, else it asks the coordinator): merged tasks are `done`, running ones `in_progress`, in review `in_review`; `blocked` becomes `on_hold`.
  - Keeps every old id as an alias (aliases may now hold any flat task id), fits long ids within 64 characters, and makes a valid tag of a phase that starts with `_`.
  - Gives open tasks with overlapping paths `conflicts_with` instead of refusing the plan.
  - The legacy requirement is approved and its capability and feature active, so the migrated tasks run; `docs/specs.md` says what to review and commit.
- **Spec records never lose data.** `spec fmt` and every op refuse a record whose `kind` is unknown or not its path's, that has unknown keys, or whose list key holds a scalar or mixed types; the file is left as it is and reported (`cannot format`, exit 1). Code fences follow Markdown (``` and ~~~, closed by a run at least as long; ```x``` is inline code). Rendered scalars always read back: control characters are quoted, flow items with `,` `[` `]` `{` `}` are quoted, floats are written as digits.
- **Readiness fails closed.** A need that does not resolve, a needed record that does not parse or breaks its schema, and a need on a node with no tasks yet keep dependents waiting; one malformed field no longer crashes the whole plan (the record is reported and left out). Dropped tasks neither block a need on their subtree nor count as producers. `suspect: refuse` treats a task without `basis` as suspect. A superseded requirement's tasks do not run; `supersedes` on itself or in a cycle is an error.
- **Plan changes cannot set coordinator statuses.** The op API (`spec_edit`, REST, the CLI) refuses `in_progress`, `in_review` and `done` (also in `create` fields), any change of a `done` task's status, and any change of a live task other than `dropped`; the public `coordinator` op flag is gone. `wavecoord spec check --base <ref>` reports such a change in a hand edit.
- **Moves keep tasks current.** The fingerprint leaves out an input's `from`, so moving a node no longer makes the tasks of its consumers suspect (tasks whose requirement has such inputs show suspect once after the upgrade: review them, then `rebase-basis`). Moving back to an old id drops that alias. A task created in a batch gets its basis after the batch's last op.
- `waves` lists only tasks that can run (or are running); held tasks come with a reason (`held`), and an unknown node is an error (`not_found`). A4 accepts a producer reached through other tasks. Cycle detection no longer hits the recursion limit. `paths` like `./src/a.py` or `src//a.py` are errors and compare as `src/a.py`.
- `/wavecoord:spec` can open its PR before tasks exist: A1 and A3 only warn while no task file is under the node.
- Ops: line breaks in one-line values, malformed scenario steps and moves of a subtree with stray files are clean errors; the Statement is never split into sentences, and the splitter keeps numbered lists, indented lines and abbreviations (`e.g.`, `approx.`, `v2. 0`). `delete --force` says what it does (children, not references). `spec query` with an unbalanced quote is a clean error.
- A symlink under the spec directory is a layout error in the working tree and in a git ref; no tool reads or writes through it.
- Skills: wave-plan and the planner no longer copy the requirement's Verify into every task's Acceptance (the refinery runs it with the last task); wave-plan reports the waves of the plan branch with `wavecoord spec waves --ref <commit>`; wave-run maps tree ids to coordinator ids; triage sends spec changes through `/wavecoord:revise` (`spec_edit`); revise reads a requirement's change from its history. docs/specs.md names only commands that exist. The worker prompt says the requirement's Verify may fail until the last task merges.
- Seeded defects S12-S15.
- Predicate S uses the lead's rule for the spec tree: with no `plan.adapter`, only a `spec.dir` that holds a spec record is the tree, so a task PR may change a stray `specs/api.yaml` in a flat-plan repo. A task PR that adds the first spec records is refused.
- An offline-override journal of any size can be flushed: the batches are cut by size (half the server's body limit), not by count, so a large journal no longer gets a 413 forever and blocks every push on the host (D36). One reachable flush sends batches until the journal is empty.
- The Stop hook reads the changed paths with `-z`, so a non-ASCII file name no longer makes the server refuse the batch and drop the other outside-lease warnings; a path that no glob can hold is reported as outside the lease without a server call.
- `guard install` also writes a `pre-merge-commit` guard. The docs now say that git runs no commit hook for `cherry-pick`, `rebase`, `am` or a fast-forward merge; E3 and E5 check those commits.
- Seeded defects D31-D39.
- A failed group commit of `coord/log` (a stale `index.lock`) is retried with a backoff instead of refusing writes until a restart.
- A forked `coord/log` (two servers on clones of one `origin`) is refused at start and after a rejected push, with both heads, instead of pushing nothing forever with `ok: true` (`rollback.md`, "A forked log").
- The hooks spool keeps entries the server answers with 5xx, 408 or 429, keeps their order when hooks spool during a replay, and is per coordinator (`spool-<hash>.jsonl`), so writes for one coordinator are never replayed into another. Entries in an old `spool.jsonl` are not replayed.
- The watchdog's idempotency keys are unique per idle episode, so a lease revived without new activity is nudged and marked stale again.
- `rebuild --at` keeps a revert marker only for an event after the rollback point, so a reverted message that comes back can be reverted again.
- `GET /api/events` refuses a negative `since` or `Last-Event-ID` (it busy-looped).
- `NaN` and `Infinity` in a body are 400, and the schema validator rejects them as numbers; a `$` at the end of a schema pattern no longer accepts a trailing newline.
- A body that is not a JSON object, or a malformed lead request, is 400 instead of 503.
- MCP `tools/call` checks the arguments against the tool's `inputSchema`; `conflicts` with `globs` as a string no longer answers "no conflicts".
- With a wildcard bind and no `coord_url`, workers get the first `allow_hosts` name (or loopback), not `http://0.0.0.0:<port>`.
- A lead line quotes a value with a trailing newline instead of breaking the line.
- **The refinery merges only what should merge.** It merges an attempt only when its report is `done` (`failed`, `blocked` and `needs_decision` block with `report_not_done`) and its task is not `dropped`, `on_hold` or `draft` on the default branch (`held`). One item whose check cannot run (git, `protected_paths_cmd`, the plan) blocks with `unavailable`; the loop goes on, and the merged items' status is flushed. Requirement Verify follows a sibling's aliases (a moved task merged under its old id counts as done), and a sibling record that does not parse blocks with `verify_unavailable`.
- **Resolvers.** The refinery starts a resolver only with `auto_resolve`, and `MAX_RESOLVES` counts resolver runs per attempt (a new conflict id per round no longer resets it). `resolve` is refused while a run of the attempt is in flight, and a resolver resumes the parked lease instead of stealing a live one. `resolve` takes `brief`, the user's decision, which the resolver reads first.
- **Rate-limit fallback.** `collect` starts the fallback attempt through the normal readiness check (no `force`), so a task dropped, held or merged meanwhile is not started again; the provider comes from the lead's chain, not the host's result. A rate-limited resolver keeps the attempt `reported` with its PR and runs the resolver again on the next provider, instead of a new full attempt.
- **Status commits.** `wavecoord spec check --base` passes the status PR (status lines only, each named by a `Wavecoord-Status:` line) and the output of `migrate-plan` (new tasks with a flat id alias), so the documented CI check no longer refuses them. After a restart the writer adopts its open status PR instead of failing on a second one. `spec.status_blocked` is logged under the `status-writer` actor, so it reaches the lead's feed.
- **Spec tree in the lead.** `SpecTreePlan.get` resolves a migrated flat id with dots before a node with the same path. A read error of the default branch fails closed (`unavailable`) instead of planning from `plan/tasks`. `spec_tree` and `spec_waves` say when `plan.adapter` keeps the tree from being dispatched (`plan_note`). `spec_edit` refuses a new task under the id of a deleted task that the coordinator merged or runs, warns (`switch:`) when its PR adds the first spec records while `plan/tasks` has tasks, and rewrites the PR body for the whole branch on a later call. `wavecoord spec apply - --dry-run --ref <ref>` checks ops against a ref.
- `plan_graph` and plan validation no longer hit the recursion limit on a long needs chain (`plan/graph.py` cycles and critical path are iterative).
- Skills: triage and wave-run call `collect` (PRs now open in the plugin workflow); triage names a retry path for an attempt that has a PR (continue it, or close the PR and `collect`) and the new holds; wave-plan and the planner create tasks `planned`; the spec, wave-plan and wave-run skills check `plan.selected_by`; the resolve skill and its template match the real resolver (same branch, `brief`).
- **Launcher.**
  - A new attempt that fails before its wrapper runs moves its state to `attempts/<project>/.failed/` and removes its worktree and branch; the attempt number counts the hosts' attempt dirs and local branches (`for-each-ref` now uses a glob). A failed launch no longer makes every later dispatch fail with `already exists`.
  - `attempts stop` never writes a final state while the wrapper holds its run lock (it answers `stopping`); a held run lock takes a slot and keeps gc away whatever `status.json` says. A stop kills a running `prepare_cmd` (its own process group). When finish fails, the wrapper still posts a `failed` report and the finish.
  - The finish fetch takes the worktree-admin lock and retries, and a push error that is not a refusal is retried, so a concurrent fetch no longer fails a finished attempt.
  - Heartbeats go on during `prepare_cmd` and the push steps, so a long build no longer makes a healthy lease stale.
  - Every remote command touches its build, so the 30-day cleanup never removes a build in use; the wrapper reads the report schema and the modules it needs at start.
  - Worktrees are `<repo_path>/../wt/<project>/<task>-<n>`, so repos side by side do not collide (a `--continue` run reuses a worktree at the old path).
  - The `ssh -R` tunnel is a connection of its own (`ControlMaster=no` now comes before the shared options).
  - Remote scripts run as `sh -c '<script>'`, so a host whose login shell is csh, tcsh or fish works.
  - A `--continue` run (a resolver) that adds no commit still gets the merge-tree check, so it is not counted clean while the branch conflicts.
- **Steering.**
  - A lead steer action acts only on the run that held the task's lease when it was sent: an old `steer stop` no longer stops every later attempt.
  - The hooks of a launched worker never show or ack a steer action, so the wrapper always gets it.
  - The inbox relay no longer acks a message because the socket write went through; the hooks ack it when the transcript shows it and deliver it themselves otherwise.
  - The STEER_WAIT_S close guard counts only turn lines (not control responses, hooks or state changes), and a control alone (a model switch with no note) gives no extra turn: a run no longer hangs until its timeout and ends `timed_out` with a valid report.
  - `steer interrupt` without a note stops the turn, not the run: a line that says to go on follows the interrupt.
  - A steered turn after the report that fails or writes no report no longer replaces the worker's valid report (`late_turn` in the result).
  - An error answer to a lead's control request (or none) is recorded in `steer.jsonl` and sent to the lead as a `steer_failed` notice.
  - The stuck detectors: `loop` keys on the whole tool input and counts assistant messages, not parallel calls; `failing` counts failures in a row and starts again after a success or an edit.
- **Hooks.**
  - The `Stop` hook no longer tells a launched worker to push.
  - Injected text is fitted before anything is acked: a message that does not fit stays unacked, and the lead cursor moves only past the event lines that went in.
  - Hook state, the relay files and the lead cursor are per coordinator (`<state dir>/coord-<sha(url)>/`), so a second coordinator's message ids are not taken as shown.
- **Setup** (found while writing the course).
  - `wavecoord init` and `wavecoord doctor` put and look for host tokens where the launcher reads them, `$WAVECOORD_HOME/hosts/` when it is set (one function, `launcher.common.host_token_file`), not always `~/.wavecoord/hosts/`.
  - `init --server` starts the service on the port of the config's `coord_url` (`serve --port`), not always on 18741; for a `coord_url` with another host it prints the `install-server --bind` command instead of widening the bind.
  - `WAVECOORD_HOME` is the launcher's root folder but one project's state folder for the server; `docs/launcher.md` says so, and doctor's new `homes` line names both folders.
  - A `claude --plugin-dir` session's plugin (`wavecoord@inline`) is no longer reported as not installed: doctor finds it through `CLAUDE_PLUGIN_ROOT` or `--plugin-id`, and `init --configure-plugin --plugin-id <id>` configures that id (in a `--plugin-dir` session it picks `wavecoord@inline` by itself).
  - doctor no longer says `gh` is "not needed" when `pr_cmd`, `merge_cmd` and `pr_state_cmd` are set: the refinery's fence check, a resolver's `Lease:` line and plan PR bodies still use `gh` (`warn`; `fail` in fence merge mode `enforce`). `docs/adapters.md`, "Other forges", lists these calls.
  - The refinery's merge order between tasks that unblock the same amount of work is the attempt that finished first, then the task id (unchanged, now documented and tested).

### Changed

- `refinery_merge` refuses `approved_protected` without `pr`: an approval covers one PR the user saw.
- Skills pre-approve only read-only `wavecoord` commands (`status`, `events`, `hint`, `attempts logs`, `spec` reads); `launch`, `steer`, `refinery`, `revert`, `rebuild`, `tokens`, `gc` and `spec apply` ask each time.
- The README and quickstart config no longer set `"plan": {"adapter": "builtin"}`, which kept the spec tree from being dispatched.
- The default `pr_state_cmd` also asks for `url`.
- The plugin option `project` is gone: nothing read it, and the project comes from `.claude/wavecoord.json`.
- The lead cursor moved to `<state dir>/coord-<sha(url)>/lead-cursor.json`. After the upgrade a lead session starts at the head, without the old backlog. `wavecoord events --for lead --state-dir` takes the same base dir and finds the subdirectory from `--url`. Run one lead session per coordinator.
- The plugin option `coord_url` must be set (it had the default `http://127.0.0.1:18741`).
- Hints (plain messages) to a task still reach every later run of the task that has not seen them; this is now documented (`launcher.md`, "Listen and steer").
- CLI text output (the `--json` outputs are unchanged): `attempts ls` sizes its columns to the content and shows the PR that `collect` opened; `refinery merge` adds a short detail to a blocked line (the paths outside the lease, or the report status) and drops the trailing space after `merged`; `refinery status` and `spec tree` size their name columns to the content; `plan` points to `wavecoord spec ready` / `spec waves` when the spec tree is the plan; `hint` says "within a few seconds" for a short `INBOX_POLL_S`; `gate-check` prints its verdict on stdout too, in words (`fail: 1 path outside the lease: README.md`). The refinery's fence hold detail lists the E5 paths first.
- Docs: the README is rewritten as the front door, with an architecture diagram and a wave sequence. The new `docs/guide.md` teaches the workflow (the model, one feature from idea to merge, writing specs, safe waves, steering, triage, recovery, upgrade notes), and `docs/README.md` maps the pages. The quickstart config sets `coord_url`, which the terminal CLI needs now that nothing has a default URL.

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

- **A too-large request could reset the connection instead of answering 413.** The server refused a body over `REQ_MAX_BYTES` before reading it, so a client still writing it got `Connection reset by peer` (seen in CI on macOS with Python 3.12). It now reads and drops up to 1 MiB of the body first, then answers 413 and closes the connection.
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
