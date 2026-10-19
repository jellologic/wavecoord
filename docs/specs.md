# Specs

Status: planned (P8). This page is the user guide; the design and the rollout are in `design/spec-layer.md`. The commands arrive in phases S1 to S5.

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

wavecoord commits task status to your default branch as work moves, batched every minute. `status` is always the first line of the frontmatter, so these commits do not conflict with your plan PRs. `ready`, `blocked` and progress are computed, never written.

Each task records `basis`, a fingerprint of its requirement. When the requirement's statement, scenarios, checks or contracts change, the task becomes suspect. `wavecoord spec suspect` lists such tasks; after review, `wavecoord spec rebase-basis <task>` clears it.

## What wavecoord enforces

- A task runs only when its requirement is approved and its parents are active. Turn this off with `"require_approved": false`.
- The worker's prompt holds its requirement, its inputs and outputs with their contracts, and the Verify commands.
- A worker's PR may not change `specs/`. Workers propose spec changes in their report, and `/wavecoord:revise` turns the ones you accept into a plan PR.
- The merge re-runs the task's Acceptance and its requirement's Verify, both taken from your default branch, and blocks on a failure. Turn this off with `"verify_at_merge": false`.
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
wavecoord spec mv <old id> <new id>
wavecoord spec migrate-plan --dry-run            move plan/tasks into the tree
```

Every read takes `--json`, and every write takes `--dry-run`. In the lead session, `/wavecoord:spec` builds the tree with you and opens one plan PR, and `/wavecoord:wave-plan` plans tasks under requirements.
