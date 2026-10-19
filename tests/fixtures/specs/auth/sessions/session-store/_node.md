---
status: approved
kind: requirement
schema: 1
title: Session store
outputs:
  - id: auth.session-store.api
    kind: interface
    contract: get(id) -> Session | None; put(Session); revoke(id)
---

## Statement
The session store shall keep each session until it is revoked.

## Verify
- `python -m unittest tests.test_store`
