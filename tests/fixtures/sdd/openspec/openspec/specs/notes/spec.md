# notes Specification

## Purpose

Users create, edit and delete plain-text notes.

## Requirements

### Requirement: Create note

The system SHALL store a new note with a title and a body.

#### Scenario: Valid note is stored

- **WHEN** a user submits a note with a title and a body
- **THEN** the note is stored
- **AND** the response contains its id

#### Scenario: Empty title is refused

- **WHEN** a user submits a note with an empty title
- **THEN** the system answers 400

### Requirement: Delete note

WHEN a user deletes a note, the system SHALL remove it from storage and from the search index.

#### Scenario: Deleted note is gone

- **GIVEN** a stored note
- **WHEN** its owner deletes it
- **THEN** reading it answers 404

### Requirement: Note size limit

The system SHALL limit a note body to 100 KB.
Larger bodies are refused with 413.

#### Scenario: Large note is refused

- **WHEN** a user submits a body of 200 KB
- **THEN** the system answers 413
