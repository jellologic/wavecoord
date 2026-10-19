# Implementation Plan

- [x] 1. Set up the reminder store
  - Create the reminders table in src/todo/reminder_store.py
  - _Requirements: 1.1_

- [ ] 2. Implement reminder scheduling
- [ ] 2.1 Validate and store reminder times
  - Add `ReminderStore.add` in src/todo/reminder_store.py
  - Reject times in the past
  - _Requirements: 1.1, 1.3_
- [ ] 2.2 Send due reminders
  - Write the scheduler loop in src/todo/scheduler.py
  - _Requirements: 1.2_
- [ ]* 2.3 Write unit tests for the scheduler
  - Use a fake clock in tests/test_scheduler.py
  - _Requirements: 1.2_

- [ ] 3. Implement snoozing
  - Add snooze to src/todo/notifier.py
  - _Requirements: 2.1_

- [ ] 4. Review the notification wording with the team
