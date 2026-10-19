# Requirements Document

## Introduction

Export the task list as a CSV file.

## Requirements

### Requirement 1: Export

**User Story:** As a team lead, I want to export tasks as CSV, so that I can share them in a spreadsheet.

#### Acceptance Criteria

1. WHEN the user chooses Export THEN the system SHALL download a CSV file with one row per task
2. WHERE the team has archived tasks THE SYSTEM SHALL include them only when the user asks for them
