# Fence tests (P6): test plan

Design: `docs/design/edit-fence.md`. These tests come first: the implementation PR adds them, sees them fail against the P2 code, then makes them pass. All tests use the stdlib only, run under `python3 -m unittest discover -s tests`, and call no model.

## 1. Randomized invariant test (`test_fence_invariant.py`)

**Setup.** A real `Coord` in process (`tests/_coord.py`) with a fake clock, behind a fault proxy on loopback. Small constants (`WAVECOORD_LEASE_TTL_S=5`, `WAVECOORD_CLIENT_TIMEOUT_S=0.3`). One git repo with N worktrees, one per simulated worker. One lead and one "other" session in the main checkout. The path tree is small on purpose so that globs collide often: about 40 files under `a/`, `a/b/`, `ab/`, `A/`, `x[1].py`, `docs/` and `.claude/`.

**Agents.** N = 3 to 8 workers per run. Each gets a lease with 1 to 3 random globs from a grammar (`<dir>/**`, `<dir>/*.py`, `<file>`, `<dir>/?.md`, `**/<name>`), each exclusive or shared at random.

**Operations**, chosen at random with weights:

| Operation | How it runs |
|---|---|
| edit | `hooks.handle("PreToolUse", ...)` with a random target: in the lease, near it (prefix and case tricks), foreign, through `../`, through a symlink, in a sibling worktree, in `$WAVECOORD_HOME`, or outside the repo. If allowed, write the file |
| shell write | write a file directly (no hook), as Bash would |
| commit | `guard check --staged --enforce` in the worker's worktree, with random renames and deletes staged |
| push | the launcher's pre-push check on the net diff |
| report, merge | E4, then `gate-check`; on pass, merge into the default branch of the test repo |
| lead | steal, release, add a reservation, grant an overlapping exclusive (expect 409 in enforce mode), revert, `rebuild --at` |
| agent | heartbeat, try to widen its own lease with `POST /api/reservations` (expect only `hot` to pass) |
| fault | kill the server; restart it; delay answers past the budget; answer 500, 503 or 421; return a truncated or wrong-schema body; step the clock back by more than `CLOCK_BACK_MS`; jump it forward by more than `GAP_S` |
| override | the "human" creates or removes `fence-override.json`; the "agent" writes the file into the worktree or sets `WAVECOORD_FENCE_OFFLINE` (both must have no effect) |

**Oracle.** A separate implementation of `ok(W)` from section 2.2 of the design, written for the test: its own `covers` (a direct reading of the rules, not `hooks.glob_match`), with case folding where the design says so. In test mode the server records, for each check, the event `seq` at which it decided. The oracle folds the log to that `seq` and evaluates the predicate.

**Assertions**, after every operation and at the end:

1. **Safety.** Every write that a point accepted has `ok(W)` true at its decision `seq`, unless the override was valid at that time.
2. **Integration.** Every path in the net diff of every merge has `ok` true at the merge check's `seq`. No exceptions.
3. **Liveness.** When the coordinator was healthy (no fault active during the call) and `ok(W)` was true, the point accepted `W`. This catches a fence that denies everything.
4. **Fail-closed.** Every call made while a fault was active ended in a refusal for a worker (E1 to E3, E5), or an allow that the journal recorded under a valid override (E1, E2 only).
5. **Override log.** Every journal entry reaches the server as a `fence_override` notice after the next reachable call, and E3 refuses while the journal is not empty. The journal may stay after one hook run only when its client got no answer, a request of the flush got no answer, or the budget ended before the flush (`fencesim.flush_deferred`; design section 4.4). At the end of each run, a reachable call must send the whole journal.
6. **Lead and other.** Their foreign-exclusive edits get `ask` (or the configured action), and never `deny` from a server fault.

**Runs.** CI runs a fixed list of 50 seeds and 1 new random seed, with up to 300 operations each. The seed is printed on failure. `WAVECOORD_FENCE_SEED=<n>` reruns one seed, and `WAVECOORD_FENCE_RUNS=<n>` runs more (for local soak runs). A failing seed is added to the fixed list with the fix. A soak seed that fails goes into `REGRESSION_SEEDS` with its operation count.

## 2. Seeded defects (`test_fence_defects.py`)

Each defect is a small patch (a monkeypatch of one function or constant) on the code under test. For each defect, the test applies it, runs the randomized test on the fixed seeds and the named table cases, and asserts that at least one assertion fails. A defect that no test catches fails the suite. Each defect also has a direct table test, so that a failure names the cause.

