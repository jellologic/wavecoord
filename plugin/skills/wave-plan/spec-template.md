# Task spec template

One file per task, `<plan dir>/<id>.md`. The frontmatter is a small YAML subset: scalars, flow lists `[a, b]` and one nested map `exec`. No block lists, no multi-line strings, no anchors.

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
  provider: default
  provider_fallback: [backup]
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
Why the task exists. What exists now (files, functions, interfaces) and what
the worker must not change. Links to the design note when there is one.

## Risks
- risk: The model edits the session store instead of using its interface
  mitigation: session store paths are not in `paths`; the fence denies them
  kind: llm
- risk: The full test suite takes 15 minutes
  mitigation: acceptance runs only tests/test_rotate.py; CI runs the rest
  kind: technical

## Acceptance
- `python -m unittest tests.test_rotate`
- `python -m unittest discover -s tests`

## Prompt
Imperative and self-contained. The goal in one sentence. The files to change
and the files not to touch. The interface to keep. The tests to write first.
The acceptance commands. When to stop and report `needs_decision` or propose a
split (`split_if`). "Commit and push your branch before you end."

## Notes
For the lead only; not sent to the worker.
```

## Field rules

| Field | Rule |
|---|---|
| `id` | Equal to the file name. Short, kebab-case, stable: other specs name it in `needs` |
| `phase` | The wave or milestone label from the goal |
| `needs` | Only real order dependencies (an interface, a schema, a file another task creates). Each extra edge removes parallelism |
| `paths` | The globs the task may change, exclusive. As narrow as the work allows. No `..`, no leading `/`, no braces, no spaces |
| `shared_paths` | Files that several tasks touch a little (index, changelog). Shared reservations do not block each other |
| `class` | `A` design first (deletes data, security boundary, concurrency, irreversible); `B` normal code; `C` docs, config, trivial |
| `size` | `XS` < 30 min, `S` < 1 h, `M` < 2 h, `L` < 4 h of worker time. No `XL`: split it |
| `state` | `todo` for a new task |
| `exec.*` | See exec-plan.md |
