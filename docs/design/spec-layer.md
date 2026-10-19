# Design: the spec tree (P8)

Status: proposed (S0). No code yet. The phases in section 11 each ship as one PR.

wavecoord tracks execution well: the event log on `coord/log`, the fence, gate-check and the refinery. Its plan is flat: task specs in `plan/tasks` with `needs`, and nothing above a task. There are no requirements, no statement of what a task takes in and hands over, and no way for a script to check that the tasks add up to what was asked. This note replaces the flat plan with a **spec tree**: capabilities, features, requirements and tasks as typed Markdown records in the consumer repo, one file each, where the folder path is the tree, `needs` is the blocker graph, typed inputs and outputs are the contracts between nodes, and every rule is checked by a script.

The files are Markdown so that people can read them and git can diff and merge them. The tools treat them as a **database**: each file is one record of a versioned schema, every field and section has a type, and anything else is an error. "Must" marks a rule that the implementation has to meet and the tests have to check.

Why: clear, checkable requirements measurably help agents (ambiguous requirements cost about 7 points of pass rate; structured, test-checked requirements resolved about 13% more tasks), and parallel agents need explicit ownership and contracts more than prose. Heavy spec processes without checks bloat and go stale. So every part of this tree is either checked or computed.

## 1. Goals and non-goals

**Goals.**

- G1. One tree, in the repo, from capability to task: the path is the ID and the parent.
- G2. Every node says what it takes in and what it hands over (typed inputs and outputs), and a script proves that parents and children line up.
- G3. Blockers are explicit (`needs`); what can run at the same time is computed and proven, never declared.
- G4. The files merge well: two branches that work on different nodes never conflict, and status commits do not conflict with plan edits.
- G5. The files are a database: typed, versioned records, queried and changed by tools (CLI and MCP) with one op API.
- G6. The tree is the plan: the coordinator dispatches its tasks, writes their status into the files, and checks merges against it.

**Non-goals.**

- N1. Specs do not generate code.
- N2. No approval system beyond a reviewed plan PR.
- N3. No IDs allocated by a counter or a server.
- N4. No external tracker sync in this design (section 12, Q3).

## 2. The tree

```
<spec.dir>/                              default specs/, set in .claude/wavecoord.json
  <cap>/                                 depth 1: capability
    _node.md
    decisions/<YYYY-MM-DD>-<slug>.md     optional, at any node
    <feature>/                           depth 2: feature
      _node.md
      <req>/                             depth 3: requirement
        _node.md
        tasks/
          <task>.md                      depth 4: task (leaf)
```

- **Depth decides the kind.** A `_node.md` at depth 1 is a capability, depth 2 a feature, depth 3 a requirement; a file in `<req>/tasks/` is a task. Each file also states its `kind`, and validate checks that it matches the depth.
- **The path is the ID.** `auth`, `auth/sessions`, `auth/sessions/token-rotation`, `auth/sessions/token-rotation/rotate-impl`. A decision is `<node id>/decisions/<date>-<slug>`.
- **The parent is the enclosing folder.** It is never written as a field, so it cannot disagree with the tree.
- **Names** match `^[a-z0-9][a-z0-9-]{0,62}$` (lowercase, so no clash on case-insensitive file systems). `_node.md`, `tasks` and `decisions` are reserved.
- **Nothing else** is allowed under `spec.dir`: no index, README or summary (rule M5). `wavecoord spec tree` is the index.

## 3. Records

### 3.1 The record frame

Every file is one record:

```markdown
---
status: planned
kind: task
schema: 1
title: Rotate refresh tokens
...more keys...
---

## Section
...
```

- **Line order is fixed.** `status` is the first line of the frontmatter, then `kind` and `schema`, which never change, then the other keys in the order of the kind's schema. Because `status` changes often (section 7) and `kind` and `schema` never do, a status commit and an edit of any other key always have an unchanged line between them, and git merges them (rule M13).
- **`schema`** is the version of the record format for that kind. `wavecoord spec migrate` upgrades files when it changes.
- **The YAML subset** is the one of task specs (`docs/task-spec.md`) with two additions: block lists of scalars, and block lists of flat maps (section 3.6). Unknown keys are errors.
- **The body** has only the sections that the kind declares, in its order. Each section has a type (section 3.7). Unknown `## ` sections are errors.

### 3.2 Keys common to all nodes

