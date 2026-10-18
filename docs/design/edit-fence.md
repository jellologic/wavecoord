# Design: the fail-closed edit fence (P6)

Status: design for review. No code yet. The test plan is in `tests/fence/README.md`.

The fence turns the advisory checks of P2 (`docs/hooks.md`) into checks that refuse a write. It is the only part of wavecoord that may stop a session on purpose, so a bug in it stops every worker. This note gives the invariant as a checkable predicate, the enforcement points, the failure modes, the races, the budget, the tests and the rollout. "Must" marks a rule that the implementation PR has to meet and the tests have to check.

## 1. Scope and threat model

**Actors.** Workers (Claude Code sessions that the launcher starts, one lease each, one worktree each), the lead (one interactive session, no lease), other sessions (a session in a repo that has a coordinator configured, but no task and no lead role), and the human.

**The fence prevents these accidents:**

| ID | Accident | Example |
|---|---|---|
| T1 | Two agents write the same exclusive path | Worker B edits `src/auth/session.py` while worker A holds `src/auth/**` exclusive |
| T2 | An agent writes outside its lease | Worker A, leased to `src/auth/**`, "fixes" `docs/api.md` on the way |
| T3 | A stale holder writes after a steal or a release | The lead steals task `api-auth` from A to C; A is still running and keeps editing and committing |

The adversary is a **confused or careless agent**: it follows its prompt, makes mistakes, and may try the obvious next thing when an edit is refused (for example, a write with Bash). It does not try to defeat the fence on purpose.

**The fence does not prevent:**

- **Malicious agents.** An agent runs as the same OS user as its hooks and its launcher. With Bash it can change git config, delete hooks, edit wavecoord state files or kill processes. Section 4.4 says what this means for the override. An OS boundary (Claude Code sandboxing, or a separate user per worker) is the only defence, and it is outside P6.
- **Shell writes before commit time.** `PreToolUse` sees only the edit tools. A write with `sed -i`, `>`, `python`, and so on is not seen until the paths are staged, committed and pushed. In a worker worktree this is safe: the write stays in that worktree until a commit reaches a guard. In a shared checkout (the lead, other sessions) a shell write can clash with no warning.
- **Files that git does not track.** Paths outside every worktree of the repo, and paths that git ignores, never reach a commit. The fence does not check them, except for the protected local paths of section 3.6.
- **Semantic conflicts.** Two leases with disjoint paths can still break each other. That is the job of the merge gate's checks (`check_cmd`), not of the fence.
- **Writes by a human.** The fence checks agents only.

**Physical vs logical clashes.** Each worker has its own worktree, so two workers never write the same file on disk through their own paths. A clash on the same file is logical: it shows up when the branches meet. So the authoritative checks are the push check and the merge check (section 3). The edit check stops the mistake early and keeps the worker from wasting work. A physical clash is possible only through an absolute path into another worktree (section 3.6) or in a shared checkout.

## 2. The invariant

### 2.1 Definitions

All paths are repo-relative, with `/` as separator, after the normalization of section 3.6.

