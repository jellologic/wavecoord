# Feature Specification: Album Sharing

**Feature Branch**: `002-album-sharing`

**Created**: 2025-06-20

**Status**: Draft

**Input**: User description: "Share an album with a read-only link."

## Clarifications

### Session 2025-06-21

- Q: Do links expire? → A: Yes, after 30 days.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Share a link (Priority: P1)

The owner of an album creates a read-only link and sends it to a friend.

**Why this priority**: Sharing is the reason for this feature.

**Independent Test**: Open the link in a private window and see the album.

**Acceptance Scenarios**:

1. **Given** an album, **When** the owner creates a link, **Then** anyone with the link sees the album without signing in
2. **Given** a link older than 30 days, **When** someone opens it, **Then** the page answers 410

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST create a read-only link for an album.
- **FR-002**: System MUST expire a link after 30 days.
- **FR-003**: System MUST notify the owner via [NEEDS CLARIFICATION: notification channel not specified - email or in-app?]
- **FR-004**: The system should keep a count of link views.

## Review Checklist

- [ ] No implementation details