| Key | Type | Notes |
|---|---|---|
| `status` | enum | per kind (section 7) |
| `kind` | enum | `capability`, `feature`, `requirement`, `task` |
| `schema` | int | record version, today 1 |
| `title` | text | one line |
| `needs` | list of node IDs | blockers (section 5) |
| `conflicts_with` | list of node IDs | clashes the tree cannot see, e.g. a shared database (section 6) |
| `inputs` | list of artifacts | what the node takes in (section 4) |
| `outputs` | list of artifacts | what the node hands over (section 4) |
| `tags` | list of names | free labels |
| `aliases` | list of old IDs | written by `mv` (rule M10) |

### 3.3 Capability and feature (`_node.md` at depth 1 and 2)

Extra keys: `owners` (list). Sections: **Intent** (prose, required), **Non-goals** (list), **Design** (prose; `###` subsections, code and tables allowed for contracts), **Open questions** (list).

### 3.4 Requirement (`_node.md` at depth 3)

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
tags:
  - security
---

## Statement
When an access token is within 60 s of expiry, the auth service shall issue a new token and revoke the old one.

## Scenarios
### Near-expiry token is refreshed
- Given a session whose token expires in 30 s
- When the client calls refresh
- Then a new token pair is returned
- And the old refresh token is rejected

## Verify
- `python -m unittest tests.test_rotate`

## Rationale
Short-lived tokens limit the damage of a leaked token.
```

Extra keys: `supersedes` (list of requirement IDs). Sections:

- **Statement** (prose, required): one sentence in EARS form with ` shall ` exactly once. The tools classify it (ubiquitous, event "When", state "While", unwanted "If ... then", optional "Where", or a combination); the class is never stored.
- **Scenarios** (scenarios): `### <name>`, then steps, each starting with Given, When, Then, And or But.
- **Verify** (commands): one backquoted shell command per item; each runs from the repo root, needs no secret or network, and fails when the requirement is not met.
- **Rationale**, **Notes** (prose).

### 3.5 Task (`<req>/tasks/<task>.md`)

```markdown
---
status: planned
kind: task
schema: 1
title: Rotate refresh tokens
basis: 3f9a12c4
needs:
  - auth/sessions/session-store/store-api
inputs:
  - id: auth.session-store.api
    kind: interface
    from: auth/sessions/session-store/store-api
outputs:
  - id: auth.rotation.service
    kind: interface
    contract: rotate(refresh_token) -> TokenPair; the old refresh token is revoked
  - id: src/auth/rotate.py
    kind: file
paths:
  - src/auth/rotate.py
  - tests/test_rotate.py
shared_paths:
  - docs/api.md
class: B
size: M
exec:
  model: opus
  effort: high
  max_turns: 80
  budget_usd: 5
  timeout_min: 60
---

## Context
Refresh tokens never rotate today. The session store owns persistence.

## Risks
- risk: The model edits the session store instead of its interface
  mitigation: session store paths are not in paths; the fence denies them
  kind: llm

## Acceptance
- `python -m unittest tests.test_rotate`

## Prompt
Implement rotation in src/auth/rotate.py through the session store interface. Write the tests first.
```

- The keys and sections of today's task spec move here unchanged (`paths`, `shared_paths`, `class`, `size`, `exec`, Context, Risks, Acceptance, Prompt, Notes), with two changes: `needs` takes any node ID (section 5), and the old `id`, `phase` and `state` keys go away (the path is the ID, the tree replaces `phase`, `status` replaces `state`).
- **`basis`** is the fingerprint (section 3.8) of the parent requirement when the task was planned or last reviewed. When the requirement's Statement, Scenarios, Verify, inputs or outputs change, the task becomes **suspect**: `wavecoord spec suspect` lists it, and `wavecoord spec rebase-basis <task>` records that the lead reviewed it and it still fits.

### 3.6 Artifacts (`inputs` and `outputs`)

An artifact is a flat map, one key per line, in a block list sorted by `id`:

| Key | Required | Meaning |
|---|---|---|
| `id` | yes | `kind: file`: a repo path (or a glob for `kind: files`). Otherwise a dotted name, `^[a-z0-9][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)+$` |
| `kind` | yes | `file`, `files`, `interface`, `api`, `schema`, `data`, `config`, `doc`, `event` |
| `contract` | outputs: yes for `interface`, `api`, `schema`, `event` | one line: the signature or the promise a consumer may rely on |
| `from` | inputs: yes, unless `external` | the node ID that produces it (any level) |
| `external` | no | `true`: produced outside the tree (an existing module, a third-party API); `contract` then says what is assumed |

The YAML subset gains block lists of flat maps for exactly this: `- key: value` starts an item, the following lines indented two more spaces are its keys, and nested lists or maps inside an item are errors.

### 3.7 Section types