- `covers(g, p)`: path `p` matches glob `g`. The semantics are those of `glob_match` in `src/wavecoord/hooks.py`: `*` is any run of characters other than `/`; `?` is one character other than `/`; `**/` is zero or more whole directories; `**` elsewhere is any run of characters; every other character, `[` included, is literal; and a match of `g` also covers every path below it (`g` can name a directory). `p` is always literal: a `*`, `?` or `[` in a file name is not a wildcard. The implementation must move this function into `src/wavecoord/globs.py` as `covers`, and the server must use it.
- `overlap(a, b)`: exactly `globs.globs_overlap(a, b)` of `src/wavecoord/globs.py`. It compares the literal prefixes before the first `*`, `?` or `[`. It can report an overlap that does not exist, but never misses a real one. For a literal path `p` with no wildcard character, `covers(g, p)` implies `overlap(p, g)`.
- `R(L)`: the **grant set** of lease `L`. These are the live reservations of `L` with `origin` in {`claim`, `lead`}. A hot-file mark (`origin: hot`, from `PostToolUse`) is **not** in `R(L)`. Today a reservation has no `origin`. The implementation must add the field (schema, fixtures, `contract.md`), and treat a reservation without the field as `claim` if a `lease.grant` or `lease.steal` event made it, else as `hot`.
- `X(L', t)`: the exclusive reservations of lease `L'` that are live at time `t`. A reservation is live while its lease is not `released` (so `active`, `stale` and `parked` all count).
- `lease(A, t)`: the one non-released lease that agent `A` holds at time `t`, or none.
- `epoch_now(task, t)`: the epoch of the non-released lease of `task` at `t`, or none if the task has no non-released lease.
- `claim(W)`: the `(task, epoch)` pair that the writer states. For a worker it is `WAVECOORD_TASK` and `WAVECOORD_LEASE_EPOCH` from the launcher, which never change during the session. At merge time it is the `Lease: <task>@<epoch>` line of the PR, checked against the attempt record on the server.

### 2.2 The predicate

A **write** `W` has an agent `A`, a path `p`, a claim `c` and a decision time `t`. The writes are:

1. the target of an `Edit`, `Write`, `MultiEdit` or `NotebookEdit` call;
2. each path that a commit changes (staged paths, with renames split into a delete and an add);
3. each path in the net diff of a push (`merge-base(default, head)..head`, renames split);
4. each path in the net diff of a merge.

`t` is the moment the server evaluates the check, under its write lock. For a worker write, `ok(W)` is true when all of these are true:

```
no_lease:           L := lease(A, t) exists
                    and c.task = L.task
stale_epoch:        c.epoch = L.epoch = epoch_now(L.task, t)
outside_lease:      exists r in R(L): covers(r.glob, p)
foreign_exclusive:  for all leases L' != L not released at t,
                      for all r' in X(L', t): not covers(r'.glob, p)
```

The name at the left of each line is the violation kind that the server reports when that line is false. `no_lease` is a new kind: today a holder whose lease was released, but not stolen, passes the epoch check, because `last_epoch` still equals its epoch. The fence must close that gap.

**The fence invariant.** For every write `W` by a worker that an enforcement point accepts, `ok(W)` is true at the decision time of that point. The one exception is a write accepted under the human's offline override (section 4.4). That write must be logged, and the merge check must still check it.

**The integration invariant** (the property that matters). For every path `p` in the net diff of a merge of attempt `(task, n)` into the default branch, `ok` is true at the decision time of the merge check, with `A` = the attempt's agent and `c` = the attempt's lease. The offline override has no effect on this check.

**Liveness, so that "deny all" does not pass.** When the coordinator answers in time and `ok(W)` is true, the fence must accept `W`. The randomized test checks this too (section 7).

**The implementation may deny more than the predicate.** It may use `overlap(p, g)` in place of `covers(g, p)` for `foreign_exclusive` only, and only for a path with no `*`, `?` or `[` in it. Such a false alarm is safe. It must never use `overlap` for `outside_lease`, because there the over-match lets a write through (for example `overlap("src/authz/x.go", "src/auth*.py")` is true). The current server uses `globs_overlap` for both, and P6 must fix that.

### 2.3 Shared and exclusive

- My own shared and exclusive reservations both count for `outside_lease`.
- A foreign **shared** reservation never blocks a write. Shared means that concurrent writers are expected, and the merge handles them.
- A foreign **exclusive** reservation blocks every other lease, also one that holds a shared reservation on the same path.
- **Grants.** With the fence in `enforce` mode, the server must refuse a reservation that conflicts with a live reservation of another lease (overlap, and at least one of the two exclusive) with 409 `conflict`. The lead may force it with `force: true`, which is logged. Without this rule, two leases that each hold an exclusive on the same path would block each other forever. `plan_ready` and `dispatch_wave` already avoid such overlaps, so this rule should almost never fire.
- **Who may widen a lease.** With an `agent` token, `POST /api/reservations` may add only `origin: hot` shared marks. A new `claim` or `lead` grant needs the lead token. Otherwise a worker could reserve `**` exclusive and pass every check.

