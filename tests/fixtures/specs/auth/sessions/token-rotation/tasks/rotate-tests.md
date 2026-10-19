---
status: planned
kind: task
schema: 1
title: Tests for token rotation
basis: a6f4586d
needs:
  - auth/sessions/token-rotation/rotate-impl
inputs:
  - id: auth.rotation.service
    kind: interface
    from: auth/sessions/token-rotation/rotate-impl
outputs:
  - id: tests/test_rotate.py
    kind: file
paths:
  - tests/test_rotate.py
class: C
size: S
exec:
  model: sonnet
  effort: medium
  max_turns: 30
  budget_usd: 1
  timeout_min: 20
---

## Context
The rotation service has no tests.

## Acceptance
- `python -m unittest tests.test_rotate`

## Prompt
Write tests for every scenario of the token rotation requirement.
