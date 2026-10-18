---
id: session-store
title: Session store interface
phase: p1
needs: []
paths: [src/session/**]
class: B
size: L
state: done
exec:
  model: sonnet
  effort: medium
  max_turns: 40
  budget_usd: 2
  timeout_min: 30
---

## Context
The session store needs one interface for token persistence.

## Risks

## Acceptance
- `python -m unittest tests.test_session`

## Prompt
Add the session store interface in src/session.