### 2.4 Lead and other sessions

The lead and other sessions hold no lease, so `no_lease`, `stale_epoch` and `outside_lease` do not apply to them. For their writes the fence checks only `foreign_exclusive`. Their default action is `ask`: the `PreToolUse` hook returns `permissionDecision: "ask"` with the reason, so the human decides. In `-p` mode Claude Code denies a call that nobody can answer. The config key `fence.lead` (`warn`, `ask` or `deny`; the default is `ask`) sets this. These writes are outside the fence invariant, but the merge check still covers what they commit if it goes through the gate.

A **subagent** of a worker fires the same hooks with the same environment, so it acts with the worker's lease. A subagent with `isolation: "worktree"` writes into a worktree below the worker's worktree (section 3.6).

## 3. Enforcement points

The points run in this order for one change. Each later point is stronger and harder to bypass.

| # | Point | Sees | Can the worker bypass it? | Role |
|---|---|---|---|---|
| E1 | `PreToolUse` hook on `Edit\|Write\|MultiEdit\|NotebookEdit` | edit tool targets | No, not with its tools (see 4.4 for the limits) | early stop |
| E2 | git `pre-commit` guard, `--enforce` | staged paths | Yes: `git commit --no-verify`, or a change of `core.hooksPath` | early stop for shell writes |
| E3 | launcher pre-push check | net diff of the push | No: the launcher runs it as its own step, not as a git hook | authoritative for the branch |
| E4 | server check at report | net diff that the launcher reports | No | records the verdict on the attempt |
| E5 | merge check (`wavecoord gate-check`) | net diff of the merge | No | authoritative for the default branch |

### 3.1 E1: `PreToolUse`

- Input: `tool_input.file_path` (Edit, Write, MultiEdit) or `tool_input.notebook_path` (NotebookEdit). Claude Code gives `file_path` as an absolute path for Write and Edit.
- Call: `POST /api/check` with `source: "hook"`, `agent`, `paths: [p]`, and for a worker `task` and `epoch` from the launcher env.
- A worker that has no epoch in its env (for example after `--no-claim`) sends a heartbeat first and uses the lease from the reply. If it still has no lease, the hook denies with `no_lease` and does not call `/api/check`.
- Allow: no output, exit 0.
- Deny: exit 0 with this output, and nothing else on stdout:
  ```json
  {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
    "permissionDecisionReason": "wavecoord fence: <kind> <path> ... Do not write this path in another way (Bash). Send a message to the lead if you need it."}}
  ```
  Claude reads the reason. When several hooks answer, Claude Code applies `deny` > `defer` > `ask` > `allow`, so another plugin's `allow` cannot undo the deny. `PreToolUse` hooks run before the permission prompt, and a blocking hook takes precedence over allow rules, so the deny is expected to hold in `bypassPermissions` mode as well (a test must check this, section 7). One known exception: a user-installed mod that handles `tool.check` can approve a call that a non-managed `PreToolUse` hook blocked. Workers must run with no such mod, and the launcher must check this.
- Backstop: if the JSON cannot be written, the hook writes the reason to stderr and exits 2. Claude Code handles that as a deny.
- `hooks.json` for this hook sets `"onFailure": "block"`. Then a hook that cannot start (a missing zipapp after a plugin update), crashes, exits with a code other than 0 or 2, prints JSON that does not parse, or reaches its `timeout`, blocks the call. This field needs Claude Code v2.1.295 or later. `claude plugin validate --strict` accepts it in a plugin's `hooks.json` (checked with v2.1.302). In enforce mode the launcher must refuse a host whose `claude --version` is lower, because there an older Claude Code would let a dead hook through.

### 3.2 E2: pre-commit

