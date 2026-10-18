# Rollback

Everything wavecoord knows is in git, so every change can be undone with git.

| What | Where | How to roll back |
|---|---|---|
| The plan | task specs on the default branch | `git revert` the plan PR |
| Coordination state | `coord/log` events | `wavecoord revert <seq>`, or `wavecoord rebuild --at <sha>` |
| Worker code | attempt branches and PRs | close the PR, or `git revert` the merge |
| The plugin version | the consumer's pin in `.claude/settings.json` | move the pin back (`git revert`); the launcher syncs that version to hosts |

## Revert one event

`wavecoord revert <seq> --reason "<text>"` (lead role) appends a `revert` event with `payload.target_seq`. The fold skips the target's effect from then on. Only some kinds can be reverted (for example `message.new`, `proposal.new`, `dispatch.request`, `conflict.open`); a lease or reservation is undone by its own transition (release), not by revert, because fencing epochs must only increase. The server refuses a revert of a non-revertible kind with 409 `conflict`.

## Rebuild the cache

`wavecoord rebuild` stops writes, deletes the sqlite cache and replays `coord/log` from the newest snapshot. It changes no state; use it after a crash or a cache bug.

`wavecoord rebuild --at <sha>` rolls state back to a `coord/log` commit. It appends one `revert` event with `payload.to_seq` (the last seq in that commit). From then on the fold is: the state after `to_seq`, then the events after the `revert` event. The events in between stay on the branch as history. Two exceptions keep fencing safe: `last_epoch` per task keeps its maximum seen value, and any lease granted after `to_seq` is released with reason `rollback`, so its holder sees it in `lost` and stops.

## Invariant tested in CI

For random sequences of operations: `rebuild --at <sha>` gives the same state as folding the log prefix up to `<sha>` (apart from the two fencing exceptions), and replay of the full log gives the same state as the live cache.