| Type | Format | Parsed as |
|---|---|---|
| prose | paragraphs, one sentence per line | text |
| list | `- item` lines | list of strings |
| scenarios | `### name`, then `- Given/When/Then/And/But ...` lines | list of `{name, steps: [[keyword, text]]}` |
| commands | `- \`command\`` lines | list of strings |
| risks | the task-spec risk items | list of `{risk, mitigation, kind}` |

### 3.8 Fingerprint

```
fp = sha256(b"wavecoord-req-fp/1\n" + canonical_json({
        "statement": norm(statement),
        "scenarios": sorted([norm(name), [[keyword.lower(), norm(text)] ...]] ...),
        "verify":    sorted(norm(cmd) ...),
        "inputs":    sorted(canonical artifact maps),
        "outputs":   sorted(canonical artifact maps)}))[:8 hex]
```

`norm`: Unicode NFC, strip one pair of backquotes around a command, collapse whitespace. Canonical JSON: sorted keys, no spaces, UTF-8. In: what a task builds against. Out: title, status, tags, aliases, Rationale, Notes and the path, so a reflow, a reorder, a status change or a move does not make tasks suspect. The prefix versions the algorithm. Eight hex digits are enough to notice an accidental change; this is not a security check.

## 4. Inputs, outputs and alignment

Every level declares its public inputs and outputs. Capabilities, features and requirements state the contract; tasks state the concrete artifacts. `wavecoord spec check` proves these rules (must, each with a test):

