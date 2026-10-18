<!-- Template, not an agent. The resolve skill fills it in and passes it to the `resolve` tool.
     It lives here, not in agents/, because Claude Code loads every .md file in agents/ as an agent. -->

# Resolver brief: conflict <conflict id>

## Goal
Make <branch B> merge cleanly onto <default branch> after <branch A> (already merged, or merging first), and keep the intent of both tasks.

## Sides
| | Task | Attempt | Branch | Spec goal (one line) |
|---|---|---|---|---|
| A | <task a> | <n> | <branch a> | <goal a> |
| B | <task b> | <n> | <branch b> | <goal b> |

## Conflict
- Type: <merge-tree | path overlap>
- Files: <file list>
- What each side changed in those files: <one line per file and side>

## Decision
<Which behavior wins where the sides disagree. "Keep both" for mechanical overlaps (lists, imports, changelog). A behavior decision here comes from the user in the lead session; write who decided.>

## Rules
- Start from <base>: rebase or merge <branch B> onto it on your own branch `<resolver branch>`.
- Change only the conflicting files and what they need to compile and pass the checks. No refactors.
- Never drop a test from either side. Never weaken a check to make it pass.
- If the decision above does not cover a hunk, stop and report `needs_decision` with the hunk.

## Acceptance
Run all of these and list them in `checks_run`:
- <acceptance commands of task a>
- <acceptance commands of task b>

## End
Commit, push your branch, and end with the report. Do not merge.
