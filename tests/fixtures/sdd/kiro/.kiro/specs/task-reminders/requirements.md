# Requirements Document

## Introduction

Reminders tell a user about a task before it is due.
A user picks when the reminder fires, and can snooze it.

## Requirements

### Requirement 1

**User Story:** As a user, I want to set a reminder on a task, so that I do not miss its due date.

#### Acceptance Criteria

1. WHEN a user sets a reminder time on a task THEN the system SHALL store the reminder with the task
2. WHEN the reminder time is reached THEN the system SHALL send a notification to the task owner
3. IF the reminder time is in the past THEN the system SHALL reject the reminder with a validation error

### Requirement 2: Snooze

**User Story:** As a user, I want to snooze a reminder, so that I am reminded again later.

#### Acceptance Criteria

1. WHEN a user snoozes a notification THEN the system SHALL send it again after 10 minutes
2. WHILE a task is marked done THE SYSTEM SHALL NOT send its reminders
3. The system should make snoozing feel quick