| Rule | Check |
|---|---|
| A1 | Every output of a non-task node is produced by **exactly one** task in its subtree (same `id` and `kind`; the node's `contract` equals the task's) |
| A2 | Every output of a task is unique in the whole tree: no two tasks produce the same artifact |
| A3 | Every input of a node either has `from` naming a node whose subtree produces that artifact, or is `external: true`, or is an input of one of the node's ancestors (an input passes down the tree) |
| A4 | A node whose input has `from: X` must reach X through `needs` (its own `needs` or an ancestor's, after expansion, section 5): every consumed artifact is produced before it is used |
| A5 | Every `kind: file` output of a task lies inside its `paths`; `kind: files` lies inside its `paths` as a glob |
| A6 | A task's `paths` that no output covers get a warning: a task should say what it hands over |
| A7 | Every output of a node is either an output of its parent or consumed by an input somewhere in the tree, or is a task's `file` output; otherwise a warning ("orphan output") |

A1 and A3 make a requirement's contract and its tasks line up. A2 and A5, with the fence's leases, keep parallel tasks from writing the same thing. A4 ties the contracts to the blockers.

## 5. Blockers: `needs`

- `needs` lists node IDs at any level. A node ID that is not a task stands for **every task in its subtree** ("after the whole session-store feature").
- A node inherits its ancestors' `needs`: a task waits for what its requirement, feature and capability need.
- **Expansion:** the task graph is every task, with an edge to each task of each expanded `needs` of the task and of its ancestors.
- Validate rejects, on the expanded graph: a cycle; a `needs` on the node's own ancestor or descendant; a `needs` that does not resolve (directly or by alias).
- `needs` names real order only. Validate warns when a `needs` is not justified by an input (rule A4 the other way): either it is a real ordering that is not about an artifact, and the author keeps it, or it is noise.

**Roll-up, computed and never stored** (rule M5):

| Computed | Meaning |
|---|---|
| `ready` | a task with `status: planned`, every expanded need `done`, and no live lease on its paths (as today) |
| `blocked` | a task with `status: planned` and a need that is not `done`; `why_not` names it |
| progress | for a non-task node: done tasks / all tasks in the subtree, and the first unmet need |

## 6. Parallelism: computed and proven

Two tasks may run at the same time exactly when **all** of these hold (predicate P):

1. neither reaches the other in the expanded `needs` graph;
2. their `paths` do not overlap (`globs.globs_overlap`, the same conservative test as reservations), except where both are `shared_paths`;
3. their outputs are disjoint (A2 already makes this true in a valid tree);
4. neither names the other, or an ancestor of the other, in `conflicts_with`.

`wavecoord spec waves [node]` prints the tasks in waves: wave 1 is every ready task, wave n+1 the tasks whose needs are all in earlier waves, and inside a wave the greedy largest set that satisfies P pairwise. It also prints the critical path and the maximum width.

`wavecoord spec check` reports, as errors, two tasks that P would let run together but that both write one path (an overlap in `paths` between tasks with no `needs` between them): the author adds a `needs`, or splits the file. So a valid tree **is** a safe parallel plan: the dispatcher can take any set of ready tasks that P allows, and the fence's leases agree with it.

`conflicts_with` is the only way to say "not at the same time" that is not about an artifact (a shared database, a port, an external quota). It is symmetric when read, so it is written on one side only.

## 7. Status

### 7.1 Values

| Kind | Status | Written by |
|---|---|---|
| capability, feature | `draft`, `active`, `deprecated` | the lead, in a plan PR |
| requirement | `draft`, `approved`, `deprecated` | the lead, in a plan PR |
| task | `draft`, `planned`, `on_hold`, `dropped` | the lead, in a plan PR |
| task | `in_progress`, `in_review`, `done` | the coordinator (7.2) |

`ready`, `blocked`, `suspect`, `superseded` and every roll-up are computed (section 5), never written: they change when other files change, so writing them would make every change touch many files.

Task transitions:

```
draft -> planned -> in_progress -> in_review -> done
                    in_progress -> planned          (the attempt failed, timed out or was stopped)
                    in_review   -> planned          (the PR was closed, or the attempt abandoned)
planned <-> on_hold;  any non-done -> dropped
```

The coordinator writes only its three values and the return to `planned`. The lead writes the others. A plan PR that sets `in_progress`, `in_review` or `done` is refused by validate.

### 7.2 Status commits

The coordinator writes status into the task file on the default branch:

- **Events:** `attempt.start` sets `in_progress`; the PR opening sets `in_review`; `refinery.merge` (or a merge outside the refinery seen by `collect`) sets `done`; a failed, timed-out, stopped or abandoned attempt sets `planned`. The implementation reconciles instead of replaying events: at each flush it derives every task's status from the coordinator's state (a live attempt or lease, a parked lease, a merged attempt) and writes the differences, so a flush is idempotent and `status-sync` is the same flush. It never changes a lead status and never undoes `done`. Flushes run every `status_flush_s` in `wavecoord serve`, after dispatch and collect, and at once after a merge.
- **Batching:** the coordinator collects changes and pushes one commit every `STATUS_FLUSH_S` (default 60 s), and at once after a merge. The commit message lists each task and its change, with the trailers `Wavecoord-Status: <task> <from> <to>`.
- **Push:** fetch, rebase the status commit onto the remote default branch, push without force, retry with backoff (`PUSH_RETRY_S`). A rejected push never drops a change: the change stays queued and is retried; the event log stays the source of truth for status between flushes.
- **Identity and rights:** the commits are made by the coordinator with `spec.status_author`.
- **Protected default branch:** when the push is rejected, the coordinator puts the batched changes on the branch `wavecoord/status` instead, keeps one open PR "wavecoord: status" for it, and merges it with `merge_cmd` at each flush. The PR has no `Task:` lines, so the merge check does not apply to it; it is validated like a plan PR and may change only `status:` lines (must). The files then lag by up to one flush; the event log stays the live truth, and `spec tree` without `--offline` shows the live status. If the status PR cannot be merged either, the coordinator logs `spec.status_blocked` and keeps the status in the event log.
- **Only the `status:` line** of the task file changes in a status commit (must, tested). Rule M13 then keeps it from conflicting with a plan PR that edits the same task.
- **Recovery:** `wavecoord spec status-sync` rewrites every task's `status` from the event log (after a rollback, or a long time without push rights), as one commit.

### 7.3 What the merge check allows

A task PR (one with the `Task:`, `Attempt:` and `Lease:` lines) must not change any file under `spec.dir` (predicate S, kind `spec_change`): requirements and plans are lead decisions, and status belongs to the coordinator. Status commits are not PRs and are not checked by gate-check. Plan PRs (no such lines) change `spec.dir` freely and are validated (section 9.3).

## 8. Merge-friendliness

Git reports a conflict when both sides change the same lines, or lines that touch with no unchanged line between them. Each rule follows from that and has a test (section 10).

| Rule | Must | Why it merges |
|---|---|---|
| M1 | One node per file; the ID is the path; no allocated numbers | Branches that work on different nodes touch different files; there is no shared counter |
| M2 | One fact per line: each key, list item, artifact key, scenario step and command on its own line | A change is a one-line hunk |
| M3 | Block lists only; `[]` only for an empty list | Branches that add different items change different lines |
| M4 | Unordered lists are sorted: `needs`, `conflicts_with`, `tags`, `aliases`, `owners`, `supersedes`, `affects`, `paths`, `shared_paths`; `inputs` and `outputs` by `id`. Tools insert in sorted position. Verify and Acceptance (slowest command last), steps, scenarios and prose keep the author's order | Inserts at different positions, with an unchanged line between, merge |
| M5 | No committed derived data: no index, counts, roll-ups, `ready`/`blocked`, back links, "superseded by", "produced by" | Derived data turns independent changes into changes of one shared line |
| M6 | Prose has one sentence per line; tools split sentences; `fmt` never reflows | A sentence edit is a one-line hunk |
| M7 | No tables for data that changes (lint warns outside Design) | A row change rewrites the alignment of other rows |
| M8 | One canonical format, written by every tool and checked by `wavecoord spec fmt --check`: fixed key and section order, one blank line between sections, LF, final newline, no trailing spaces; `fmt` is idempotent | Tool writes never differ by formatting |
| M9 | A fingerprint is stored only on the task (`basis`) | Editing a requirement never touches its tasks' files |
| M10 | A move is a `git mv` in the same commit that rewrites every reference (`needs`, `conflicts_with`, `from`, `supersedes`) and adds the old ID to `aliases` | Git's rename detection carries the other branch's edits into the moved file; references added on the other branch resolve through the alias |
| M11 | Names are lowercase | No clash on case-insensitive file systems |
| M12 | No merge driver and no `merge=union`; recommend only `<spec.dir>/** text eol=lf` | Real conflicts must surface |
| M13 | `status` is the first frontmatter line, followed by `kind` and `schema`, which never change | A status commit and an edit of any other key always have two unchanged lines between them |

**Conflicts that remain, on purpose:** two branches change the same Statement, scenario or section; two plan PRs set the same task's status to different values; two inserts into the same sort position of one list (keep both, run `fmt`); move against move; delete against modify.

**Races that merge cleanly and validate reports:** a reference to a moved node (resolves by alias; warning "uses an alias"); a requirement change against a task planned on the old fingerprint (suspect); a `needs` added on one branch and a cycle closed on the other (error after the merge). So `wavecoord spec check` belongs in the consumer's CI or `check_cmd`.

## 9. Tools

### 9.1 Modules

New package `src/wavecoord/specs/` (stdlib only):

| Module | Does |
|---|---|
| `model.py` | Records per kind, from the kind schemas |
| `doc.py` | Parse and canonical render; `parse(render(r)) == r` |
| `fingerprint.py` | Section 3.8 |
| `source.py` | A working tree (`os.walk`) or a git ref in two processes for any number of files (`git ls-tree -r -z`, one `git cat-file --batch`) |
| `tree.py` | One pass: records, the tree, aliases, the expanded needs graph, the artifact index |
| `check.py` | Validate: schema, layout, status rules, alignment A1-A7, blockers, predicate P, fingerprints |
| `ops.py` | The op API (9.3) |
| `query.py` | Reads and the query language (9.2) |
| `status.py` | Status commits (7.2) |
| `cli.py` | `wavecoord spec ...` |

`plan/spec.py` gets the YAML additions (block lists of scalars and of flat maps), a shared typed section splitter, and in-place rewriting of one frontmatter key (used by status commits so a task file is never re-rendered by the coordinator).

The kind schemas are the definitions of one file, `schemas/spec-record.schema.json` (one per kind; the validator reads references within one file only), plus `spec-ops.schema.json` for the op API and the JSON of each query (S2). A task's `exec` is checked against `task-spec.schema.json#/definitions/exec`. `task-spec.schema.json` stays for the flat store until the migration.

### 9.2 Reads and queries

| Command | Answers |
|---|---|
| `tree [node] [--offline]` | The tree with status, roll-up progress and the first unmet need per node; with the coordinator, live attempts too |
| `show <id> [--diff]` | One record as text or JSON; `--diff` for a suspect task shows how its requirement changed since `basis` |
| `check [node]` | Every rule of sections 3 to 8; exit 1 on an error |
| `waves [node]` | Section 6: waves, critical path, width |
| `ready` | Ready tasks with `why_not` for the others |
| `trace <id>` | For any node: ancestors, children, needs (direct and expanded), who needs it, producers and consumers of its artifacts, decisions, PRs and merged commits of its tasks (from `git log` trailers) |
| `artifacts [--id x]` | The artifact index: each artifact, its producer, its consumers |
| `suspect` | Tasks whose `basis` differs from their requirement's fingerprint |
| `query '<expr>'` | Records that match, e.g. `kind=task status=planned paths~src/auth/*`, `kind=requirement status=approved !outputs` |
| `fmt [--check]`, `lint`, `fp <id>` | The formatter, style warnings, one fingerprint |

Every read takes `--json`; the JSON shapes are in `spec-ops.schema.json`. **Query language:** space-separated terms, all of which must hold: `key=value`, `key!=value`, `key~glob` (any list item or the value matches), `key` (present and not empty), `!key` (absent or empty), and `under=<node>`. Keys are frontmatter keys plus the computed ones (`ready`, `blocked`, `suspect`, `depth`, `parent`). Target: 2,000 records load from a git ref and `check` in under one second (opt-in perf test).

### 9.3 Writes: the op API

`apply_ops(tree, ops) -> changes` applies ops in memory, in order, then runs the whole `check` on the tree as it would be after the change, and returns `{path: new text or None}` plus the moves. Any failed op or check error: nothing is written (must). The CLI and the MCP tools both use it.

| Op | Arguments | Effect |
|---|---|---|
| `create` | `id, kind, fields, sections` | A new record; the parent must exist (except a capability) |
| `set` | `id, key, value` | A scalar key (status goes through `set_status`) |
| `set_status` | `id, status` | Lead statuses only; checked against section 7.1 |
| `set_section` | `id, section, value` | A section; prose is split into sentences |
| `add`, `remove` | `id, key, value` | A list item, in sorted position (artifacts as maps) |
| `set_artifact`, `remove_artifact` | `id, list, artifact` | One input or output by its `id` |
| `mv` | `from, to` | Move or rename a subtree (`git mv`), rewrite references, add aliases |
| `rebase_basis` | `task` | Set `basis` to the requirement's current fingerprint |
| `supersede` | `old, new` | `new.supersedes += old`, `old.status = deprecated` |
| `delete` | `id, force?` | Refused while a reference names it or it has children, unless `force`; prefer `dropped` or `deprecated` |

**CLI** (offline, changes the working tree; you commit): `wavecoord spec new|set|status|section|add|rm|artifact|mv|rebase-basis|supersede|delete ...` and `wavecoord spec apply <ops.json>`. Every write takes `--dry-run` and prints a unified diff.

**MCP and REST** (the lead; reads from the default branch):

| Tool | Does |
|---|---|
| `spec_tree`, `spec_get`, `spec_query`, `spec_waves`, `spec_trace`, `spec_check` | The reads |
| `spec_edit` | `{ops[], branch?, title?, body?}`: applies the ops on a `plan/<n>` branch and opens one PR, with the `check` result in the PR body |

`spec_edit` replaces `plan_edit`. Its git flow is `plan/edit.py`'s (a worktree at the default branch, one commit, push `plan/<n>`, `pr_cmd`), extended with `git mv` and `git rm`. Several calls with the same `branch` build one PR.

### 9.4 The rest of wavecoord

- **Coordinator ids.** The coordinator's task id is the tree id with `.` for `/` (names have no dots, so the mapping goes both ways); leases, attempts, branches and the `Task:` line keep their format. `check` reports a task whose coordinator id is longer than 64 characters.
- **Plan store.** The tree replaces the flat `plan/tasks` store: `plan_ready`, `plan_get`, `plan_graph`, `dispatch` and `dispatch_wave` read tasks from the tree; `ready` uses section 5; `dispatch_wave` takes a set of ready tasks that predicate P allows, up to `wip_cap`. The `commands` plan adapter stays for consumers with their own tracker.
- **Worker prompt.** It gets, after Context: the requirement's Statement and Scenarios, the task's inputs (with their contracts and producers) and outputs (with their contracts), and "Also run" with the requirement's Verify. Only the task's own requirement goes in.
- **Leases.** A dispatched task claims its `paths` (exclusive) and `shared_paths` (shared), as today.
- **Gate-check.** `spec_change` (7.3): predicate S, kind `spec_change`, blocks in `advisory` and `enforce` like `config_change`, reported in `off`; `spec.dir` from the base-side config. A fence change: section 8.1 of `edit-fence.md`, seeded defect D25 and a table check. `spec` joins `gatecheck.CONFIG_KEYS`.
- **Worker proposals.** Workers never edit `spec.dir`. The report's `proposals` get the kinds `requirement_gap` (missing behavior) and `spec_change` (a statement or contract is wrong), each with `node` and optional suggested `ops` (the op API's shape, as data). `/wavecoord:revise` turns the accepted ones into one plan PR through `spec_edit`; the lead decides. Suggested ops are never applied without the lead.
- **Merge.** With `spec.verify_at_merge` (on by default), the refinery runs the task's Acceptance and its requirement's Verify, both from the default branch, after `check_cmd`; a failure blocks with `verify_failed`. A merge commit gets the trailer `Task: <task id>` (and `Spec-Basis: <fp>`), so `trace` works from `git log` alone.
- **Events.** `attempt.start` and `refinery.merge` carry `basis`; new kinds `spec.status` (a flushed status commit: sha and changes) and `spec.status_blocked`.
- **Config.**

  ```json
  "spec": {"dir": "specs", "require_approved": true, "suspect": "warn", "verify_at_merge": true,
           "status_flush_s": 60, "status_author": "wavecoord <wavecoord@localhost>"}
  ```

  `require_approved` (default on): a task is ready only when its requirement is `approved` and every ancestor is `active`; tasks may still be planned under a draft. `suspect`: `warn` or `refuse` (a suspect task is not ready). `verify_at_merge` (default on): section 9.4, Merge.

