---
status: active
kind: capability
schema: 1
title: Authentication
owners:
  - team-auth
tags:
  - security
---

## Intent
Users stay signed in across restarts without long-lived secrets on the device.

## Non-goals
- Single sign-on with third parties.

## Design
Access tokens live 15 minutes.
Refresh tokens rotate on every use.
