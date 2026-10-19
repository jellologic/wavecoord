---
status: done
kind: task
schema: 1
title: Session store API
basis: 443d71d0
outputs:
  - id: auth.session-store.api
    kind: interface
    contract: get(id) -> Session | None; put(Session); revoke(id)
  - id: src/auth/store.py
    kind: file
  - id: tests/test_store.py
    kind: file
paths:
  - src/auth/store.py
  - tests/test_store.py
class: B
size: S
exec:
  model: sonnet
  effort: medium
  max_turns: 40
  budget_usd: 2
  timeout_min: 30
---

## Context
There is no session store yet.

## Acceptance
- `python -m unittest tests.test_store`

## Prompt
Add the session store in src/auth/store.py with tests.