### 9.5 Skills and agents

| Part | Change |
|---|---|
| `/wavecoord:spec` (new) | Builds and revises the tree with the user: capability, features, requirements with EARS statements, contracts as inputs and outputs, Verify commands. Runs `spec_check` until clean, the reviewer in spec mode, then one plan PR. Asks the user before approving or deprecating |
| `wave-plan` | Plans tasks under requirements: each task's outputs cover its requirement's (A1), its inputs come from `needs` (A4), `paths` are disjoint where P must hold; shows `spec_waves` in the PR body. Replaces the flat task output |
| `revise` | Handles suspect tasks (read the diff, then rebase the basis or edit the task) and worker proposals, as `spec_edit` ops in one plan PR |
| `planner` | Drafts tasks for one requirement or feature, with artifacts and `needs` |
| `reviewer` | Spec mode: EARS, testable scenarios, Verify that fails without the work, contracts on every interface output, A1-A7 clean, no derived data |
| `triage`, `wave-run` | Read `spec_tree` and `spec_waves` instead of the flat plan |

### 9.6 Migration

`wavecoord spec migrate-plan [--into <cap>] [--dry-run]` moves each `plan/tasks/<id>.md` to `<spec.dir>/<cap>/unsorted/unsorted/tasks/<id>.md` (default `<cap>` is `legacy`), creating the capability, feature and requirement nodes as `draft`. It keeps `needs` (rewritten to the new IDs), `paths`, `shared_paths`, `exec`, `class`, `size` and the sections; maps `state: done` to `status: done` and other states to `planned`; adds the old ID to `aliases`; writes one commit. The lead then moves tasks under real requirements with `mv`. `wavecoord spec migrate` upgrades records when a `schema` version changes.

