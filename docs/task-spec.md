# Task specs

With the built-in plan adapter, each task is one Markdown file `<plan.dir>/<id>.md` on the default branch. The parsed form is `schemas/task-spec.schema.json`; examples are in `tests/fixtures/task-spec/`.

## File format

```markdown
---
id: api-auth
title: Token rotation
phase: p1
needs: [session-store]
paths: [src/auth/**, tests/test_rotate.py]
shared_paths: [docs/api.md]
class: B
size: M
state: todo
exec:
  model: opus
  fallback_model: sonnet
  effort: high
  max_turns: 80
  budget_usd: 5
  timeout_min: 60
  host_needs: [python3]
  split_if: the change needs a storage schema migration
  autocompact_window: 160000
---

## Context
Why the task exists and what the worker must know first.

## Risks
- risk: The model edits the session store instead of using its interface
  mitigation: session store paths are not in `paths`; the fence denies them
  kind: llm

## Acceptance
- `python -m unittest tests.test_rotate`
- `python -m unittest discover -s tests`

## Prompt
The worker prompt. Imperative, self-contained.
```

The frontmatter is a small YAML subset (scalars, flow lists `[a, b]`, one nested map `exec`). The parser (`src/wavecoord/plan/spec.py`) is stdlib-only and rejects anything outside the subset: block lists, multi-line values, tabs, nested lists, duplicate keys, `null`. A scalar is an integer, a decimal, `true`/`false`, `'single'` or `"double"` quoted text, or bare text. Quote a value that starts with `*`, `&`, `!`, `[`, `{`, `|`, `>`, `%`, `@` or a backquote (for example a glob `'**/*.md'`). A `## ` heading that is not one of the sections below is an error, and so is text before the first section (a `# ` title line is allowed). `## Risks` may be empty. An item of `## Acceptance` in backquotes is unquoted. `wavecoord plan validate` checks a plan offline.

## Fields

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`, equal to the file name |
| `title` | no | One line |
| `phase` | yes | Grouping label (milestone, wave) |
| `needs` | yes | Task ids that must be `done` first. Can be empty |
| `paths` | yes | Globs the task may change; reserved `exclusive` |
| `shared_paths` | no | Globs the task may touch but others may too (indexes, changelogs); reserved `shared` |
| `class` | yes | Risk class. `A`: design first (deletes data, security boundary, concurrency, irreversible). `B`: normal code. `C`: docs, config, trivial |
| `size` | yes | `XS` `S` `M` `L` `XL` |
| `state` | yes | `todo` `active` `review` `blocked` `done` `dropped`. "Ready" is computed, never stored |
| `exec.model` | yes | Model alias or id for `claude -p --model` |
| `exec.fallback_model` | no | `--fallback-model` |
| `exec.effort` | yes | `low` `medium` `high` `xhigh` `max` |
| `exec.max_turns` | yes | Turn cap |
| `exec.budget_usd` | yes | `--max-budget-usd`, > 0. Not passed for a provider with `cost_basis: none` (`providers.md`) |
| `exec.max_tokens` | no | Token budget of one run: input, output and cache-write tokens of the main conversation (cache reads are not counted). The launcher interrupts the run when it is used up (`reason: token_budget`) |
| `exec.timeout_min` | yes | Wall clock limit; the launcher kills the attempt after it |
| `exec.host_needs` | no | Capabilities a host must have (matched against host names and labels) |
| `exec.split_if` | no | When the worker should stop and propose a split |
| `exec.provider` | no | Provider profile for the worker (`providers.md`); default `default_provider` |
| `exec.provider_fallback` | no | Profiles to use next, in order, after a rate limit or overload |
| `exec.autocompact_window` | no | Token count at which the worker compacts |

## Body sections

| Heading | Required | Parsed as |
|---|---|---|
| `## Context` | yes | `sections.context` (text) |
| `## Risks` | yes | `sections.risks`: list of `{risk, mitigation, kind?}`; `kind` is `llm`, `technical`, `process` or `external`. Plan the LLM risks too: scope creep, edits outside `paths`, fake test results, context exhaustion |
| `## Acceptance` | yes | `sections.acceptance`: one shell command per item, at least one. The worker runs them and lists them in `checks_run` |
| `## Prompt` | yes | `sections.prompt` (text) |
| `## Notes` | no | `sections.notes` (text, not sent to the worker) |

## Ready

The lead tools use the **effective state**: a `done` or `dropped` spec state always wins; otherwise a task with a merged attempt (`refinery.merge`) is `done`, a task with an `active` or `stale` lease or an attempt in flight is `active`, and a task with a `parked` lease is `review`. So a merge makes the dependents ready before the spec file says `done`.

A task is ready when its effective `state` is `todo`, every task in `needs` is `done`, and none of its `paths` overlaps an exclusive reservation of a non-released lease (or its own exclusive `paths` overlaps any reservation). `dispatch_wave` picks ready tasks in plan order, skipping any that overlap a task already picked in the same wave, up to `wip_cap`.
