# Specs

Status: available (0.2.0). The design and its rules are in `design/spec-layer.md`.

The spec tree is your plan, from capability to task, as Markdown files in your repo. People read and review them like code; wavecoord treats them as a database: each file is one typed record, and `wavecoord spec check` proves that the tree is consistent.

## The tree

```
specs/                                   another folder: "spec": {"dir": "..."} in .claude/wavecoord.json
  auth/                                  capability
    _node.md
    sessions/                            feature
      _node.md
      token-rotation/                    requirement
        _node.md
        tasks/
          rotate-impl.md                 task
          rotate-tests.md                task
```

- The path is the ID: `auth/sessions/token-rotation/rotate-impl`.
- The folder is the parent. Names are lowercase letters, digits and `-`.
- There is no index file. `wavecoord spec tree` is the index.

## What every node says

```markdown
---
status: approved
kind: requirement
schema: 1
title: Token rotation on expiry
needs:
  - auth/sessions/session-store
inputs:
  - id: auth.session-store.api
    kind: interface
    from: auth/sessions/session-store
outputs:
  - id: auth.rotation.service
    kind: interface
    contract: rotate(refresh_token) -> TokenPair; the old refresh token is revoked
---

## Statement
When an access token is within 60 s of expiry, the auth service shall issue a new token and revoke the old one.

## Scenarios
### Near-expiry token is refreshed
- Given a session whose token expires in 30 s
- When the client calls refresh
- Then a new token pair is returned

## Verify
- `python -m unittest tests.test_rotate`
```

- **`outputs`**: what the node hands over, each with a kind (`file`, `interface`, `api`, `schema`, `data`, `config`, `doc`, `event`) and, for an interface, a one-line contract.
- **`inputs`**: what the node takes in, and `from` which node, or `external: true`.
- **`needs`**: what must be done first. A node can need a task or a whole feature; a node also waits for whatever its parents need.
- **`conflicts_with`**: what must not run at the same time for a reason the tree cannot see, such as a shared database.

A task adds what a worker needs: `paths` it may change, `exec` (model, budget, turns), Context, Risks, Acceptance and Prompt, as in today's task specs.

## What `check` proves

- Every output a requirement, feature or capability promises is produced by exactly one task under it.
- Every input comes from a node the consumer `needs`, or is external.
- No two tasks produce the same thing, and file outputs are inside the task's `paths`.
- `needs` has no cycles, and every reference resolves.
- The files are in canonical form.

## What can run at the same time

You never write it. `wavecoord spec waves` computes it: two tasks can run together when neither needs the other, their paths do not overlap, and they do not conflict. It prints the waves, the critical path and the widest wave. If two tasks could run together but write the same file, `check` tells you to add a `needs` or split the file.

## Status

| Node | Status | Set by |
|---|---|---|
| capability, feature | `draft`, `active`, `deprecated` | you, in a plan PR |
| requirement | `draft`, `approved`, `deprecated` | you, in a plan PR |
| task | `draft`, `planned`, `on_hold`, `dropped` | you, in a plan PR |
| task | `in_progress`, `in_review`, `done` | wavecoord |

wavecoord commits task status to your default branch as work moves: `in_progress` when a worker starts, `in_review` when its PR is open, `done` when it merges, and back to `planned` when an attempt ends without a merge. The coordinator reconciles: at each flush (every `spec.status_flush_s`, 60 s, and at once after a merge) it compares the files with what it knows and commits only the `status:` lines that differ, as `wavecoord: status (<n> tasks)` with one `Wavecoord-Status: <task> <from> <to>` line per change. It reads the files at one commit of the default branch and builds the status commit on that same commit, so it changes only `status:` lines. It writes only over `planned`, `in_progress` and `in_review`: it never changes `draft`, `on_hold` or `dropped`, and never undoes `done`. `in_review` needs an open PR; a finished attempt without one stays `in_progress`. `status` is always the first line of the frontmatter, so these commits do not conflict with your plan PRs. `ready`, `blocked` and progress are computed, never written.

If you hold, drop or draft a task while its attempt runs, your status stays in the file and the task is not dispatched again. The attempt is not stopped; use `wavecoord steer <task> stop`. `status-sync` lists such tasks under `held`.

If a merge is rolled back (`wavecoord rebuild --at`, or a revert of the merge commit), the file still says `done`. `wavecoord spec status-sync --reset <task>` (or `spec_status_sync` with `reset`) sets it back to `planned` when the coordinator has no merged attempt for it.

