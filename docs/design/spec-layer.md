# Design: the spec layer (P8)

Status: proposed (S0). No code yet. The phases in section 9 each ship as one PR.

wavecoord tracks execution: task specs in `plan/tasks`, the event log on `coord/log`, the fence, gate-check and the refinery. Above a task it has nothing. There are no requirement IDs, a task cannot say which requirement it implements, no event records which version of a requirement a worker built, and acceptance is self-reported. This note adds a spec layer: requirements as Markdown files in the consumer repo, tracked by git, linked from task specs, read and changed by tools in the plugin. "Must" marks a rule that the implementation has to meet and the tests have to check.

Background and sources: the research report behind this note found that clear, checkable requirements measurably help agents (ambiguity costs about 7 points of pass rate on average; structured, test-checked requirements resolved about 13% more tasks), and that spec-driven development fails when specs bloat, go stale or conflict. No study shows that a heavy spec process beats plain planning. So the layer is small, every requirement is checkable, and nothing in it is a second copy of something else.

## 1. Goals and non-goals

**Goals.**

- G1. Requirements live in the consumer repo as Markdown, one file each, reviewed in PRs like code.
- G2. The files merge well: two branches that change different requirements never conflict, and a conflict that git reports is a real disagreement.
- G3. Tools in the plugin parse, query and change the files quickly and safely: a CLI that edits the working tree, MCP tools that open a plan PR, and one structured op API behind both.
- G4. Each task names the requirements it implements, with a fingerprint, so a changed requirement shows which tasks to look at again.
- G5. The worker sees the requirements it implements. The merge can re-run their checks from the default branch.

**Non-goals.**

