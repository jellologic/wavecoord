---
status: planned
kind: task
schema: 1
title: Rotate refresh tokens
basis: a6f4586d
needs:
  - auth/sessions/session-store/store-api
inputs:
  - id: auth.session-store.api
    kind: interface
    from: auth/sessions/session-store/store-api
outputs:
  - id: auth.rotation.service
    kind: interface
    contract: rotate(refresh_token) -> TokenPair; the old refresh token is revoked
  - id: src/auth/rotate.py
    kind: file
paths:
  - src/auth/rotate.py
shared_paths:
  - docs/api.md
class: B
size: M
exec:
  model: opus
  effort: high
  max_turns: 80
  budget_usd: 5
  timeout_min: 60
---

## Context
Refresh tokens never rotate today.
The session store owns persistence.

## Risks
- risk: The model edits the session store instead of its interface
  mitigation: session store paths are not in paths; the fence denies them
  kind: llm

## Acceptance
- `python -m unittest tests.test_rotate`

## Prompt
Implement rotation in src/auth/rotate.py through the session store interface.