## 10. Tests

| File | Covers |
|---|---|
| `tests/test_spec_doc.py` | Parse and render per kind; YAML additions; errors |
| `tests/test_spec_check.py` | Each rule of sections 3 to 8, one table case per rule |
| `tests/test_spec_ops.py` | Each op; atomicity |
| `tests/test_spec_query.py` | Each read, the query language, JSON shapes |
| `tests/test_spec_status.py` | Status commits: batching, rebase and retry, only the `status` line, recovery |
| `tests/test_spec_merge.py` | The merge properties, with `tests/specworld.py` (an oracle) and `tests/specsim.py` (random trees and ops) |
| `tests/specdefects.py` | Seeded defects |

**Properties.**

| ID | Property |
|---|---|
| P0 | Round trip: `parse(render(r)) == r`; `fmt` is idempotent; a valid non-canonical file formats to the same record |
| P1 | Merge: a random valid tree in a temp repo; nodes split into disjoint sets A and B; random ops on two branches, each limited to its set; `git merge`. No conflict; `check` and `fmt --check` pass; `tree`, `waves`, `suspect` and every file equal the oracle's result |
| P2 | Distinct sorted inserts into one list, with an unchanged line between them, merge |
| P3 | Genuine conflicts surface: the same Statement, or the same status to different values, on two branches |
| P4 | Status against plan edits: a random sequence of status commits on the default branch and a plan PR that edits any other keys and sections of the same tasks merge without conflict (M13) |
| P5 | Races: a move against a reference to the old ID merges with an alias warning; a requirement change against a task planned on the old fingerprint merges with a suspect task; a cycle closed by two merges is an error |
| P6 | A failing op batch writes nothing, in the working tree and on a plan branch |
| P7 | Fingerprint: unchanged by reflow, reordering, title, status, tags, Rationale, Notes or a move; changed by a one-token change of the Statement, a step, a Verify command or an artifact |
| P8 | Parallel safety: for a random valid tree, every pair of tasks in one computed wave satisfies P (checked independently of `parallel_ok`), and every need of a task runs in an earlier wave. With the coordinator (S3): a dispatch of a whole wave gets every lease granted in fence enforce mode |
| P9 | Alignment: random trees with one injected misalignment (an unproduced output, a double producer, an input without `from` or need, a file output outside `paths`) fail `check` with that rule |
| P10 | `spec_change`: D25 and the table check |

