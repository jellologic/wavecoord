---
description: "Task list for Photo Albums"
---

# Tasks: Photo Albums

**Input**: Design documents from `/specs/001-photo-albums/`

**Prerequisites**: plan.md, spec.md

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2)

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and basic structure

- [X] T001 Create the project structure in src/albums/__init__.py
- [ ] T002 [P] Configure the test runner in tests/__init__.py

---

## Phase 2: Foundational (Blocking Prerequisites)

- [ ] T003 Create the SQLite schema and migrations in src/albums/store.py
- [ ] T004 [P] Create the Album and Photo models in src/albums/models.py

**Checkpoint**: Foundation ready

---

## Phase 3: User Story 1 - Create an album (Priority: P1) 🎯 MVP

**Goal**: Users create albums from photos.

### Tests for User Story 1

- [ ] T005 [P] [US1] Test album creation in tests/test_albums.py

### Implementation for User Story 1

- [ ] T006 [US1] Implement create_album in src/albums/service.py (depends on T004)
- [ ] T007 [US1] Add duplicate-name validation in src/albums/service.py
- [ ] T008 [US1] Add logging for album operations

---

## Phase 4: User Story 2 - Reorder albums (Priority: P2)

- [ ] T009 [P] [US2] Implement the drag-and-drop script in src/web/reorder.js
- [ ] T010 [P] [US2] Persist the album order in `src/albums/order.py`
- [ ] T011 [US2] Test reordering in tests/test_order.py

---

## Phase 5: Polish & Cross-Cutting Concerns

- [ ] T012 [P] Document albums in docs/albums.md
- [ ] T013 Run the quickstart.md validation

## Dependencies & Execution Order

- Setup (Phase 1) has no dependencies.
- Foundational (Phase 2) blocks all user stories.
- User stories can then run in parallel.
