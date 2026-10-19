---
status: accepted
kind: decision
schema: 1
title: Use rotating refresh tokens
affects:
  - auth/sessions/token-rotation
---

## Context
Static refresh tokens live for weeks.

## Decision
Every refresh issues a new refresh token and revokes the old one.

## Consequences
A stolen refresh token works at most once.