A push that lost a race is retried after a fetch. When the default branch refuses the push (branch protection), the same commit goes to the branch `wavecoord/status`, one PR "wavecoord: status" carries it, and `merge_cmd` merges it with the same placeholders and `WAVECOORD_*` variables as a task PR. wavecoord force-pushes this branch (it is its own), and rebuilds it only when the changes or the default branch move, so its checks can finish. A closed status PR is replaced by a new one. Any other push failure, or a status PR that cannot be merged, is logged as `spec.status_blocked`, once per reason, and the status stays live in the coordinator. A status flush never fails `dispatch`, `collect` or a merge. `wavecoord spec status-sync` (or the `spec_status_sync` tool) flushes at once.

Status commits and the commits of `spec_edit` and `plan_edit` do not run your git hooks (`core.hooksPath=/dev/null`, `--no-verify`), so a commit-message or pre-push hook does not refuse them. Check them in CI instead.

Each task records `basis`, a fingerprint of its requirement. When the requirement's statement, scenarios, checks or contracts change, the task becomes suspect. `wavecoord spec suspect` lists such tasks; after review, `wavecoord spec rebase-basis <task>` clears it.

## What wavecoord enforces

- A task runs only when its requirement is approved and its parents are active. Turn this off with `"require_approved": false`.
- The worker's prompt holds its requirement, its inputs and outputs with their contracts, and the Verify commands.
- A worker's PR may not change `specs/` (or your `spec.dir`, in any letter case). Workers propose spec changes in their report, and `/wavecoord:revise` turns the ones you accept into a plan PR. This rule applies only when the repo uses the spec tree: `plan.adapter` is `specs`, or there is no `plan` key and `spec.dir` has files on the default branch. With `"plan": {"adapter": "builtin"}`, a `specs/` folder of your own (an OpenAPI file, say) is just code.
- The merge re-runs the task's Acceptance, taken from your default branch, and, with the last open task of a requirement, the requirement's Verify too (a `dropped` or `on_hold` sibling is not open). They run on the merge of your default branch and the PR, so sibling work merged earlier is there, and after the fence check. They get a scrubbed environment: add names with `"verify_env": ["CARGO_HOME"]` (`security.md`). A failure blocks (`verify_failed`). A task or requirement record that is missing or does not parse blocks too (`verify_unavailable`). Turn this off with `"verify_at_merge": false` in the config on your default branch; the lead's working tree copy does not count.
- If you drop the last open task of a requirement after its siblings merged, no merge runs the requirement's Verify. Run it yourself, or in CI.
- `spec.dir` is read as a clean relative path (`./specs/` is `specs`); the schema rejects `.`, `..` and `//` in it.
- The PR body of a task ends with the trailers `Spec-Task:` and `Spec-Basis:`, so a squash merge carries them into `git log`.
- When your default branch is protected, status commits go through one `wavecoord: status` PR instead.

## Keeping merges clean

Two branches that change different nodes never conflict. To keep it that way:

- one node per file;
- one item per line, kept sorted;
- one sentence per line in prose;
- no counts, summaries or "done" lists in the files;
- let the tools write, or run `wavecoord spec fmt`;
- move nodes with `wavecoord spec mv`, which keeps the old ID as an alias.

Put `wavecoord spec check && wavecoord spec fmt --check` in CI.

## Commands

```
wavecoord spec tree [node]                       the tree with status and progress
wavecoord spec waves [node]                      what can run together
wavecoord spec query 'kind=task status=planned paths~src/auth/*'
wavecoord spec show <id> [--json]
wavecoord spec trace <id>                        parents, children, needs, producers, consumers, PRs
wavecoord spec new requirement auth/sessions/token-rotation --title ...
wavecoord spec status <task> on_hold
wavecoord spec status-sync [--reset <task>]      write status now; --reset undoes a rolled-back done
wavecoord spec mv <old id> <new id>
wavecoord spec migrate-plan --dry-run            move plan/tasks into the tree
```

Every read takes `--json`, and every write takes `--dry-run`. In the lead session, `/wavecoord:spec` builds the tree with you and opens one plan PR, and `/wavecoord:wave-plan` plans tasks under requirements.

## In the lead session

The lead's tools read the tree on the default branch: `spec_tree`, `spec_get`, `spec_query`, `spec_waves`, `spec_trace` and `spec_check`. `spec_edit` takes the same ops as `wavecoord spec apply`, writes them as one commit on a new `plan/<n>` branch (moves as `git mv`), and opens a PR; pass its `branch` to the next `spec_edit` to add to the same PR.

The coordinator names a task by its path with `.` for `/`: `auth/sessions/token-rotation/rotate-impl` is task `auth.sessions.token-rotation.rotate-impl` in leases, attempts, branches (`wave/<task>-<n>`) and the PR's `Task:` line. `check` keeps that name within 64 characters.

A worker's prompt holds its requirement's statement and scenarios, its inputs and outputs with their contracts, and the requirement's Verify commands under "Also run".
