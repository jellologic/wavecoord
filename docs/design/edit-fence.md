# Design: the fail-closed edit fence (P6)

Status: implemented in P6. The lead decisions D1 to D7 (PR #5) took the proposed defaults; section 11 lists what the implementation does and where it differs. The test plan is in `tests/fence/README.md`.

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
- **Who may widen a lease.** With an `agent` token, `POST /api/reservations` may add only `origin: hot` shared marks, each one path with no `*` or `?` (0.2.1; a `**` mark would block the readiness of every task, D38). A new `claim` or `lead` grant needs the lead token. Otherwise a worker could reserve `**` exclusive and pass every check.
- **Who may claim.** An `agent` token acts only within what the lead dispatched (0.2.1, `fencecore.agent_claim_error`, D39). With `POST /api/leases` it may renew the lease that the lead granted to its holder (same task and attempt), or, with `resume`, take over a parked lease of the same attempt whose holder is on its own host, with that lease's grant set (no `reservations`, or the same ones). A lease for a task or attempt that has none, a takeover of another holder's live lease, a takeover across hosts, a failed-park takeover by a newer attempt and any other grants need the lead token (403). Otherwise a worker could claim `**` for a task the lead never dispatched, or resume its parked lease with `**` and pass E5 through the resolver lineage.

### 2.4 Lead and other sessions

The lead and other sessions hold no lease, so `no_lease`, `stale_epoch` and `outside_lease` do not apply to them. For their writes the fence checks only `foreign_exclusive`. Their default action is `ask`: the `PreToolUse` hook returns `permissionDecision: "ask"` with the reason, so the human decides. In `-p` mode Claude Code denies a call that nobody can answer. The config key `fence.lead` (`warn`, `ask` or `deny`; the default is `ask`) sets this. These writes are outside the fence invariant, but the merge check still covers what they commit if it goes through the gate.

A **subagent** of a worker fires the same hooks with the same environment, so it acts with the worker's lease. A subagent with `isolation: "worktree"` writes into a worktree below the worker's worktree (section 3.6).

## 3. Enforcement points

The points run in this order for one change. Each later point is stronger and harder to bypass.

| # | Point | Sees | Can the worker bypass it? | Role |
|---|---|---|---|---|
| E1 | `PreToolUse` hook on `Edit\|Write\|MultiEdit\|NotebookEdit` | edit tool targets | No, not with its tools (see 4.4 for the limits) | early stop |
| E2 | git `pre-commit` and `pre-merge-commit` guard, `--enforce` | staged paths | Yes: `git commit --no-verify`, a change of `core.hooksPath`, and every git command that runs no commit hook (`cherry-pick`, `rebase`, `am`, a fast-forward merge) | early stop for shell writes |
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

`wavecoord guard check --staged --enforce`. It reads the staged paths with `git diff --cached --name-only --no-renames --ignore-submodules=none -z`. Without `--no-renames`, the rename of a foreign file shows only the new name, and the delete of the old path is hidden. Without `--ignore-submodules=none` (and `-c diff.ignoreSubmodules=none`), a `submodule.<name>.ignore = all` in `.gitmodules` or a `diff.ignoreSubmodules` in the git config hides a submodule (gitlink) change; with it, the gitlink path (`vendor/sub`) is a changed path, and a lease must cover that path (`vendor/sub`, or `vendor/**`; `vendor/sub/**` does not cover `vendor/sub` itself). The same flags apply to the push and merge diffs, the Stop hook and the refinery's protected-path check.

The guard runs as `pre-commit`, and as `pre-merge-commit` for a merge that makes a commit. Git runs no commit hook for `cherry-pick`, `rebase`, `am` or a fast-forward merge, so E2 does not see the commits these make. E3 and E5 do: they check the net diff of the branch, whatever made its commits. The pre-push guard reads the pushed range; when the remote head is not in the local repo (a forced push over a branch that moved), it reads the commits that no remote-tracking ref has (`git log -m --first-parent`, so a merge commit lists what it brings in). When git cannot compute the paths, the pre-push guard refuses in enforce mode. The guard reads its identity (agent, task, epoch) from `<git dir>/wavecoord-hooks/identity.json`, which `guard install` writes. The environment of a `git commit` that a worker starts with Bash is not a reliable source. It exits 1 on a violation or on any failure (section 4).

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
| the agent's own worktree (`WAVECOORD_WORKTREE`), or a worktree of the same repo nested below it | check the repo-relative path | as section 2 |
| a nested repo of another repo below the own worktree (a submodule, a vendored clone) | check the path from the own worktree (`vendor/sub/src/x.c`), never from the nested repo's root | as section 2 |
| another worktree, or the main checkout, of the same repo (same `git-common-dir`) | deny | `foreign_worktree` |
| `.git/` of any worktree, or the git common dir | deny | `protected_local` |
| `$WAVECOORD_HOME`, the pinned plugin directory, `~/.claude/` | deny | `protected_local` |
| a path that git ignores in the own worktree | allow, no call | none |
| anything else outside the repo (for example `/tmp`) | allow, no call | none |

4. **Case.** On a case-insensitive file system (`core.ignorecase=true`, the macOS default), `realpath` does not fix the case of a name. The protected folders of the table compare without case where the disk folds case (`~/.CLAUDE/settings.json` is `~/.claude/settings.json`), and a `.git` segment compares without case everywhere. So the hook must rebuild every existing path component with its case on disk (from a directory listing) and keep the case of a new leaf as given. Then `foreign_exclusive` compares with case folding (it denies more), and `outside_lease` compares the case-correct path exactly (it allows less).
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

**Logging.** Each write that the override lets through is appended to `$WAVECOORD_HOME/state/fence-override.jsonl` (time, agent, task, epoch, path, point). The next hook run that reaches the coordinator, and has budget left after its decision, sends `POST /api/notify` with kind `fence_override`, the file's `created`, `until` and `reason`, and the journal entries (the oldest ones that fit: at most 200, and a body of at most half of `REQ_MAX_BYTES`, so a journal of long reasons never gets a 413; with more, a count and that first batch, then the next batch until the journal is empty or a send makes no progress; D36). The idempotency key is `fence-override:<nonce>:<offset>`. The server logs it as a `notify.post` event, and the lead sees it in the inbox and on the channel. A worker whose journal is not empty cannot pass E3 until the journal has been sent. The journal is append-only, and an entry leaves it only when the server acknowledges it.

The flush uses what is left of the hook run's budget (section 6), after the decision. It has no budget of its own, because a longer E1 run delays the edit tool call. When the check uses the budget, or a request of the flush gets no answer, the journal stays complete, and a later reachable call sends it. This is safe: E3 refuses while the journal is not empty, and E5 checks every path. The flush runs before the spool replay, because only the journal stops a push (0.1.4).

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
- **Seeded defects.** Each defect is a patch that the test applies to the code under test. The randomized test, or a named table test, must fail for every defect. The list has the seven defects of the milestone brief and fifteen more that this design found (22 in all). D23 came from a soak run in 0.1.3 (section 11). D24 (a task PR that changes the lead config) came with the rule of section 8 in 0.1.4. D25 came with section 8.1 in P8, D26 to D30 with the review fixes of 0.2.1 (sections 8 and 8.1), and D31 to D37 with the second review (section 8, submodules in 3.2, nested repos and case in 3.6, the journal batches in 4.4, the pre-push guard in 3.2), and D38 and D39 with the agent authority rules of 0.2.1 (section 2.3).
- **Real Claude Code checks.** The `onFailure`, deny precedence and settings precedence facts are checked with a real `claude` and no model call, or listed as a manual release check where that is not possible.

## 8. Rollout

**Config.** `fence` in `.claude/wavecoord.json` on the default branch:

```json
{"fence": {"edit": "advisory", "commit": "advisory", "push": "advisory", "merge": "advisory", "lead": "ask"}}
```

Each point is `off`, `advisory` (check, warn, count "would deny"; today's P2 behavior) or `enforce`. The launcher reads the config from the default branch and passes it to workers as `WAVECOORD_FENCE`. The worktree's copy of `.claude/wavecoord.json` is writable by the agent, so it can only make the fence stricter. The hook uses the stricter of the two values for each point.

**A config change is a lead decision (0.1.4).** The fence modes and the other lead config change only in a plan or config PR that the lead makes. A task PR must not change them. A task PR is a PR from a worker attempt: its body has the `Task:`, `Attempt:` and `Lease:` lines. Plan PRs (`plan/<n>` branches from `plan_edit`) and lead PRs do not have these lines, so this rule does not apply to them. The branch name is not used, because it does not identify the author. The lead config is:

- in `.claude/wavecoord.json`: `fence`, `merge_cmd`, `check_cmd`, `protected_paths_cmd`, `providers`, `auto_steer` (0.1.6), `spec` and `plan` (P8), and since 0.2.1 every other key that the lead or a host runs or that decides where things run, connect or merge: `pr_cmd`, `pr_state_cmd`, `coord_url`, `project`, `log_branch`, `default_provider`, `auto_resolve` and `branch_prefix`. Of each entry in `hosts` (by `name`): `claude_flags`, `env`, `ssh`, `repo_path`, `prepare_cmd`, `providers`, `labels` and `dispatch`. Only `$schema`, `wip_cap` and the hosts' `name` and `slots` are not lead config (`gatecheck.WORKER_KEYS`; a test checks that every schema key is in one list);
- in `.claude/settings.json` and `.claude/settings.local.json`: every key but `$schema` (since 0.2.1; before, only `hooks`, `disableAllHooks`, `enabledPlugins`, `extraKnownMarketplaces` and `env`). Settings run commands (`hooks`, `statusLine`, `apiKeyHelper`, `awsAuthRefresh`, `otelHeadersHelper`), widen trust (`permissions`, `enableAllProjectMcpServers`, `enabledMcpjsonServers`) or steer the session (`model`, `outputStyle`), and each Claude Code release can add one, so the rule denies by default instead of keeping a list of the dangerous keys;
- every other file that Claude Code or a plugin that the lead loads reads (since 0.2.1): any path with a `.claude` or `.claude-plugin` folder in it, at any depth (`.claude/agents/`, `.claude/commands/`, `.claude/skills/`, a nested `pkg/.claude/settings.json` that a session started in `pkg` loads), and every `.mcp.json` and `CLAUDE.local.md`;
- each file `hooks/hooks.json` (the plugin hooks), at any depth.

Paths compare without case, and only the exact root names `.claude/wavecoord.json` and `.claude/settings*.json` get the key compare: a case variant (`.claude/Settings.json`, which a case-insensitive checkout opens as `.claude/settings.json`) or a nested copy is a change by itself (D31). Not covered: `CLAUDE.md` files (project docs that tasks edit) and the scripts that a hook command names; protect them with `protected_paths_cmd` where they matter.

Predicate: for a task PR with the net diff `D = merge-base(base, head)..head`, the PR passes E5 only if, for each root file above in `D`, each listed key has the same JSON value at the merge base and at the head (an absent file is `{}`; a new file that does not parse changes every key), and `D` has no other file of the list. `gate-check` computes this with git (`gatecheck.config_changes`), and each change is a violation of kind `config_change`. The check does not depend on the `Lease:` line: `gate-check` runs it before it reads the lease, so a body with no `Lease:` line, or with two (a worker's summary can add one), still reports the change (D26). The refinery refuses a missing or doubled `Lease:` line like a stale one (a PR body that `gh pr view` cannot read is an E5 problem instead: `enforce` blocks, `advisory` notes). The `merge` mode of the config on the base side (the default branch) decides what happens; the refinery reads it there, never from the lead's working tree, and `WAVECOORD_FENCE_FORCE` in the lead's environment can only make it stricter:

| `merge` mode | `gate-check` | refinery |
|---|---|---|
| `enforce` | exit 1 | blocks, reason `config_change` |
| `advisory` | exit 1 | blocks, reason `config_change` |
| `off` | reports the change on stderr and in `--json` (`config_changes`), exit not changed | merges, with a `fence` note |

The block holds in `advisory` mode too, unlike a path violation. Advisory mode is a way to measure the path fence before it denies. It is not a permission for a worker to change the fence. A worker that changes `fence` to `off` in a merged task PR turns the fence off for the next wave, and no stage of this rollout has that as a lead decision. The detail tells the lead to make the change in a plan or config PR. Seeded defects D24, D31 (case and depth) and D32 (the five-key settings list) and the table check `config_change_check` check the rule.

The lead's own config lookup (`client.find_repo_config`) reads `.claude/wavecoord.json` only at the repo's top level, so a nested copy in a subfolder never becomes the config of a session started there.

### 8.1 The spec tree is a lead decision too (P8)

The spec tree (`docs/specs.md`, `design/spec-layer.md`) holds the requirements, the plan and the task status. Requirements and plans change in plan PRs (`spec_edit`); status belongs to the coordinator's status commits. A task PR changes none of it.

Predicate S: for a task PR with the net diff `D`, no path of `D` is under `spec.dir`, where `spec.dir` comes from the config on the base side and at the merge base (default `specs`), so a worker cannot move it and a tree that the lead moved meanwhile is still covered (D29). The value is normalized (`./specs/.` is `specs`, D28), and paths compare without case (`SPECS/x` lands in `specs/` on a case-insensitive checkout, D30). With `--no-renames`, a rename into or out of the tree is a path in it. Predicate S applies only at a revision that uses the spec tree, with the lead's rule for picking its plan (`LeadBase.plan_store`): `plan.adapter` is `specs`, or there is no `plan.adapter` and `spec.dir` holds at least one spec record there. A repo with a flat plan may have a `specs/` folder of its own (`specs/openapi.yaml`): it is not the spec tree, and a task PR may change it (0.2.1). The revisions are the base side, the merge base and the head, so a task PR that adds the first spec records (and so would switch the repo to the spec tree) is refused too. A git error or a tree that cannot be read counts as "uses the spec tree". `gate-check` reports each such path as a violation of kind `spec_change` (`gatecheck.spec_changes`), with the `merge` mode table above: `enforce` and `advisory` block (refinery reason `spec_change`), `off` reports. A lease that covers `spec.dir` does not change this: the fence leases decide what a worker may write, predicate S decides what a task PR may merge. Plan PRs, lead PRs and the status PR have no `Task:` lines and are not checked. Seeded defects D25 (`spec_changes` returns nothing), D28, D29 and D30 and the table check `spec_change_check` check the rule.

The refinery runs the fence check (E4, E5, sections 8 and 8.1) before `check_cmd` and before the spec tree's Acceptance and Verify, so a PR that is refused never runs its code on the lead. Verify runs on the merge result with a scrubbed environment (`security.md`).

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

## 11. Implementation notes (P6)

**Decisions.** D1 `fence.lead` is `ask`. D2 a git-ignored path in the own worktree passes with no call. D3 the `gate` role may send `POST /api/check` with `source: "gate"`. D4 an `agent` token may add only `shared` `hot` marks (403 otherwise). D5 no sandboxing in P6 (P7). D6 the window between an external gate's check and its merge is accepted (section 5.5). D7 the override lasts at most 4 h, and `fence offline` refuses while the coordinator answers.

**Lead decisions after P4.** (1) Resolver lineage: a resolver is a `--continue` run of the same attempt and branch, and it takes over the attempt's lease with a new epoch. E5 accepts a PR whose `Lease:` epoch is an older lease of the same attempt when the attempt's current lease is newer (`fencecore.lineage_ok`; the answer carries `lease.from_epoch`). The predicate is then evaluated with the current epoch and the resolver as the writer. When a resolver run ends clean, `collect` sets the PR's `Lease:` line to the new epoch (`gh pr view` and `gh pr edit`). (2) The launcher removes lead and server credentials from the worker env on every host, the local host included (`CLAUDE_PLUGIN_OPTION_TOKEN`, `WAVECOORD_TOKEN_FILE`, `WAVECOORD_TOKEN`, `WAVECOORD_TOKENS_FILE`). A worker gets only its host's agent token; a token that is the lead's own token, or that the tokens file on that machine lists with another role, is refused.

**Gaps found by the end-to-end test (PR #8).** (1) In a worker session the hooks client uses only `WAVECOORD_TOKEN_FILE` (the host's agent token), never `CLAUDE_PLUGIN_OPTION_TOKEN` or `~/.wavecoord/token` (`client.resolve_token`). (2) The refinery refuses a PR whose `Lease:` line is not the attempt's live lease, or an older lease of the same attempt (resolver lineage), in every fence mode (reason `stale_lease`; `gatecheck.lease_ok`). The other fence results block only in `merge` enforce mode.

**The gaps of the P2 code, and where each is closed.**

| Gap | Fix |
|---|---|
| A released (not stolen) lease passed the epoch check | `no_lease` (`fencecore.evaluate`, `writer_lease`, `epoch_now` from the non-released lease) |
| `outside_lease` only for an `active` lease | `lease_checked`: every non-released lease |
| Hot-file marks widened the lease | reservation `origin`; `grant_set` holds `claim` and `lead` only |
| An agent token could reserve anything | `agent_reservation_ok` (403), `force` is lead only |
| An agent token could claim any task, or resume with other grants (0.2.1) | `agent_claim_error` (403) |
| Diffs without `--no-renames` (and, until 0.2.1, without `--ignore-submodules=none`) | `fence.DIFF_RENAME_FLAGS` and `fence.DIFF_GIT_CONFIG` in every diff (guard, E3, E5, the Stop hook, the refinery's protected-path check) |
| The launcher's push ran with no agent id | E3 runs the check itself with the attempt's identity; `git push` runs with the worker env; the guard reads `identity.json` |
| Sibling worktrees, macOS case, `..` and symlinks | `fence.classify` (`realpath`, innermost `.git`, `common_dir` read from `.git` with no git call), `canon_case`, `fold_case` |
| `disableAllHooks` from a worktree settings file | worker `--settings` pin `disableAllHooks: false` and the fence env |
| A user mod that handles `tool.check` | `fencehost.tool_check_mods`, a best-effort scan (see below) |

**Differences from the design.**

- The foreign check uses `covers` (case-folded on a case-insensitive file system), not `overlap`. The design allows the over-match; exact `covers` keeps the liveness assertion exact.
- The server also refuses an empty path segment (`a//b`) with 400.
- `check_request.source` has `push` (E3) besides `hook`, `guard` and `gate`.
- E1's budget is 2.5 s (`BUDGET_S["PreToolUse"]`) and its request timeout at most 2 s. Tests set `WAVECOORD_FENCE_BUDGET_S`.
- A handler that runs past its budget writes no more journal entries (in a real hook the process is gone; the in-process tests need the rule).
- The override journal leaves the client only when the answer is a real `fence_override` notice. A 200 with any other body (a proxy, a fault) keeps it. The randomized test found this.
- An entry leaves the journal only when the acknowledged notice holds it (`fence.acked_entries`). A retry has the same idempotency key as the first send. When the first send was logged and its answer was lost, the server returns the first notice, which does not hold the entries that were added later. The flush then sends again with a new key, so one reachable call sends the whole journal. A soak run found this in 0.1.3 (defect D23).
- Assertion 5 of the randomized test (the journal is sent after a reachable call) accepts a journal that stays only when the hook run's client got no answer, a request of the flush got no answer, or the budget ended before the flush (`fencesim.flush_deferred`). A flush whose requests all got answers must leave the journal empty. The end of each run still requires one reachable call to send the whole journal. A run under heavy parallel load found this in 0.1.4 (seed 631187689, Python 3.9): the check used the 1.5 s test budget, and the flush had no time left.
- The refinery runs E4 and E5 before `merge_cmd`: in `merge` enforce mode a `fail` verdict or a non-zero gate blocks the PR (`reason: fence`); in advisory mode the result carries a `fence` note. It calls the coordinator in process (`gatecheck.coord_checker`).
- Metrics: the server counts checks by source and kind and returns them in `GET /api/live` as `fence` (with the modes). The client-side counts in the heartbeat (unavailable, auth and malformed denies, E1 latency) and the `would_deny` count are not in P6.
- The mod check scans `~/.claude/plugins`, `~/.claude/dev-mods`, `~/.claude/skills` and `CLAUDE_CODE_PLUGIN_DIRS` for a hooks module that names `tool.check`. It cannot see a mod that a managed policy or another path loads, and it can also flag a mod that is installed but not enabled. It runs only in `edit` enforce mode.
- Since 0.2.1 an agent token may claim only within the lead's dispatch (section 2.3, "Who may claim"). The launcher claims with the lead token. A worker that renews its own lease keeps its epoch; a resume of the same attempt gets a new epoch with the same grants, so E5's resolver lineage never widens a lease.
- Real Claude Code checks (section 7, README section 4): `tests/fake-claude/claude` runs the plugin's real `hooks.json` and zipapp for one emulated `Edit` (`FAKE_CLAUDE_EDIT`) with `onFailure`, the timeout, exit 2 and the deny precedence, and `claude plugin validate --strict` runs when `claude` is installed. The tool-call cases with a real model stay a manual release check.