`wavecoord guard check --staged --enforce`. It reads the staged paths with `git diff --cached --name-only --no-renames -z`. Without `--no-renames`, the rename of a foreign file shows only the new name, and the delete of the old path is hidden. The same fix applies to the push and merge diffs. The guard reads its identity (agent, task, epoch) from `<git dir>/wavecoord-hooks/identity.json`, which `guard install` writes. The environment of a `git commit` that a worker starts with Bash is not a reliable source. It exits 1 on a violation or on any failure (section 4).

### 3.3 E3: launcher pre-push check

Before `git push`, the launcher itself runs the check on `git diff --name-only --no-renames merge-base(origin/<default>, HEAD) HEAD`, with the attempt's agent, task and epoch, after its own rebase. A violation or a failure means no push. The attempt ends `failed` with the note `fence: <kind or unavailable>`. The commits stay in the worktree. `gc` never removes unpushed work, and `--continue` can push later. Today the launcher's `git push` runs with the wrapper's env, which has no `WAVECOORD_AGENT_ID`, so the advisory pre-push guard checks with no agent. This direct check replaces that path. The pre-push git hook stays in the chain as a second check.

### 3.4 E4: report

The report request gets a new field, `changed_paths` (the same net diff as E3). The server evaluates `ok` for each path and stores `fence: {verdict, violations, seq}` on the attempt. A failed verdict changes nothing else, but `refinery_status` shows it and the refinery does not merge the attempt. E4 is a record. The merge check does not trust it.

### 3.5 E5: merge check

`wavecoord gate-check --task T --attempt N [--range BASE..HEAD] [--pr-body FILE]`, for the refinery's `merge_cmd` and for external gates.

- It reads the attempt's lease from the server and compares it with the PR body's `Lease:` line. A mismatch fails, because a PR body is easy to edit.
- It computes the net diff itself with git, and calls the check with `source: "gate"`.
- Exit codes: 0 pass; 1 violation; 3 coordinator unavailable or any error. A gate must treat every exit other than 0 as "do not merge".
- No override has any effect here.
- Role: the `gate` token may read only leases and health today. P6 lets `gate` call `POST /api/check` with `source: "gate"` and read the reservations of one lease (open decision D3).

### 3.6 Path normalization and paths outside the repo

The order is:

1. Make the path absolute from the hook's `cwd`. Resolve symlinks and `..` with `realpath`.
2. Find the worktree that contains the result: the innermost directory that git reports as a worktree top level. A plain `rev-parse` from `cwd` is not enough: a subagent worktree in `.claude/worktrees/<name>` is inside the parent worktree, and a path from the parent's root would be wrong.
3. Classify the path:

| The target is in | Worker action | Kind |
|---|---|---|
| the agent's own worktree (`WAVECOORD_WORKTREE`), or a worktree nested below it | check the repo-relative path | as section 2 |
| another worktree, or the main checkout, of the same repo (same `git-common-dir`) | deny | `foreign_worktree` |
| `.git/` of any worktree, or the git common dir | deny | `protected_local` |
| `$WAVECOORD_HOME`, the pinned plugin directory, `~/.claude/` | deny | `protected_local` |
| a path that git ignores in the own worktree | allow, no call | none |
| anything else outside the repo (for example `/tmp`) | allow, no call | none |

4. **Case.** On a case-insensitive file system (`core.ignorecase=true`, the macOS default), `realpath` does not fix the case of a name. So the hook must rebuild every existing path component with its case on disk (from a directory listing) and keep the case of a new leaf as given. Then `foreign_exclusive` compares with case folding (it denies more), and `outside_lease` compares the case-correct path exactly (it allows less).
5. A path with a space, a control character, or a name that is not valid UTF-8 cannot be in any glob (`valid_glob`). For a worker this is `outside_lease`, unless it is git-ignored or outside the repo.

The server does not trust client normalization. It refuses a path that is absolute, has a `..` or `.` segment, or has a backslash, with 400. The hook then denies (section 4).

