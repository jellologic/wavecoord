---
id: api-docs
title: 'API docs: rotation'
phase: p1
needs: [api-auth]
paths: [docs/rotation.md]
shared_paths: [docs/api.md]
class: C
size: XS
state: todo
exec:
  model: haiku
  effort: low
  max_turns: 10
  budget_usd: 0.5
  timeout_min: 10
---

## Context
Document token rotation.

## Risks
- risk: Docs describe behavior that does not exist
  mitigation: read the merged code first
  kind: llm

## Acceptance
- `test -s docs/rotation.md`

## Prompt
Write docs/rotation.md from the merged code.

## Notes
Not sent to the worker.
