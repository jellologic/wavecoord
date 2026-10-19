# Specs

Status: planned (P8). This page is the user guide for the spec layer; the design and the rollout are in `design/spec-layer.md`. The commands arrive in phases S1 to S5.

Specs are the requirements of your product, as Markdown files in your repo. Task specs (`task-spec.md`) say what a worker does; specs say what must be true when the work is done. Each task names the requirements it implements, and wavecoord keeps the two in step.

## Layout

```
specs/                         set another folder with "spec": {"dir": "..."} in .claude/wavecoord.json
  auth/
    spec.md                    the capability: intent, non-goals, design, interfaces
    req/
      token-rotation.md        one requirement per file
      reject-revoked.md
    decisions/
      2026-10-19-rotating-tokens.md
```

The path is the ID: `auth/token-rotation`. Names are lowercase letters, digits and `-`.

## A requirement

```markdown
---
title: Token rotation on expiry
status: approved
tags:
  - security
---

## Statement
When an access token is within 60 s of expiry, the auth service shall issue a new token and revoke the old one.

## Scenarios
### Near-expiry token is refreshed
- Given a session whose token expires in 30 s
- When the client calls refresh
- Then a new token is returned
- And the old token is rejected

## Verify
- `python -m unittest tests.test_rotate`

## Rationale
Short-lived tokens limit the damage of a leaked token.
```

- **Statement**: one sentence in EARS form, with "shall" once: "When ...", "While ...", "If ... then ...", "Where ...", or "The X shall ...".
- **Scenarios**: Given, When, Then steps, one per line.
- **Verify**: commands that fail when the requirement is not met.
- **status**: `draft`, `approved` or `deprecated`. Approving is your decision, in a plan PR.

## Linking a task

```markdown
implements:
  - auth/token-rotation@1a2b3c4d
```

The part after `@` is a fingerprint of the Statement, Scenarios and Verify. When someone changes them, the link becomes **suspect**: `wavecoord spec suspect` lists the tasks to look at, and `wavecoord spec relink <task>` confirms that the task still fits. A change to the title, status, rationale or notes, or a rename, does not make links suspect.

## Rules that keep merges clean

Two branches that change different requirements never conflict. To keep it that way:

- one requirement per file, and no index or table of contents (`wavecoord spec list` is the index);
- one sentence per line in prose, and one item per line in lists;
- no counts, status summaries or "implemented by" lists in the files: the tools compute them;
- let the tools write the files, or run `wavecoord spec fmt` before you commit; `wavecoord spec fmt --check` belongs in CI;
- rename with `wavecoord spec rename`, which moves the file, keeps the old ID as an alias and updates the links.

A conflict that git still reports is a real disagreement: two branches changed the same statement or the same status.

## Commands

```
wavecoord spec list [cap] [--view suspect|coverage]      what exists, and what needs attention
wavecoord spec show auth/token-rotation [--diff]
wavecoord spec trace auth/token-rotation                 tasks, decisions, supersession
wavecoord spec new-req auth/token-rotation --title ... --statement ...
wavecoord spec set auth/token-rotation status approved
wavecoord spec link api-auth auth/token-rotation
wavecoord spec validate && wavecoord spec fmt --check    for CI
```

Every read takes `--json`; every write takes `--dry-run`. In the lead session, `/wavecoord:spec` writes specs with you and opens one plan PR, and `/wavecoord:wave-plan --spec auth` plans tasks for the requirements of `auth` that no task covers yet.

## What wavecoord does with them

- The worker's prompt holds the requirements its task implements.
- A task PR may not change `specs/`: requirements change in plan PRs.
- With `"spec": {"verify_at_merge": true}`, the merge runs the task's Acceptance and its requirements' Verify commands, taken from your default branch, and blocks on a failure.
- With `"require_approved": true`, a task waits until its requirements are approved.
