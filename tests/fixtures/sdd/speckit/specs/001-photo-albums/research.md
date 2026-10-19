# Research: Photo Albums

## Decision: store the album order as an integer position

**Rationale**: A position column is simple to update in one transaction.

**Alternatives considered**: a linked list of album ids; harder to repair after a crash.
