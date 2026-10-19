# Importing specs from other tools

Status: available (0.3.0).

A project that already uses another spec-driven setup can move it into the spec tree ([specs.md](specs.md)). `wavecoord spec import` reads the files of GitHub Spec Kit, Amazon Kiro or OpenSpec and writes them as draft records. In the lead session, `/wavecoord:spec-import` runs it as a dry run, asks what matters, and opens one plan PR; it also reads free-form documents (PRDs, architecture notes, ADRs) and proposes a tree from them.

```
wavecoord spec import --from kiro --dry-run                print the diff; the report goes to stderr
wavecoord spec import --from kiro --dry-run --json         the ops, the files and the report, as JSON
wavecoord spec import --from openspec --into notes         write the records under the capability notes
wavecoord spec import --from speckit --dir docs/specs      write into another spec dir (see Spec Kit below)
wavecoord spec import --from kiro --granularity story      one requirement per user story
```

| Flag | Meaning |
|---|---|
| `--from` | `speckit`, `kiro` or `openspec` |
| `--src PATH` | The folder that holds the other tool's files. Default: the repo root |
| `--into CAP` | The capability to create, at most 20 characters. Default: the `--from` name |
| `--granularity` | Kiro only: `criterion` (default) or `story` |
| `--dir`, `--ref`, `--json`, `--dry-run` | As for the other `spec` write commands. `--ref` works only with `--dry-run` |

## What every import does

- **One capability.** Everything goes under `--into`: the other tool's features (or OpenSpec capabilities) become features under it. Move them later with `wavecoord spec mv`.
- **Drafts.** Every record is `draft` (a decision is `proposed`). Nothing runs until you approve the requirements and activate the capability and features, in a plan PR.
- **Provenance.** Each record names its source file: `Imported from <file>` in a requirement's Notes, a feature's Intent or a task's Context. The source files are never changed or deleted; remove them yourself when the tree replaces them.
- **EARS.** A statement is rewritten into EARS form when the rewrite is mechanical (`System MUST x` becomes `The system shall x.`, `WHEN a THEN the system SHALL b` becomes `When a, the system shall b.`). Anything else is kept as written and flagged.
- **Verify.** None of the three tools has Verify commands, so Verify stays empty and the report prints `add a Verify command` for each requirement. `check` passes; `check` rejects a requirement only when it is approved without a scenario or Verify.
- **Tasks.** A task item with a file path in its text becomes a task, with `paths` from those files and the "S" starting point of the exec plan (`/wavecoord:wave-plan` sizes it). An item without a file path, or one checked done, is listed in its requirement's Notes instead, and what waited for it waits for what it waited for. Two tasks that may run together and name the same file get `conflicts_with`.
- **Names.** Ids are lowercase, unique, and short enough that a task's coordinator id stays within 64 characters. The same files give the same ids and the same records.
- **Checked.** The records go through the op API, so the result passes `wavecoord spec check`: errors refuse the import, warnings are allowed.
- **Re-import.** When the capability exists and the import would write the same files, nothing changes. When it differs, the import is refused: pick another `--into`, or delete the old capability first.

The report lists what was created, what a person has to look at (`non-ears`, `no-paths`, `done`, `conflict`, `unknown-section`, `grouping`, `unlinked`, `delta`, `scenario`, `bugfix`, `no-purpose`), a Verify warning per requirement, and the next steps.

## GitHub Spec Kit

Spec Kit keeps a constitution in `.specify/memory/constitution.md` (or `memory/constitution.md`) and one folder per feature, `specs/<NNN>-<feature>/`, with `spec.md`, `plan.md`, `research.md`, `data-model.md`, `contracts/` and `tasks.md`.

| Spec Kit | wavecoord |
|---|---|
| `constitution.md` with a `**Ratified**` date | a decision `<cap>/decisions/<date>-constitution`: principles in Decision, Governance in Consequences |
| `constitution.md` without a date | the capability's Design |
| `specs/<NNN>-<name>/` | the feature `<cap>/<name>` (the number is dropped) |
| `**Input**: User description` | the feature's Intent |
| `User Story N - Title (Priority: PN)` | the requirement `us-N`, tagged `pN`: the story text as Statement (flagged), `**Why this priority**` as Rationale, the acceptance scenarios as Given/When/Then |
| `FR-NNN` | the requirement `fr-NNN`, its statement in EARS when mechanical |
| Edge Cases, `[NEEDS CLARIFICATION: ...]` | the feature's Open questions |
| Key Entities, Success Criteria, Assumptions, Clarifications, `plan.md`, `research.md` | the feature's Design; other files are named there |
| `- [ ] T012 [P] [US1] ... in src/x.py` | the task `t012` under `us-1`, `paths: [src/x.py]` |
| tasks without `[USn]` before the first story phase | under the requirement `setup`, which the stories need |
| tasks without `[USn]` after the story phases | under the requirement `polish`, which needs the stories |
| order and `[P]` | in a phase (or a `###` part of one), a `[P]` task needs only what the phase waits for; any other task needs every open task before it in the phase; the next phase needs the last ones; `depends on T004` adds that task |

**`specs/` is also wavecoord's spec dir.** By default `spec.dir` is `specs`, where Spec Kit keeps its features. The import refuses to write into a folder that holds the source files. Either import into another folder and point wavecoord at it:

```
wavecoord spec import --from speckit --dir docs/specs
```

then set `"spec": {"dir": "docs/specs"}` in `.claude/wavecoord.json`; or move the Spec Kit folder (`git mv specs speckit`) and pass `--src speckit`.