`WAVECOORD_SPEC_SEED`, `WAVECOORD_SPEC_RUNS` and `WAVECOORD_SPEC_OPS` as for the fence tests; a failing seed goes into the fixed list with its fix.

**Seeded defects** (each must be caught):

| ID | Defect | Caught by |
|---|---|---|
| S01 | Render writes flow lists | P2 |
| S02 | Inserts append instead of sorting | P2 |
| S03 | The fingerprint includes `status` | P7 |
| S04 | The fingerprint ignores outputs | P7 |
| S05 | `mv` does not rewrite references | P1 |
| S06 | `mv` does not add an alias | P5 |
| S07 | Render puts `status` after `title` | P4 |
| S08 | A status commit re-renders the whole file | P4 |
| S09 | `needs` on a feature does not expand to its subtree | P8 |
| S10 | `check` skips A1 | P9 |
| S11 | Waves ignore `paths` overlap | P8 |

Live: `scripts/smoke-live.py` gets a `spec` check: a two-wave tree, the worker prompts hold their requirement and contracts, the status commits appear on the default branch in order, and the merge of the second wave finds the first wave's output.

## 11. Rollout

Branches `p8/spec-*`. Each PR ships its schemas, fixtures, `docs/contract.md` changes and the rebuilt zipapp.

