# Lesson 5: Bring specs from Spec Kit, Kiro or OpenSpec

**Video:** [course-05-import-specs.mp4](https://github.com/jellologic/wavecoord/releases/download/v0.3.0/course-05-import-specs.mp4) (about 5 minutes)

## Goals

- Import the files of GitHub Spec Kit, Amazon Kiro or OpenSpec with `/wavecoord:spec-import`.
- Review the draft import: provenance, EARS rewrites, flags.
- Add Verify commands, then approve in a separate plan PR.
- Handle the Spec Kit folder clash.

## Why it matters

Your specs already hold decisions. Rewriting them by hand loses detail and wastes time. The importer maps each tool's structure to the spec tree, keeps a link to every source file, and flags what a person must look at. What none of these tools has is a Verify command that the merge queue runs; you add that during the import.

## What an import does

- Everything goes under **one capability** (`--into`, by default the tool's name).
- Every record is a **draft**: nothing runs until you approve it.
- Each record says **`Imported from <file>`**. Your source files are never changed or deleted.
- A statement becomes EARS when the rewrite is mechanical (`System MUST x` becomes `The system shall x.`). Anything else is kept and flagged `non-ears`.
- A task item that names a file becomes a task with those `paths`. Items without a file go into the requirement's Notes.
- The report lists the flags and one `add a Verify command` warning per requirement.

| Tool | Files it reads | Becomes |
|---|---|---|
| Spec Kit | `.specify/memory/constitution.md`, `specs/NNN-feature/spec.md`, `plan.md`, `tasks.md` | a decision (the constitution), a feature per folder, requirements `us-N` (user stories) and `fr-NNN`, tasks `tNNN` |
| Kiro | `.kiro/steering/*.md`, `.kiro/specs/<feature>/requirements.md`, `design.md`, `tasks.md` | the capability's Design (steering), a feature per spec, one requirement per acceptance criterion (`rN-M`, or per story with `--granularity story`), tasks `tN-M` in file order |
| OpenSpec | `openspec/project.md`, `openspec/specs/<c>/spec.md`, `openspec/changes/<change>/` | a feature per capability, requirements with their scenarios, a requirement per change with its tasks; deltas applied |

The full mapping and the gaps of each tool are in [import.md](../import.md).

## Steps

The demo is `recipes`, which uses Spec Kit: a constitution and one feature, `specs/001-scale-recipe/`.

### 1. Run the import skill

```
/wavecoord:spec-import speckit --into recipes
```

What you see: the skill finds the Spec Kit files, then stops on the **folder clash**. Spec Kit keeps its features in `specs/`, and `specs/` is also wavecoord's default spec folder. The import never writes into a folder that holds its source.

### 2. Settle the clash

Two ways, each in a commit of your own:

- Point wavecoord at another folder: add `"spec": {"dir": "docs/specs"}` to `.claude/wavecoord.json` on the default branch, then import with `--dir docs/specs`. Spec Kit keeps working as before.
- Or move the Spec Kit folder and pass `--src`. The video does this:

```
! git mv specs speckit && git commit -qm 'Move the Spec Kit files to speckit/' && git push -q
```

Kiro (`.kiro/`) and OpenSpec (`openspec/`) have no clash.

### 3. Import, with your answers

The skill runs `wavecoord spec import --dry-run --json` and shows the mapping in one table: features, requirements (with their statement and flags) and tasks. It asks what changes the result. The video answers in one message:

```
The Spec Kit files are now in speckit/. Import them with --src speckit into the capability recipes.
Import both user stories. Rewrite each flagged statement in EARS form. Verify: python3 -m unittest
tests.test_scale for us-1, fr-001 and fr-002; python3 -m unittest tests.test_rounding for us-2 and fr-003.
Keep the records draft for now.
```

What you see: one plan PR with the records, all `draft`.

### 4. Review the draft

```
! gh pr diff 1 | grep -E '^\+(title|Imported from|The system|When )' | head -24
```

Check:

- each user story is now an EARS statement you agree with (they were flagged `non-ears`);
- the constitution became a decision record (`recipes/decisions/<date>-constitution`);
- tasks that wrote the same file got `conflicts_with` (flag `conflict`): add a `needs` if the order matters;
- each requirement has a Verify that fails today.

Merge it: drafts are safe on main, because they never run.

### 5. Approve in a second plan PR

```
Open a plan PR that sets every requirement under recipes/scale-recipe to approved and the capability
recipes and the feature recipes/scale-recipe to active.
```

Merge it, then `! wavecoord spec tree`. From here on it is the usual loop: `/wavecoord:wave-plan recipes` sizes the imported tasks.

### Kiro, OpenSpec and free-form documents

```
/wavecoord:spec-import kiro
/wavecoord:spec-import openspec
/wavecoord:spec-import docs/prd.md
```

For Kiro, `--granularity story` gives one requirement per user story instead of one per acceptance criterion. Kiro writes no task dependencies, so the import chains tasks in file order: loosen the `needs` where tasks can run together. A PRD, ADRs or a README section has no fixed format: the skill reads it and proposes the tree for you to confirm. In a terminal, `wavecoord spec import --from kiro --dry-run` prints the diff and the report without changing anything.

## Common mistakes

- **Approving in the import PR without reading the rewrites.** A mechanical EARS rewrite can still change meaning.
- **Leaving Verify empty.** `spec check` allows it for drafts, but an approved requirement without Verify is refused.
- **Importing everything at once.** Import the features you will build next; leave the rest for later imports.
- **Deleting the source files right away.** Keep them until the tree replaces them for your team.

## Exercise

Build the Kiro and OpenSpec demos (`python3 scripts/course/demos/demos.py kiro /tmp/wavecoord-demo/habits --port 18741`, and `openspec /tmp/wavecoord-demo/glossary`):

1. Run `wavecoord spec import --from kiro --dry-run` and read the report. How many requirements did the two user stories give?
2. Run it again with `--granularity story`. Which do you prefer, and why?
3. Import the OpenSpec demo with `/wavecoord:spec-import openspec`. Rewrite the flagged proposal statement in EARS form and add Verify commands.

Next: [Lesson 6: Writing good specs and tasks](06-good-specs.md).