## 4. Failure modes

### 4.1 Rules

- **F1.** A worker write that the fence cannot prove `ok` is refused. "Cannot prove" includes every failure below.
- **F2.** The fence never uses the spool for a decision. A check is a read, and it is never queued or replayed.
- **F3.** The fence never uses the local clock for a decision, except for the expiry of the offline override (section 4.4). Ages and epochs come from the server.
- **F4.** An allow is never cached (section 6).
- **F5.** The reason that the agent sees names the cause, and tells it to wait, retry or ask the lead. It must not suggest a way around the fence.

### 4.2 What each point does

| Condition | E1 PreToolUse (worker) | E1 (lead, other) | E2 pre-commit | E3 pre-push | E5 gate-check |
|---|---|---|---|---|---|
| Coordinator not configured (no URL) | allow, no output (rule 1). In enforce mode the launcher always sets the URL, so for a worker "no URL" means a broken env: deny `unavailable` when `WAVECOORD_TASK` is set | allow | exit 0, or exit 1 when the identity file exists | refuse | exit 3 |
| Connection refused, DNS error, TLS or tunnel down | one retry after 200 ms if the budget allows, then deny `unavailable` | allow, with `systemMessage` | exit 1 | refuse | exit 3 |
| Timeout (`CLIENT_TIMEOUT_S`, or the budget ends) | deny `unavailable` | allow, with `systemMessage` | exit 1 | refuse | exit 3 |
| HTTP 5xx, 503 `unavailable` | deny `unavailable` | allow, with `systemMessage` | exit 1 | refuse | exit 3 |
| 503 `clock` | not possible: the check is a read and does not wait for the clock | | | | |
| 401, 403, 421 | deny `auth` | allow, with `systemMessage` | exit 1 | refuse | exit 3 |
| 400 (a bad path, a bug) | deny `bad_request` | allow | exit 1 | refuse | exit 3 |
| 200 with a body that does not parse or does not match `check_response` | deny `malformed` | allow | exit 1 | refuse | exit 3 |
| 200 with violations | deny, with the kinds | `ask` or as `fence.lead` | exit 1 | refuse | exit 1 |
| The hook itself crashes, or Claude Code kills it | `onFailure: block` blocks | same | git: exit != 0 stops the commit | the launcher treats an exception as a refusal | exit 3 |
| The wavecoord build is missing | `onFailure: block` blocks | same | refuse in enforce mode (today the script exits 0) | the launcher has its own build | exit 3 |

The lead and other sessions fail open on purpose. They are human-attended, the human can see the `systemMessage`, and E5 still covers the default branch.

### 4.3 Server restart, rollback, clock

- **Restart.** The server is down, so E1 to E5 refuse. After a restart every `active` lease gets a fresh TTL (contract 3.1), so no lease turns stale because of the outage.
- **`rebuild --at <sha>`.** Leases granted after `to_seq` are released with reason `rollback`. Their holders then fail `no_lease` at every point.
- **Clock back.** Grants fail with 503 `clock`. Checks still answer, so the fence goes on. **Sleep gap.** The server shifts the expiry times, so no lease turns stale.
- **Expiry while a worker runs.** A `stale` lease is still the holder's lease (section 5.4).

### 4.4 The offline override (human only)

**What it does.** When the coordinator is unavailable, it changes the E1 and E2 result for a worker from "deny `unavailable`" to "allow, and log". It never changes a deny that the server answered (a real violation). It has no effect on E3, E4 and E5. So no override can put an unchecked write on the default branch: the merge check checks every path when the coordinator is back.

**Where it lives.** In a file, not in an environment variable: `$WAVECOORD_HOME/fence-override.json`, mode 600, outside every worktree. The human creates it with `wavecoord fence offline --for 1h --reason "<text>"` in a terminal. The content is `{until, reason, created, host, nonce}`. The maximum TTL is 4 h, and an expired file has no effect. `wavecoord fence online` deletes it.