- N1. Specs are not the source of the code (no "spec-as-source" generation).
- N2. No new approval system. A plan PR that a human merges is the approval, as for task specs today.
- N3. No requirement IDs that a server or counter allocates.
- N4. No sync with an external tracker in this design (a later adapter, section 10).
- N5. No change to how the fence decides an edit (section 7.4 adds one merge-time rule, in the fence's own design note).

## 2. Layout and IDs

```
<spec.dir>/                                  default specs/, set in .claude/wavecoord.json
  <cap>/spec.md                              a capability: intent, non-goals, design, interfaces
  <cap>/req/<slug>.md                        one requirement
  <cap>/decisions/<YYYY-MM-DD>-<slug>.md     one decision (ADR)
plan/tasks/<id>.md                           task specs, as today, plus `implements:`
```

- A capability is a folder, one level deep. `<cap>` and `<slug>` match `^[a-z0-9][a-z0-9-]{0,62}$`: lowercase only, so two branches cannot add `Foo` and `foo` on a case-insensitive file system.
- The ID is the path. Capability `auth`, requirement `auth/token-rotation`, decision `auth/decisions/2026-10-19-rotating-tokens`. A requirement ID has two segments and a decision ID three, so they cannot collide.
- No other file is allowed under `spec.dir`: `validate` reports it. There is no index, README or table of contents (rule M5).

## 3. Formats

All three documents use the YAML subset of task specs (`docs/task-spec.md`), extended with block lists (section 3.5), between `---` lines, then fixed `## ` sections. An unknown key or an unknown `## ` section is an error. Keys and sections have a fixed order; the formatter writes them in that order.

### 3.1 Capability: `<cap>/spec.md`

```markdown
---
title: Session authentication
status: active
owners:
  - team-auth
tags:
  - security
---

## Intent
Users stay signed in across restarts without long-lived secrets on the device.

## Non-goals
- Single sign-on with third parties.

## Design
Access tokens live 15 minutes.
Refresh tokens rotate on every use.

## Interfaces
`POST /auth/refresh` takes a refresh token and returns a new pair.

## Open questions
- Do we revoke all sessions on a password change?
```

| Key | Required | Values |
|---|---|---|
| `title` | yes | text |
| `status` | yes | `draft`, `active`, `deprecated`, `retired` |
| `owners` | no | block list |
| `tags` | no | block list |

Sections: Intent (required), Non-goals, Design, Interfaces, Open questions. Design and Interfaces may hold `###` subsections, code fences and tables for contracts that rarely change.

### 3.2 Requirement: `<cap>/req/<slug>.md`

```markdown
---
title: Token rotation on expiry
status: approved
tags:
  - security
supersedes:
  - auth/rotate-on-login
aliases:
  - auth/token-refresh
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

## Notes
```

| Key | Required | Values |
|---|---|---|
| `title` | yes | text |
| `status` | yes | `draft`, `approved`, `deprecated` |
| `tags` | no | block list |
| `supersedes` | no | block list of requirement IDs |
| `aliases` | no | block list of old IDs (written by `rename`) |

Sections:

- **Statement** (required): one sentence in EARS form, with ` shall ` exactly once. The tools classify it (ubiquitous "The X shall", event "When", state "While", unwanted "If ... then", optional "Where", or a combination) but never store the class.
- **Scenarios**: `### <name>`, then one step per line, each starting with Given, When, Then, And or But.
- **Verify**: one shell command per item, in backquotes, as in a task's Acceptance. Each command runs from the repo root, needs no secret or network, and fails when the requirement is not met.
- **Rationale**, **Notes**: prose.

An `approved` requirement must have a Statement that classifies, and at least one Scenario or Verify item.

### 3.3 Decision: `<cap>/decisions/<YYYY-MM-DD>-<slug>.md`

```markdown
---
title: Use rotating refresh tokens
status: accepted
affects:
  - auth/token-rotation
supersedes:
  - auth/decisions/2026-03-01-static-tokens
---

## Context
## Decision
## Consequences
## Alternatives
```

`status` is `proposed`, `accepted` or `rejected`. Context, Decision and Consequences are required. The date is only in the file name and must be a valid date.

### 3.4 Task spec: `implements`

```markdown
implements:
  - auth/token-rotation@1a2b3c4d
```

An optional block list. Each item is `<requirement id>@<fingerprint>` and matches `^[a-z0-9][a-z0-9-]{0,62}/[a-z0-9][a-z0-9-]{0,62}@[0-9a-f]{8}$`. Items are unique. The flow form (`implements: [a@..., b@...]`) is accepted on input; the tools write the block form.

### 3.5 Block lists in the YAML subset

`parse_frontmatter` (`src/wavecoord/plan/spec.py`) gets one new form: a top-level `key:` with no value, followed by lines indented with `- item`, is a list of scalars. Nested lists and maps in a list are errors. `exec:` stays the only nested map, and `exec:` followed by `- ` is an error. An empty `key:` with nothing under it is still an error; an empty list is `key: []`. Every file that parses today parses to the same value (must, tested on every fixture).

### 3.6 Fingerprint

```
fp = sha256(b"wavecoord-req-fp/1\n" + canonical_json({
        "statement": norm(statement),
        "scenarios": sorted([norm(name), [[keyword.lower(), norm(text)] for each step]] for each scenario),
        "verify":    sorted(norm(cmd) for each command)}))[:8 hex]
```

- `norm(s)`: Unicode NFC, strip one pair of backquotes around a command, collapse whitespace to one space.
- Canonical JSON: sorted keys, no spaces, UTF-8.
- In: the meaning of Statement, Scenarios and Verify. Out: title, status, tags, aliases, supersedes, Rationale, Notes, and the path, so a reflow, a reorder, a status change or a rename does not make links suspect.
- The prefix `wavecoord-req-fp/1` versions the algorithm. Changing it makes every link suspect, on purpose.
- 32 bits are enough to notice an accidental change. The fingerprint is not a security check.

## 4. Merge-friendliness

Git's three-way merge reports a conflict when both sides change the same lines, or change lines that touch with no unchanged line between them. Each rule below follows from that, and each has a test (section 8).

| Rule | Must | Why it merges |
|---|---|---|
| M1 | One requirement per file; the ID is the path; no allocated numbers | Two branches that work on different requirements touch different files. There is no "next number" line for both to change |
| M2 | One fact per line: each key, list item, scenario step and verify command on its own line | A change is a one-line hunk |
| M3 | Block lists in spec files and in `implements`; `[]` only for an empty list | Two branches that add different items change different lines. A flow list puts every item on one line |
| M4 | Unordered lists (`tags`, `owners`, `supersedes`, `aliases`, `affects`, Verify, `implements`) are kept sorted; tools insert in sorted position. Scenario steps, scenario order and prose keep the author's order | Inserts at different positions, with an unchanged line between them, merge. Appends always meet at the end |
| M5 | No committed derived data: no index, README, counts, status rollups, `implemented`/`verified` fields, requirement-to-task links or "superseded by" fields. The tools compute them | Derived data turns independent changes into changes of one shared line |
| M6 | Prose has one sentence per line. Tool writes split sentences; `fmt` never reflows | A sentence edit is a one-line hunk; a reflow rewrites the paragraph |
| M7 | No tables for data that changes; `lint` warns on tables in `req/` and `decisions/` | A row change rewrites the alignment of other rows |
| M8 | One canonical format, written by every tool and checked by `wavecoord spec fmt --check`: fixed key and section order, one blank line between sections, LF, final newline, no trailing spaces. `fmt` is idempotent | Two tool writes never differ by formatting, so they never make a spurious hunk |
| M9 | The fingerprint is stored only in the task's `implements` | Editing a requirement never touches a task file; linking a task never touches a requirement file |
| M10 | A rename is a `git mv` in the same commit that rewrites the links to it and adds the old ID to the new file's `aliases` | Git's rename detection carries an edit from the other branch into the renamed file; a link added to the old ID on the other branch still resolves |
| M11 | IDs are lowercase | No clash on case-insensitive file systems |
| M12 | No merge driver and no `merge=union`. The only recommended attribute is `<spec.dir>/** text eol=lf` | A union merge duplicates changed lines and corrupts the frontmatter. Real conflicts must surface |

**Conflicts that remain, on purpose.** These are disagreements a person must settle, and git reports them:

- two branches change the same Statement, scenario or section;
- two branches change the `status` of the same document to different values;
- two branches insert into the same sort position of one list (resolve: keep both, run `fmt`);
- rename against rename, and delete against modify.

**Races that merge cleanly but `validate` reports.**

- Branch A renames X; branch B links to X. The link resolves through `aliases`; `validate` warns "uses an alias; relink".
- Branch A changes X's Statement; branch B links X with the old fingerprint. The link is suspect.
- Branch A deprecates X; branch B links it. `validate` warns; dispatch can refuse (section 7.2).

So `wavecoord spec validate` belongs in the consumer's CI or `check_cmd`, after the merge.

## 5. Lifecycle

Files store only human decisions. Everything else is computed.

| Document | Stored `status` | Computed |
|---|---|---|
| Capability | `draft`, `active`, `deprecated`, `retired` | requirement counts, coverage |
| Requirement | `draft`, `approved`, `deprecated` | `superseded`, `linked`, `implemented`, `verified`, `suspect` |
| Decision | `proposed`, `accepted`, `rejected` | `superseded` |

Computed requirement states:

- `superseded`: an `approved` requirement lists it in `supersedes`.
- `linked`: a task's `implements` names it.
- `implemented`: a linking task has the effective state `done`.
- `verified`: the last refinery merge of a linking task ran its Verify commands and they passed (section 7.5; from the event log).
- `suspect`: a link's fingerprint differs from the current one.

Supersession is stored only on the new document (`supersedes`), never as a back link.

Status changes the op API allows: `draft` to `approved` or `deprecated`, `approved` to `deprecated`, and `deprecated` back to `approved` only with `force`. Approving or deprecating is a lead decision in a plan PR (section 7.4).

**Validate rules.**

| ID | Rule |
|---|---|
| V1 | An `approved` requirement has a classifiable Statement and at least one Scenario or Verify item |
| V2 | `supersedes` targets exist, are not `draft`, and form no cycle |
| V3 | Aliases are unique and do not name a live ID |
| V4 | A `retired` capability has no `approved` requirement |
| V5 | A decision's `affects` and `supersedes` resolve |
| V6 | Each `implements` link resolves (directly or by alias) and has a well-formed fingerprint |
| V7 | A task that is not `done` or `dropped` and links a `deprecated` or `superseded` requirement gets a warning |
| V8 | Every file is in canonical form (`fmt --check`) and is in the layout of section 2 |

Suspect links are a separate list, not an error: a suspect link is a question for the lead, not a broken file.

## 6. Tools

### 6.1 Modules

New package `src/wavecoord/specs/` (stdlib only):

| Module | Does |
|---|---|
| `model.py` | Plain data for the three documents |
| `doc.py` | Parse and canonical render; `parse(render(m)) == m` |
| `fingerprint.py` | Section 3.6 |
| `source.py` | `TreeSource`: a working tree (`os.walk`), or a git ref in two processes for any number of files (`git ls-tree -r -z`, then one `git cat-file --batch`) |
| `corpus.py` | One pass over a source: documents, fingerprints, aliases, task links |
| `ops.py` | The op API (6.2) |
| `query.py` | The reads (6.3) |
| `cli.py` | `wavecoord spec ...` (6.4) |

`plan/spec.py` gets the block lists (3.5), a generic section splitter `parse_doc(text, sections)` that task specs and spec documents share, and `set_frontmatter_list(text, key, items)`, which rewrites one key of a frontmatter in place. The link ops use it, so a task file is never re-rendered.

### 6.2 The op API

`apply_ops(tree, ops, cfg) -> changes` takes a list of ops, applies them in memory in order, validates the whole corpus as it would be after the change (all spec documents and all task links), and returns `{path: new text or None}` plus a list of renames. If any op fails or validation fails, nothing is written (must). The CLI and the MCP tools both call it.

| Op | Arguments | Writes |
|---|---|---|
| `create_capability` | `cap, title, intent, status=draft` | `spec.md` |
| `create_requirement` | `id, title, statement, scenarios?, verify?, tags?, status=draft` | `req/<slug>.md` |
| `create_decision` | `cap, slug, date?, title, context, decision, consequences, affects?` | `decisions/...` |
| `set` | `doc, key, value` (scalar keys: `title`, `status`) | one document |
| `set_section` | `doc, section, text` or `items` | one document; prose split into sentences |
| `add_item`, `remove_item` | `doc, field, value` (a frontmatter list or a list section) | one document; sorted when unordered |
| `set_scenario`, `remove_scenario` | `id, name, steps?` | one requirement |
| `rename` | `from, to` (capability or requirement) | `git mv`; `to` gets `aliases += from`; rewrites `implements`, `supersedes` and `affects`; the fingerprint does not change |
| `deprecate` | `id, reason` | `status: deprecated`; the reason as one line in Notes |
| `supersede` | `old, new` | `new.supersedes += old` |
| `link`, `unlink` | `task, id` | the task's `implements` (link writes the current fingerprint) |
| `relink` | `task, id?` | refresh fingerprints: the lead read the change and the task still fits. Without `id`, every suspect link of the task |
| `delete` | `doc, force?` | refused while a link, `supersedes` or `affects` names it, unless `force`; prefer `deprecate` |

The op shapes are in a new `schemas/spec-doc.schema.json`, with the parsed documents and the JSON of each query.

### 6.3 Reads

| Query | Answers |
|---|---|
| `list [cap] [--status s] [--view suspect\|coverage\|uncovered]` | Capabilities and requirements with their computed states |
| `show <id> [--diff]` | One document, its fingerprint, computed state and text; `--diff` shows how its fingerprinted sections changed since the fingerprint a task links |
| `trace <id\|task>` | For a requirement: linking tasks (fingerprint ok or suspect, task state), decisions that affect it, the supersedes chain. For a task: its requirements |
| `suspect` | Every suspect link: task, requirement, linked and current fingerprint |
| `coverage [cap]` | Approved requirements with no link, with no `done` task, or with no Verify |
| `context <task>` | The Markdown block of the task's requirements, for the worker prompt |
| `validate`, `lint`, `fmt [--check]`, `fp <id>` | Section 5 rules; style warnings; the formatter; one fingerprint |

Every read takes `--json`. `list --view suspect` and `validate` exit 1 when they find something, for CI. Target: 2,000 requirements and 500 tasks load from a git ref in under one second (an opt-in perf test, `WAVECOORD_PERF=1`).

### 6.4 CLI, MCP, REST

**CLI** (offline; changes the working tree; you commit as usual):

```
wavecoord spec list|show|trace|suspect|coverage|context|validate|lint|fmt|fp ...
wavecoord spec new-cap|new-req|new-decision|set|section|add|rm|scenario|rename|deprecate|supersede ...
wavecoord spec link|unlink|relink|delete ...
wavecoord spec apply <ops.json> [--dry-run]
```

Each write takes `--dry-run` and then prints a unified diff. `rename` runs `git mv` inside a repo. `apply` takes the same op JSON as the MCP tool.

**MCP** (lead role, reads from the default branch):

| Tool | Arguments | Does |
|---|---|---|
| `spec_list` | `cap?, status?, view?` | `list` |
| `spec_get` | `id?` or `task?` | `show`, or `context` for a task |
| `spec_trace` | `id` | `trace` |
| `spec_validate` | | `validate` |
| `spec_edit` | `ops[], edits[]?, branch?, title?, body?` | applies the ops on a `plan/<n>` branch and opens a PR, like `plan_edit` |

REST mirrors them under `/api/spec/*` (`docs/contract.md` sections 6 and 8).

**One PR for specs and tasks.** `plan/edit.py` is split into `commit_files()` (the worktree, commit, push of `plan/<n>` and PR, now also with `git mv` and `git rm`) and `validate_files()` (the validator by path: a task spec under `plan.dir`, a spec document under `spec.dir`). `plan_edit` and `spec_edit` both use them and both take `branch`, so a skill can create requirements and then the tasks that link them on the same branch, in one PR. Link checks read that branch's tree, so a new requirement resolves. The anchored edit schema gets `area: "plan" | "spec"` (default `plan`); an anchored edit of a spec document must still leave it in canonical form.

With the command plan adapter (`plan.adapter: "commands"`), the spec ops work, but `link`, `unlink`, `relink` and the link rewrite of `rename` refuse: the consumer's adapter owns the task specs.

## 7. Integration with the rest of wavecoord

### 7.1 Config

```json
"spec": {"dir": "specs", "require_approved": false, "suspect": "warn", "verify_at_merge": false}
```

`suspect` is `warn` or `refuse`. `spec` joins `gatecheck.CONFIG_KEYS`, so a task PR cannot change it (the `config_change` rule of `edit-fence.md` section 8).

### 7.2 Dispatch and ready

`plan_ready` and `dispatch` add `why_not` reasons:

- `requirement <id> is <status>`, when `require_approved` is on and a linked requirement is not `approved` (or is superseded);
- `suspect link <id>`, when `suspect` is `refuse`.

With `suspect: warn`, the reason goes into the dispatch result and the task is dispatched.

### 7.3 Worker prompt

`prompt_from_spec()` (`src/wavecoord/launcher/client.py`) adds `## Requirements` after Context: for each linked requirement, its ID and title, Statement, Scenarios, and its Verify commands under "Also run". The text comes from the default branch, not from the worker's worktree. Only the linked requirements go in: per-task context stays small.

### 7.4 The merge check: `spec_change`

A task PR (one with the `Task:`, `Attempt:` and `Lease:` lines) must not change a file under `spec.dir`. Requirements are lead decisions, made in plan PRs. Predicate S, for a task PR with the net diff `D = merge-base(base, head)..head`: no path of `D` is under `spec.dir`, where `spec.dir` comes from the config on the base side, so a worker cannot move it. Gate-check reports each path as a violation of kind `spec_change`; it blocks in the `merge` modes `advisory` and `enforce` and is only reported in `off`, exactly as `config_change`. The refinery blocks with reason `spec_change` and tells the lead to make the change in a plan PR (`spec_edit`).

This is a fence change. Its implementation PR adds section 8.1 to `edit-fence.md` with predicate S, seeded defect D25 (`gatecheck.spec_changes` returns nothing) and a table check `spec_change_check`, as D24 did for `config_change`.

### 7.5 Verify at merge

With `verify_at_merge` on, the refinery runs, after `check_cmd`, in the same check worktree at the PR head: the task's Acceptance commands and the Verify commands of its linked requirements. Both lists come from the **default branch** (the plan and the specs as merged), never from the PR, so a worker cannot weaken the checks it is graded by. The environment has `WAVECOORD_TASK`, `WAVECOORD_ATTEMPT` and `WAVECOORD_REQS` (`id,id`). A failure blocks with `verify_failed`. A pass adds `{reqs, fps}` to the `refinery.merge` event, which gives the computed `verified` state. This also closes the self-grading gap: today the refinery runs only `check_cmd`.

### 7.6 Skills and agents

| Part | Change |
|---|---|
| `/wavecoord:spec` (new skill) | Writes and revises capabilities, requirements and decisions with `spec_edit`. Interviews the user, writes EARS statements and runnable Verify commands, runs the reviewer in spec mode, opens one plan PR. Asks the user before it approves or deprecates. References: `ears.md`, `spec-templates.md` |
| `wave-plan` | Starts from `spec_list view=coverage`. With `--spec <cap>` it plans that capability's uncovered requirements. Missing requirements are created first, on the same branch. Each task gets `implements`; the checklist adds "Acceptance covers the Verify of each linked requirement" |
| `revise` | Handles suspect links: reads the change (`spec_get --diff`), then relinks (the task still fits) or edits the task and relinks, in one plan PR. New proposal kinds `requirement_gap` and `spec_change` from workers |
| `reviewer` agent | Spec mode: EARS statements, testable scenarios, Verify commands that fail without the work, no derived data, no suspect links in the plan it reviews |
| `planner` agent | Reads `spec_get` for the requirements of its area; puts `implements` on each task |

## 8. Tests

| File | Covers |
|---|---|
| `tests/test_spec_doc.py` | Parse and render of each document; errors |
| `tests/test_spec_fp.py` | Fingerprint invariance and sensitivity |
| `tests/test_spec_ops.py` | Each op; atomicity |
| `tests/test_spec_query.py` | Each query and its JSON |
| `tests/test_spec_merge.py` | The merge properties, with `tests/specworld.py` (an oracle) and `tests/specsim.py` (the random ops) |
| `tests/specdefects.py` | Seeded defects |

**Properties.**

| ID | Property |
|---|---|
| P0 | Round trip: `parse(render(m)) == m`; `fmt(fmt(x)) == fmt(x)`; a valid but non-canonical file (flow lists, unsorted tags, extra blank lines) formats to the same model; every existing task fixture parses to the same value as before |
| P1 | The merge property. A random base corpus in a temp repo; requirements and tasks split into disjoint sets A and B; random ops (`WAVECOORD_SPEC_OPS`, default 60) on two branches, each limited to its set; `git merge`. Then: no conflict, `validate` and `fmt --check` pass, and `suspect`, `trace` and each file's text equal the oracle's result of applying both op lists to an abstract model |
| P2 | Distinct sorted inserts into the same list, with an unchanged line between them, merge cleanly |
| P3 | Genuine conflicts are not hidden: the same Statement, or the same `status` to different values, on two branches makes `git merge` report a conflict |
| P4 | Races: rename against a link to the old ID merges with an alias warning; a Statement change against a link with the old fingerprint merges with a suspect link |
| P5 | A failing op batch writes nothing, in the working tree and on a plan branch |
| P6 | Fingerprint: unchanged by reflow, reordering, and changes to title, status, tags, Rationale, Notes or path; changed by a one-token change of the Statement, a step, a step keyword or a Verify command |
| P7 | `spec_change`: covered by D25 and the table check (section 7.4) |

`WAVECOORD_SPEC_SEED=<n>` reruns one seed, `WAVECOORD_SPEC_RUNS=<n>` adds random seeds, `WAVECOORD_SPEC_OPS=<n>` sets the ops per branch. CI runs a fixed seed list and one new random seed. A failing seed goes into the fixed list with its fix.

**Seeded defects.** Each must be caught; a defect that no test catches fails the suite.

| ID | Defect | Caught by |
|---|---|---|
| S01 | Render writes flow lists | P2 |
| S02 | Inserts append instead of sorting | P2 |
| S03 | The fingerprint includes `status` | P6 |
| S04 | The fingerprint ignores Scenarios | P6 |
| S05 | `rename` does not rewrite links | P1 |
| S06 | `rename` does not add an alias | P4 |
| S07 | `spec.md` holds a committed requirement count | P1 (conflict) |
| S08 | The block-list parser drops the last item | P0 |

Live check: `scripts/smoke-live.py` gets a `spec` check: a task linked to a requirement, the worker prompt contains the requirement, and the merge with `verify_at_merge` records `{reqs, fps}`.

## 9. Rollout

Branches `p8/spec-*`. Each PR ships its schema, fixtures, `docs/contract.md` changes and the rebuilt zipapp.

| Phase | Scope | Done when |
|---|---|---|
| S0 | This note and the user guide `docs/specs.md` | Reviewed and merged |
| S1 | Block lists in the YAML subset; `specs/` model, doc, fingerprint, source, corpus; read CLI (`list`, `show`, `validate`, `lint`, `fmt`, `fp`); `spec-doc.schema.json`; `spec.dir` | P0 and P6 pass; every task fixture unchanged; perf target met |
| S2 | `ops.py` and the write CLI; `implements` in the task schema; `trace`, `suspect`, `coverage`, `context`; link checks in `plan validate` | P1 to P5 pass; S01 to S08 caught |
| S3 | `edit.py` split; `area` in the edit schema; MCP and REST `spec_*` | One PR from `spec_edit` and `plan_edit` on one branch; a rename commits as a rename (`git diff -M` shows `R100`) |
| S4 | Prompt `## Requirements`; dispatch gating; `spec_change` (edit-fence 8.1, D25); `verify_at_merge` | Fence suites pass with D25 caught; the e2e worker prompt holds the requirement; a failing Verify blocks with `verify_failed` |
| S5 | `/wavecoord:spec`; wave-plan, revise, reviewer, planner; docs; version 0.2.0 | `claude plugin validate --strict`; `test_skills`; a live dogfood run |

Each phase is useful on its own: S1 gives a linter and a formatter for hand-written specs, S2 the CLI to change them, S3 the lead's tools, S4 the guarantees, S5 the workflow.

## 10. Open decisions

| ID | Question | Proposed default |
|---|---|---|
| Q1 | Should `verify_at_merge` default to on once S4 ships? | Off in 0.2.0; on in a later release after real use |
| Q2 | Should a `draft` requirement be linkable at all? | Yes, with `require_approved: false`; the reviewer flags it |
| Q3 | Requirements from an external tracker (Jira, Linear) | Later: a command adapter for specs, like `plan.adapter: "commands"` for tasks |
| Q4 | Store the Verify results of `verify_at_merge` in the repo? | No (M5). Only in the event log |
| Q5 | Should workers propose requirement changes? | Yes, as report proposals (`requirement_gap`, `spec_change`) that `revise` turns into a plan PR; never by editing `spec.dir` (7.4) |

## Prior art

- **OpenSpec**: requirements with `#### Scenario:` blocks and SHALL statements, a strict validator, deltas merged into living specs. Taken: scenarios, a deterministic validator, living specs. Left: the `changes/` and `archive/` folders, because a plan PR is the change and git history is the archive.
- **GitHub Spec Kit**: spec, plan and tasks per feature; `[P]` parallel tasks; `converge`, which appends work and never rewrites tasks. Taken: requirement-to-task decomposition and the append-only idea for revise. Left: a feature folder per change and numbered feature branches.
- **Kiro**: EARS statements, requirement IDs on tasks, waves of independent tasks. Taken: EARS and links from tasks. Left: the fixed three-document set per feature.
- **Doorstop** and **sphinx-needs**: a fingerprint of the parent stored in the child link, and "suspect links". Taken as section 3.6 and the `suspect` view.
- **ADRs (Nygard, MADR)** and **KEPs**: an immutable decision with a status lifecycle, supersession recorded on one side, and machine-readable metadata next to prose. Taken as section 3.3.
- **Tessl**: specs as the source of code. Not taken (N1).
