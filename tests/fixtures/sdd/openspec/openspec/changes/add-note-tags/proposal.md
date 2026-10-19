# Proposal

## Why

Users with many notes cannot group them.
Tags let them find related notes quickly.

## What Changes

- Add tags to notes.
- Let search filter by tag.
- Drop fuzzy matching, which returned too many false hits.

## Impact

- Affected specs: notes, search
- Affected code: src/notes/, src/search/