**Why not an env var such as `WAVECOORD_FENCE_OFFLINE=1`.**

- The hook process inherits its env from the Claude Code process, and that process inherits it from the shell or the launcher that started it. A Bash command of the agent runs in a child process. It can set variables for its own children, but not for its parent or for the hook processes that Claude Code starts. So far, an env var looks safe.
- But Claude Code also takes env values from the `env` key of settings files, and the file watcher picks up settings changes during a session. `.claude/settings.local.json` in the worktree is writable by the agent. So an env var that the hook honors can be set by an agent that writes one file. The same route can set `disableAllHooks: true`. The launcher's `--settings` file has a higher precedence than local and project settings, so the worker settings must pin `"disableAllHooks": false` (a real Claude Code check, `tests/fence/README.md` section 4).
- An env var also leaks into every child process, the worker's `claude` included, so it would turn off the fence for every session that inherits it.

So the hook reads the override only from the file. It ignores `WAVECOORD_FENCE_OFFLINE`, and if the variable is set it says so in `systemMessage`, so that a human who sets it learns that it has no effect.

**The honest limit.** A worker runs as the same OS user, so a worker with Bash **can** write `$WAVECOORD_HOME/fence-override.json`. P6 makes that unlikely, not impossible:

- E1 denies an edit tool write to `$WAVECOORD_HOME` (`protected_local`).
- `wavecoord fence offline` refuses to run when `CLAUDECODE` is set in its env. Claude Code sets this variable for the commands of its Bash tool. An agent can unset it, so this check only stops an accident.
- The worker prompt (`--append-system-prompt`) states that the override is for the human only.
- A write under the override cannot reach the default branch without the E5 check.

Only an OS boundary stops a determined agent: Claude Code sandboxing with a write allowlist (the worktree only) or a separate OS user per worker. Sandboxing would also stop the shell writes outside the worktree. It is a follow-up (open decision D5).

**Logging.** Each write that the override lets through is appended to `$WAVECOORD_HOME/state/fence-override.jsonl` (time, agent, task, epoch, path, point). The next hook run that reaches the coordinator sends `POST /api/notify` with kind `fence_override`, the file's `created`, `until` and `reason`, and the journal entries (up to 200; with more, a count and the first 200). The idempotency key is `fence-override:<nonce>:<offset>`. The server logs it as a `notify.post` event, and the lead sees it in the inbox and on the channel. A worker whose journal is not empty cannot pass E3 until the journal has been sent. The journal is append-only, and an entry leaves it only when the server acknowledges it.

## 5. Races

The server evaluates every check under its write lock (`Coord.wlock`), the same lock that serializes grants, steals, releases and reservations. So each check sees one consistent state, and every check has one place in the order of all events. The decision time `t` of section 2 is that place.

### 5.1 Check, then write

E1 allows at `t0` and the tool writes at `t1 > t0`. Between them a steal or a new reservation can make the write wrong. No file system write can be atomic with a server check, so E1 cannot close this window. That is why the fence invariant is stated at the decision time, and why E1 is not the authority. The epoch closes the window later: the write stays in the holder's own worktree, and every later point (E2, E3, E5) checks again against the newer state. The stale holder then fails `stale_epoch` or `no_lease`. The window is open only for the time of one tool call (milliseconds to seconds), and the result stays local.

### 5.2 Steal between check and write

Same as 5.1. Also, the next heartbeat (hook or launcher, at most every `HB_INTERVAL_S`) lists the lease in `lost`, and the launcher stops the worker. Between the steal and the stop, every E1 call fails `stale_epoch`, because the worker's claimed epoch is the old one and never changes during the session.

### 5.3 Two grants at the same time

The server serializes them. With the enforce rule of section 2.3, the second of two overlapping exclusive grants fails with 409. A check that runs between them sees either none or one of them, never half a grant.

### 5.4 Heartbeat expiry and long edits

