## ADDED Requirements

### Requirement: Tag filter

WHEN a search names a tag, the system SHALL return only notes with that tag.

#### Scenario: Tag filter narrows results

- **GIVEN** two notes with "garden", one tagged "home"
- **WHEN** a user searches for "garden" with the tag "home"
- **THEN** only the tagged note is returned

## REMOVED Requirements

### Requirement: Fuzzy matching

**Reason**: Too many false hits.
**Migration**: Search for the exact word.

## RENAMED Requirements

- FROM: `### Requirement: Full-text search`
- TO: `### Requirement: Word search`
