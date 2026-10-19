# Implementation Plan: Photo Albums

**Branch**: `001-photo-albums` | **Date**: 2025-06-14 | **Spec**: [spec.md](spec.md)

## Summary

Albums are rows in a local SQLite database; the web page renders them with plain HTML and a small drag-and-drop script.

## Technical Context

**Language/Version**: Python 3.11
**Primary Dependencies**: none beyond the standard library
**Storage**: SQLite
**Testing**: unittest

## Project Structure

```text
src/
  albums/
    models.py
    store.py
    service.py
  web/
    reorder.js
tests/
  test_albums.py
```