Gaps: user stories and FRs are not linked in Spec Kit, so FRs have no tasks and stories have no FRs; `SC-NNN` success criteria stay prose in Design; `setup` and `polish` are groupings the import makes, flagged for you; `data-model.md` and `contracts/` are named, not parsed into outputs; checklists (`checklists/*.md`) are not read.

## Amazon Kiro

Kiro keeps one folder per spec, `.kiro/specs/<feature>/`, with `requirements.md` (or `bugfix.md`), `design.md` and `tasks.md`, and project-wide steering files in `.kiro/steering/`.

| Kiro | wavecoord |
|---|---|
| `.kiro/steering/*.md` | the capability's Design (the first paragraph of `product.md` also goes into its Intent) |
| `.kiro/specs/<feature>/` | the feature `<cap>/<feature>` |
| `## Introduction` | the feature's Intent |
| `### Requirement N`, `**User Story:**` | with `criterion` (default): one requirement `rN-M` per acceptance criterion, the user story as Rationale |
| | with `story`: one requirement `rN` per story; a single criterion is the statement, several are listed in Notes and the story is the statement (flagged) |
| `N. WHEN ... THEN the system SHALL ...` | the statement, in EARS form (`When ..., the system shall ...`; IF, WHILE and WHERE too) |
| `design.md` | the feature's Design |
| `bugfix.md` | the feature's Design, flagged: write its requirements by hand |
| `- [ ] 2.1 ...` with `_Requirements: 1.2, 1.3_` | the task `t2-1` under the first requirement it names; the others in its Notes (`Also covers`) |
| a parent item `- [ ] 2. ...` | its sub-items are the tasks; its text is their Context, its details and requirements are theirs too |
| `- [ ]* 2.3` (optional) | tagged `optional` |
| order | each task needs the one before it in the file |
| no `_Requirements:_` | under the requirement `unlinked`, flagged |

Gaps: Kiro writes no dependencies between tasks, so the import keeps them in file order, one after another: loosen the `needs` where tasks can run together. The `_Requirements:_` line and the `- [ ]*` mark follow the files Kiro writes, not a published format. Kiro has no scenarios; add them. Steering front matter (`inclusion`, `fileMatchPattern`) is dropped.

## OpenSpec

OpenSpec keeps the current behaviour in `openspec/specs/<capability>/spec.md` and each proposed change in `openspec/changes/<change>/`, with `proposal.md`, `design.md`, `tasks.md` and delta specs under `specs/<capability>/spec.md`. Finished changes move to `openspec/changes/archive/`.

| OpenSpec | wavecoord |
|---|---|
| `openspec/project.md` | the capability's Intent (Purpose) and Design |
| `openspec/specs/<c>/spec.md` | the feature `<cap>/<c>`, its `## Purpose` as Intent |
| `### Requirement: Name` | the requirement `<cap>/<c>/<name>`: the sentence with SHALL or MUST is the statement, the rest Notes |
| `#### Scenario: Name` with `- **WHEN**`, `**THEN**`, `**AND**` | a scenario with those steps |
| other sections of a spec | the feature's Design, flagged |
| `changes/<change>/specs/<c>/spec.md`, `## ADDED Requirements` | a new requirement |
| `## MODIFIED Requirements` | the requirement's statement and scenarios replaced |
| `## REMOVED Requirements` | the requirement `deprecated`, the reason in Notes (never deleted) |
| `## RENAMED Requirements` (`FROM:`, `TO:`) | the requirement under its new name |
| `proposal.md` | the requirement `<cap>/changes/<change>`: Why as Rationale (its first sentence as the statement, flagged), the rest in Notes |
| `design.md` of a change | that requirement's Notes |
| `tasks.md`: `- [ ] 1.1 ...` | the task `t1-1` under that requirement; each task needs the one before it |
| `changes/archive/` | skipped: its deltas are in `openspec/specs` already |

Changes are applied in name order, each in OpenSpec's order: RENAMED, REMOVED, MODIFIED, ADDED.

Gaps: a change's tasks name no requirement, so they sit under the change's requirement: move them with `wavecoord spec mv`. Most OpenSpec tasks name no file and stay in Notes. `openspec/config.yaml` and `.openspec.yaml` are not read. A renamed requirement gets the new id and no alias, as nothing ran under the old one.

## Free-form documents

PRDs, BMAD documents (`docs/prd.md`, `docs/architecture.md`, `docs/stories/`), ADR folders and README sections have no fixed format, so there is no importer for them. `/wavecoord:spec-import` reads them, proposes capabilities, features and EARS requirements with scenarios following the `/wavecoord:spec` rules, turns ADRs into decision records, confirms the shape with you, and opens the plan PR with `spec_edit`.

## Formats

The mappings follow the public docs of each tool, read in October 2026:

- Spec Kit: [spec template](https://github.com/github/spec-kit/blob/main/templates/spec-template.md), [tasks template](https://github.com/github/spec-kit/blob/main/templates/tasks-template.md), [plan template](https://github.com/github/spec-kit/blob/main/templates/plan-template.md), [README](https://github.com/github/spec-kit).
- Kiro: [Specs](https://kiro.dev/docs/specs/), [Feature specs](https://kiro.dev/docs/specs/feature-specs/), [Best practices](https://kiro.dev/docs/specs/best-practices/), [Steering](https://kiro.dev/docs/steering/).
- OpenSpec: [README](https://github.com/Fission-AI/OpenSpec), [Concepts](https://github.com/Fission-AI/OpenSpec/blob/main/docs/concepts.md), [Writing specs](https://github.com/Fission-AI/OpenSpec/blob/main/docs/writing-specs.md).

Sample projects for each are in `tests/fixtures/sdd/`.