| Phase | Scope | Done when |
|---|---|---|
| S0 | This note and `docs/specs.md` | Reviewed and merged |
| S1 | YAML additions; `specs/` model, doc, fingerprint, source, tree; kind schemas; read CLI (`tree`, `show`, `check` for sections 3 and 8, `fmt`, `lint`, `fp`, `query`) | P0, P7 pass; perf target met |
| S2 | Full `check` (alignment, blockers, P), `waves`, `ready`, `trace`, `artifacts`, `suspect`; `ops.py` and the write CLI; `migrate-plan` | P1, P2, P3, P5, P6, P8, P9 pass; S01-S06, S09-S11 caught |
| S3 | `spec_edit` and the read tools over MCP and REST; the tree as the plan store (`plan_ready`, `dispatch`, `dispatch_wave`); the worker prompt | e2e wave runs from a spec tree; one PR from several `spec_edit` calls; a move commits as a rename |
| S4 | Status commits (direct and the status-PR fallback); `spec_change` (edit-fence 8.1, D25); `verify_at_merge`; merge trailers; events | P4, P10 pass; S07, S08 caught; status appears on the default branch in an e2e wave |
| S5 | `/wavecoord:spec`, wave-plan, revise, planner, reviewer, triage, wave-run; docs; version 0.2.0 | `claude plugin validate --strict`; `test_skills`; a live dogfood run |

## 12. Decisions

Decided by the lead (S0 review):

| ID | Question | Decision |
|---|---|---|
| Q1 | `verify_at_merge` on by default? | Yes, from 0.2.0 |
| Q2 | May a task run under a `draft` requirement? | It may be planned, not run: `require_approved` is on by default |
| Q3 | External trackers (Jira, Linear) | Later, as a command adapter for the tree |
| Q4 | Status commits on a protected default branch | Fall back to the `wavecoord/status` PR (section 7.2) |
| Q5 | Workers proposing spec changes | Report proposals only (`requirement_gap`, `spec_change`), turned into plan PRs by `revise` (section 9.4) |
| Q6 | More than one requirement per task | No: one parent. Shared work becomes its own requirement that the others `need`; its output is their input |

## Prior art

- **OpenSpec**: SHALL requirements with `#### Scenario:` blocks, a strict deterministic validator, living specs. Taken: scenarios, the validator. Left: `changes/` and `archive/` (a plan PR is the change, git history the archive).
- **GitHub Spec Kit**: spec, plan, tasks; `[P]` parallel tasks on different files; `converge`. Taken: decomposition, file-disjointness as the parallel rule (computed here, not marked).
- **Kiro**: EARS, requirements linked from tasks, waves of independent tasks. Taken: EARS, waves.
- **Doorstop, sphinx-needs**: the parent's fingerprint in the child, suspect links. Taken as `basis`.
- **KEPs, ADRs**: metadata separate from prose, status lifecycles, supersession on one side. Taken: typed frontmatter, `supersedes`, decisions.
- **BMAD**: the agent stops at "built", someone else decides "done". Taken: the worker never writes status; the coordinator does.