| # | Defect | Caught by |
|---|---|---|
| D01 | Overlap off by one: a prefix compare without the `/`, so `a/b` covers `a/bc` | safety (`a/b` vs `a/bc`) and liveness |
| D02 | Fail-open on timeout: the hook prints nothing when the budget ends | fail-closed |
| D03 | Stale epoch ignored: the server skips the epoch compare | safety after a steal |
| D04 | Shared handled as exclusive | liveness (two shared holders) |
| D05 | Exclusive handled as shared | safety |
| D06 | Override read from an agent-writable place (the worktree's `.claude/wavecoord.json`, a settings `env` value, or `WAVECOORD_FENCE_OFFLINE`) | fail-closed with the agent's override step |
| D07 | No path normalization: `../` not resolved, or symlinks not resolved | safety on the `../` and symlink targets |
| D08 | Case-insensitive file system: `A/x.py` passes against an exclusive `a/**` | safety (a real case-insensitive FS on macOS CI; on Linux, the FS probe is patched to report case-insensitive and the open is folded) |
| D09 | Hot-file marks counted in `R(L)`, so an out-of-lease write widens the lease after an override | integration |
| D10 | A released (not stolen) lease passes, because `last_epoch` still matches (no `no_lease`) | safety after a release or a rollback |
| D11 | `globs_overlap` used for `outside_lease` | safety (`a/x*.py` vs `a/xyz/q.go`) |
| D12 | An allow cached for the session | safety after a steal |
| D13 | The fence mode read from the worktree config, so the agent can lower it | fail-closed |
| D14 | `outside_lease` checked only for an `active` lease | safety while the lease is `stale` |
| D15 | Diffs without `--no-renames`: a rename hides the delete of a foreign file | integration |
| D16 | `[` in a path read as a wildcard (`x[1].py`) | safety |
| D17 | A target in a sibling worktree treated as "outside the repo" | safety |
| D18 | The guard with no identity (no agent) passes | safety on commit |
| D19 | `gate-check` exits 0 when the coordinator is unavailable | integration under faults |
| D20 | A 200 with a body that is not valid JSON read as "no violations" | fail-closed |
| D21 | An agent token can add a `claim` exclusive reservation `**` | safety |
| D22 | Exclusive overlap granted in enforce mode | liveness (deadlock of two holders) and a table test |
| D23 | The override journal flush removes the whole batch on a deduplicated ack (the first send was logged, its answer lost, and the retry with the same key carries more entries) | override log, and the table test `journal_retry_check` (0.1.3) |
| D24 | A task PR that changes the lead config (`fence`, `merge_cmd`, `providers`, `hosts[].claude_flags`, the settings hook keys, a `hooks/hooks.json`) passes the merge check | the table test `config_change_check` (0.1.4; design section 8) |

## 3. Table tests (`test_fence_points.py`)

One test per row of the design's section 4.2 table, for each point (E1 worker, E1 lead, E2, E3, E5): the exact output, the exit code and the deny reason kind. Also:

- the exact E1 output shape (`hookSpecificOutput`, `hookEventName`, `permissionDecision: "deny"`, `permissionDecisionReason`), nothing else on stdout, and exit 0;
- exit 2 with the reason on stderr when stdout cannot be written;
- each row of the design's path table (section 3.6), including a subagent worktree in `.claude/worktrees/<name>` and a git-ignored path;
- `WAVECOORD_FENCE_OFFLINE=1` alone has no effect, and is named in `systemMessage`;
- `fence offline` refuses when `CLAUDECODE` is set, caps the TTL, and writes the file with mode 600;
- an expired override has no effect;
- the stricter of the launcher mode and the worktree mode wins;
- `hooks.json` has `"onFailure": "block"` on `PreToolUse`, and `claude plugin validate --strict` passes (CI already runs it).

## 4. Real Claude Code checks

These facts come from the Claude Code hooks reference. The fence depends on them, so they are checked. A real `claude` can run a hook without a model call where the hook fires before the model (`SessionStart`). The tool-call cases need a model, so they are a **manual release check** (`docs/design/edit-fence.md`, section 3.1), run once for each release that changes the hooks, with the Claude Code version recorded in the PR:

1. A `PreToolUse` command hook with `onFailure: "block"` that cannot start (the zipapp removed) blocks an `Edit`.
2. The same hook that sleeps past its `timeout` blocks the `Edit`.
3. A JSON `deny` from wavecoord wins over an `allow` from a second hook.
4. The deny holds in `--permission-mode bypassPermissions`.
5. `.claude/settings.local.json` with `"disableAllHooks": true` in the worktree does not turn off the hooks when the launcher's `--settings` pins `false`.
6. A settings `env` value in the worktree reaches the hook process (this confirms why the override is not an env var).
7. A subagent's `Edit` fires the plugin's `PreToolUse` hook with the worker's env.

`tests/fake-claude/claude` gets support for `onFailure` and the deny precedence, so that the automated tests cover the wavecoord side of each case.

## 5. Performance (`test_fence_perf.py`, opt-in)

`WAVECOORD_PERF=1` runs 200 E1 calls against a loopback server with 200 live reservations, and asserts the p50 and p99 of the design's section 6. It is opt-in because CI runners are noisy. The CI run only asserts that one check stays under the 2.5 s budget.

## 6. Where the tests are (P6)

| File | What |
|---|---|
| `tests/fenceworld.py` | the world: a real coordinator with a fake clock behind a fault proxy, the repo and worktrees, and the oracle (its own glob matcher `omatch`, its own log fold `OState`, and the inode-based `truth` of a target) |
| `tests/fencesim.py` | the random operations and the six assertions |
| `tests/test_fence_invariant.py` | 50 fixed seeds and one new random seed in parallel processes; on a case-sensitive file system 5 more seeds with a case-insensitive probe. `OracleCacheTest`: a cached oracle fold equals a fresh fold, also when a later event list gets the id of a freed one (seeded defect O1 of the oracle, seed 44 after seed 5 in one process) |
| `tests/fencedefects.py`, `tests/test_fence_defects.py` | the 24 defects as patches, one named table check per defect, and the simulation on the first seeds of each defect (`WAVECOORD_FENCE_DEFECT_SEEDS`, default 2) |
| `tests/test_fence_points.py` | the failure table, the path table, the output shapes, the override, the modes, the server rules, gate-check, the launcher helpers and the fake-claude hook runs |

`WAVECOORD_FENCE_SEED=<n>` reruns one seed, `WAVECOORD_FENCE_RUNS=<n>` adds n random seeds, and `WAVECOORD_FENCE_OPS=<n>` sets the maximum number of operations per seed (default 80; use 300 for a soak run).
