# search Specification

## Purpose

Find notes by words in their title or body.

## Requirements

### Requirement: Full-text search

The system SHALL return the notes whose title or body contains every word of the query.

#### Scenario: Matching notes are returned

- **WHEN** a user searches for "garden plan"
- **THEN** every note that contains both words is returned

### Requirement: Fuzzy matching

The system MUST match words with one typo.

#### Scenario: Typo still matches

- **WHEN** a user searches for "gardn"
- **THEN** notes with "garden" are returned

## Ranking notes

Results are ordered by the number of matches.
