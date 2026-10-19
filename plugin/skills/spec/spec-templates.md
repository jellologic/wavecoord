# Spec tree records

Each record is one file. The tools write the canonical form; this page shows it.

## Capability or feature (`<cap>/_node.md`, `<cap>/<feature>/_node.md`)

```markdown
---
status: draft
kind: feature
schema: 1
title: Sessions
---

## Intent
A session survives a restart and ends on logout.

## Non-goals
- Single sign-on with third parties.

## Design
One sentence per line.
```

## Requirement (`<cap>/<feature>/<req>/_node.md`)

```markdown
---
status: draft
kind: requirement
schema: 1
title: Token rotation on expiry
needs:
  - auth/sessions/session-store
inputs:
  - id: auth.session-store.api
    kind: interface
    from: auth/sessions/session-store
outputs:
  - id: auth.rotation.service
    kind: interface
    contract: rotate(refresh_token) -> TokenPair; the old refresh token is revoked
---

## Statement
When an access token is within 60 s of expiry, the auth service shall issue a new token and revoke the old one.

## Scenarios
### Near-expiry token is refreshed
- Given a session whose token expires in 30 s
- When the client calls refresh
- Then a new token pair is returned

## Verify
- `python -m unittest tests.test_rotate`

## Rationale
Short-lived tokens limit the damage of a leaked token.
```

## Decision (`<node>/decisions/<YYYY-MM-DD>-<slug>.md`)

```markdown
---
status: proposed
kind: decision
schema: 1
title: Use rotating refresh tokens
affects:
  - auth/sessions/token-rotation
---

## Context
## Decision
## Consequences
```

## As ops for `spec_edit`

```json
{"op": "create", "id": "auth/sessions/token-rotation",
 "fields": {"title": "Token rotation on expiry", "needs": ["auth/sessions/session-store"],
            "inputs": [{"id": "auth.session-store.api", "kind": "interface", "from": "auth/sessions/session-store"}],
            "outputs": [{"id": "auth.rotation.service", "kind": "interface",
                         "contract": "rotate(refresh_token) -> TokenPair; the old refresh token is revoked"}]},
 "sections": {"Statement": "When an access token is within 60 s of expiry, the auth service shall issue a new token and revoke the old one.",
              "Scenarios": [{"name": "Near-expiry token is refreshed",
                             "steps": [["Given", "a session whose token expires in 30 s"], ["When", "the client calls refresh"],
                                       ["Then", "a new token pair is returned"]]}],
              "Verify": ["python -m unittest tests.test_rotate"]}}
```

## Checklist

- [ ] Every requirement has one EARS statement, at least one scenario and a Verify command that fails without the work.
- [ ] Every interface, api, schema or event output has a contract.
- [ ] Every input has `from` (and that node is in `needs`) or `external: true` with a contract.
- [ ] No counts, summaries, "done" lists or back links in the files: the tools compute them.
- [ ] Names are lowercase; a task id with `.` for `/` stays within 64 characters.
