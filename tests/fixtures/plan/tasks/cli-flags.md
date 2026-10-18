---
id: cli-flags
title: CLI flags
phase: p1
needs: []
paths: [src/cli/**]
class: B
size: S
state: todo
exec:
  model: sonnet
  effort: medium
  max_turns: 20
  budget_usd: 1
  timeout_min: 20
---

## Context
The CLI has no --verbose flag.

## Risks

## Acceptance
- `python -m unittest tests.test_cli`

## Prompt
Add --verbose to the CLI.
