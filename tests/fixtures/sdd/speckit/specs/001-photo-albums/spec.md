# Feature Specification: Photo Albums

**Feature Branch**: `001-photo-albums`

**Created**: 2025-06-14

**Status**: Draft

**Input**: User description: "Organize photos in separate albums grouped by date, and reorder albums by drag and drop."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Create an album (Priority: P1)

A user groups photos into a new album so related photos stay together.

**Why this priority**: Albums are the core of the product; nothing else works without them.

**Independent Test**: Can be fully tested by creating an album with two photos and listing it.

**Acceptance Scenarios**:

1. **Given** a library with three photos, **When** the user creates an album from two of them, **Then** the album lists those two photos
2. **Given** an album named "Trips", **When** the user creates another album named "Trips", **Then** the second album is refused with a duplicate-name error

---

### User Story 2 - Reorder albums (Priority: P2)

A user drags an album to a new position on the main page.

**Why this priority**: Users with many albums need to put the important ones first.

**Independent Test**: Can be tested by moving the third album to the first position and reloading.

**Acceptance Scenarios**:

1. **Given** albums A, B and C in that order, **When** the user drags C before A, **Then** the order is C, A, B after a reload

---

### Edge Cases

- What happens when an album is created with no photos?
- How does the system handle two devices reordering at the same time?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST allow users to create an album from selected photos.
- **FR-002**: System MUST reject an album name that is already used.
- **FR-003**: Users MUST be able to reorder albums by drag and drop.
- **FR-004**: System MUST persist the album order across sessions.
- **FR-005**: System MUST group photos by date inside an album and show the newest group first.

### Key Entities

- **Album**: a named, ordered collection of photos; has a position on the main page.
- **Photo**: an image file with a capture date; belongs to zero or more albums.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A user creates an album with ten photos in under 30 seconds.
- **SC-002**: Reordering 100 albums takes under 200 ms to save.

## Assumptions

- Photos are stored on the local disk; cloud upload is out of scope.