A lease that has had no activity for `LEASE_TTL_S` turns `stale`. A stale lease is not free (nobody gets the task until the lead acts), so it creates no foreign conflict, and its holder still passes `no_lease` and `stale_epoch`. Today `outside_lease` is checked only for an `active` lease, so a stale holder can write anywhere. P6 must check it for every non-released lease. The next heartbeat revives the lease with the same epoch. A long edit is one tool call, much shorter than the 600 s TTL. A stale lease that the lead steals or releases is section 5.2.

### 5.5 Merge check, then merge

E5 passes, then the lease changes, then the merge happens. The refinery runs E5 and the merge for one attempt with the lease held in `parked` state. A parked lease does not expire, and only the lead can steal or release it. The lead also runs the refinery, so the lead's own actions are in order. An external gate must run `gate-check` as its last step before the merge. The window between the check and the merge stays open for an external gate. It is documented and not closed in P6.

## 6. Performance budget

| Item | Target |
|---|---|
| E1 hook wall time, coordinator on loopback | p50 at most 100 ms, p99 at most 300 ms |
| E1, coordinator through an SSH tunnel on a LAN | p50 at most 150 ms, p99 at most 500 ms |
| E1 hard budget, then deny `unavailable` | 2.5 s (`CLIENT_TIMEOUT_S` for this call: 2 s) |
| `hooks.json` `timeout` for `PreToolUse` | 6 s; `onFailure: "block"` covers a hang past the budget |
| Server `/api/check`, one path, 200 live reservations | p99 at most 5 ms under the lock |
| E2 and E3 | at most 5 s for up to 1000 paths (one request) |

Most of the E1 time is Python start-up and one or two `git` calls. The hook must make at most two `git` calls on the allow path (worktree top level; and `check-ignore` only for a path that matched no glob).

**Caching.** An allow is never cached. A steal or a release can come at any moment, and a cached allow would accept a write that the server already refuses. The check costs one loopback request, so the saving would be small. The hook may cache only facts that cannot change during the session: the agent identity, the claimed task and epoch from the env, the worktree root, and the case sensitivity of the file system. A deny is not cached either, so a retry after the lead fixes the lease works at once.

## 7. Test plan (summary)

The full plan is in `tests/fence/README.md`. In short:

- **Randomized invariant test.** N simulated agents with random globs, edits, commits, pushes and merges against a real in-process server. Random coordinator kills, delays, 5xx answers, malformed bodies, clock steps, steals, releases and rollbacks. An independent oracle evaluates `ok(W)` against the server state at each decision. The test asserts zero accepted writes with `not ok(W)` (outside a logged override), and full liveness while the coordinator is healthy.
- **Seeded defects.** Each defect is a patch that the test applies to the code under test. The randomized test, or a named table test, must fail for every defect. The list has the seven defects of the milestone brief and fifteen more that this design found (22 in all).
- **Real Claude Code checks.** The `onFailure`, deny precedence and settings precedence facts are checked with a real `claude` and no model call, or listed as a manual release check where that is not possible.

## 8. Rollout

**Config.** `fence` in `.claude/wavecoord.json` on the default branch:

```json
{"fence": {"edit": "advisory", "commit": "advisory", "push": "advisory", "merge": "advisory", "lead": "ask"}}
```

