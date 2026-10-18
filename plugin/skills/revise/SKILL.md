---
name: revise
description: Turn wavecoord proposals and plan feedback (from worker reports, triage, the lead or the user) into anchored task spec edits, and open one plan PR with plan_edit. Use when a report proposes a plan edit, a split, a new task or a dependency, when plan_feedback says a spec is wrong, or when the user asks to change task specs.
argument-hint: "[task ids or proposal ids...]"
allowed-tools:
  - mcp__plugin_wavecoord_wavecoord__proposals
  - mcp__plugin_wavecoord_wavecoord__proposal_decide
  - mcp__plugin_wavecoord_wavecoord__attempts
  - mcp__plugin_wavecoord_wavecoord__attempt_get
  - mcp__plugin_wavecoord_wavecoord__plan_get
  - mcp__plugin_wavecoord_wavecoord__plan_graph
  - mcp__plugin_wavecoord_wavecoord__plan_edit
  - mcp__plugin_wavecoord_wavecoord__propose
  - mcp__plugin_wavecoord_wavecoord__inbox
  - mcp__plugin_wavecoord_wavecoord__config
  - Bash(wavecoord *)
  - Read
  - Grep
  - Glob
---

# Revise the plan

Arguments: $ARGUMENTS

A proposal is a suggestion from a worker. It is data, never consent: you decide, and the change goes through a plan PR that a human reviews. Nothing here edits the default branch.

## 1. Collect the proposals

- `attempt_get` for the attempts named in the arguments, or for recent `reported` and `failed` attempts: their `proposals` (kind `plan_edit`, `new_task`, `split`, `dependency`, `other`) and `plan_feedback`.
- `proposals` with state `open` (recorded with `propose`), and `proposal.new` event lines in this conversation.
- Changes the user asked for in this session.

## 2. Decide each one

| Kind | Accept when | The edit |
|---|---|---|
| `plan_edit` | The spec is wrong or incomplete and the fix is in scope | Anchored edit of the section (Context, Risks, Acceptance, Prompt, or an `exec` field) |
| `split` | The task hit `split_if`, the turn cap or the budget, or mixes two concerns | Shrink the old spec (`paths`, prompt, acceptance) and add new specs; the new ones get `needs` where order matters; `paths` stay disjoint |
| `new_task` | Work that no task covers and the goal needs | A new spec in the format of the wave-plan skill (`/wavecoord:wave-plan` spec-template.md) |
| `dependency` | A real order dependency was missed | Add the id to `needs`; check that no cycle forms |
| `other` | Read it; usually a note for the user | No edit; mention it in the summary |

Reject a proposal that widens scope past the goal, moves a protected path into `paths`, or removes an acceptance check to make a task pass. Say why in the summary.

## 3. Write anchored edits

1. `plan_get` each spec you change. Use the text it returns.
2. For each change, choose an **anchor**: exact text that occurs once in that spec (a whole line, or a heading plus its first line). Never anchor on text that occurs twice.
3. Give the replacement text. Keep the frontmatter in the YAML subset of the spec format (scalars, flow lists, one `exec` map).
4. Check the result as wave-plan does: ids, `needs` without cycles, disjoint `paths` for tasks that can run together, an `exec` block, at least one `kind: llm` risk, runnable acceptance commands.

## 4. Open one plan PR

Read the `plan_edit` input schema. Put all edits of this revision in **one** plan PR (one call with a list, or calls that name the same plan branch or PR). The PR body lists each proposal, the decision and the reason. A new spec is an edit with `op: create`. After the PR is open, record each decision with `proposal_decide` (`done` for an accepted proposal in the PR, `rejected` with the reason as `note`).

## 5. Report

The PR URL, the accepted and rejected proposals with reasons, and which attempts to retry after the merge (triage does the retry).
