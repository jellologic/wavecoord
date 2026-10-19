## ADDED Requirements

### Requirement: Note tags

The system SHALL store up to 10 tags per note.

#### Scenario: Tags are stored

- **WHEN** a user creates a note with the tags "home" and "garden"
- **THEN** the note is stored with both tags

## MODIFIED Requirements

### Requirement: Create note

The system SHALL store a new note with a title, a body and optional tags.

#### Scenario: Valid note is stored

- **WHEN** a user submits a note with a title, a body and a tag
- **THEN** the note is stored with the tag