Each point is `off`, `advisory` (check, warn, count "would deny"; today's P2 behavior) or `enforce`. The launcher reads the config from the default branch and passes it to workers as `WAVECOORD_FENCE`. The worktree's copy of `.claude/wavecoord.json` is writable by the agent, so it can only make the fence stricter. The hook uses the stricter of the two values for each point.

**Stages.** Each stage needs a lead decision and one config PR. Going back is one config PR, or a move of the plugin pin (`docs/rollback.md`).

1. All points `advisory`. Collect the metrics for at least 3 waves (or 200 attempts).
2. `merge` and `push` to `enforce`. These are server-side and cheap, and they protect the default branch. Gate: no unexplained "would deny" in stage 1, and gate-check unavailability below 0.1 %.
3. `edit` to `enforce`. Gate: E1 p99 within budget on every host, and "would deny" from E1 explained (each one a real T1, T2 or T3, or a lease that was too small).
4. `commit` to `enforce`. It adds little after stage 3, because E3 catches the same paths later.

**Metrics.** The server counts every check, so it holds most of them, in memory, and returns them in `GET /api/live` as `fence`:

- checks by point and verdict (`allow`, each violation kind, `would_deny` in advisory mode);
- denies that the lead later resolved by a grant to the same lease (a measure of leases that are too small);
- override sessions and override writes (from `fence_override` notices);
- E5 results by exit code.

Client-side counts that the server cannot see (`unavailable`, `auth` and `malformed` denies, and E1 latency p50 and p99) go in the next heartbeat as an optional `fence` object: counts since the last heartbeat, and the latency of the last 100 checks.

## 9. Changes this design needs (for the implementation PR)

- `globs.covers` (from `hooks.glob_match`). The server uses `covers` for membership, and `covers` or `overlap` for foreign checks.
- `check_response.kind` gets `no_lease`. Client-only kinds (`foreign_worktree`, `protected_local`, `unavailable`, `auth`, `malformed`, `bad_request`) appear only in deny reasons and metrics, not in the schema.
- `check_request.source` gets `gate`. The `gate` role may call `/api/check` (open decision D3).
- Reservation `origin`. The agent role may add only `hot`. Exclusive overlaps are refused in enforce mode, and the lead may force them.
- `outside_lease` for every non-released lease, not only `active` ones.
- `--no-renames` in every diff. `guard install` writes `identity.json`. The launcher runs the E3 check itself. A guard with no wavecoord build refuses in enforce mode.
- `report_request.changed_paths`, and the `fence` verdict on the attempt.
- `wavecoord fence offline|online|status`, `wavecoord gate-check`.
- `hooks.json`: `onFailure: "block"` on `PreToolUse`. The launcher checks `claude --version` >= 2.1.295 in enforce mode, and in enforce mode a failed claim fails the launch.
- Worker settings pin `disableAllHooks: false`.
- `docs/hooks.md`, `docs/contract.md`, the schemas and the fixtures change in the same PR.

## 10. Open decisions

- **D1.** The default `fence.lead` action: `ask` (proposed), `warn` or `deny`.
- **D2.** Git-ignored paths in the own worktree: allow with no call (proposed), or check them like tracked paths.
- **D3.** The gate role: let `gate` call `/api/check` with `source: "gate"` (proposed), or add a narrower `GET /api/gate/<task>/<n>` that returns the verdict for a given diff.
- **D4.** Agent-role reservations: only `hot` marks (proposed), or let a worker add `claim` reservations that the lead approves later.
- **D5.** Sandboxing workers (a write allowlist of the worktree) as the real defence against shell writes and override tampering: a P6 option, or a later milestone.
- **D6.** The external gate's window between check and merge (section 5.5): accept it and document it (proposed), or add a merge token that the server issues at check time and checks at merge.
- **D7.** The maximum override TTL (4 h proposed), and whether `fence offline` also needs the lead token when the coordinator is up (it is useless then, so the proposal is to refuse it when the coordinator answers).

## Prior art

MCP Agent Mail (`Dicklesworthstone/mcp_agent_mail`) has advisory file reservations (exclusive or shared, with a TTL; conflicts are reported, and the reservation is still granted) and an optional pre-commit and pre-push guard. The guard matches with git wildmatch pathspecs against repo-relative paths, and honors `core.ignorecase`. It has a bypass (`AGENT_MAIL_BYPASS=1`, or `--no-verify`) and a warn mode. wavecoord takes from it the case handling, the pre-push range check and the advisory stage. It adds fencing epochs, a fail-closed edit hook and a merge-time check that no bypass skips. A bypass in an env var that an agent can set is the main thing it does not take.
