# Rollback

Everything wavecoord knows is in git, so every change can be undone with git.

| What | Where | How to roll back |
|---|---|---|
| The plan | task specs on the default branch | `git revert` the plan PR |
| Coordination state | `coord/log` events | `wavecoord revert <seq>`, or `wavecoord rebuild --at <sha>` |
| Worker code | attempt branches and PRs | close the PR, or `git revert` the merge |
| The plugin version | the consumer's pin in `.claude/settings.json` | move the pin back (`git revert`); the launcher syncs that version to hosts |

## Revert one event

`wavecoord revert <seq> --reason "<text>"` (lead role) appends a `revert` event with `payload.target_seq`. The fold skips the target's effect from then on. Only these kinds can be reverted: `message.new`, `message.ack`, `notify.post`, `proposal.new`, `dispatch.request` and `conflict.open`. Ids are never reused after a revert; a lease or reservation is undone by its own transition (release), not by revert, because fencing epochs must only increase. The server refuses a revert of a non-revertible kind with 409 `conflict`.

## Rebuild the cache

`wavecoord rebuild` stops writes, deletes the sqlite cache and replays `coord/log` from the newest snapshot. It changes no state; use it after a crash or a cache bug.

`wavecoord rebuild --at <sha>` rolls state back to a `coord/log` commit. It appends one `revert` event with `payload.to_seq` (the last seq in that commit). From then on the fold is: the state after `to_seq`, then the events after the `revert` event. The events in between stay on the branch as history. These exceptions keep fencing safe: `last_epoch` per task keeps its maximum seen value; any lease granted after `to_seq` is released with reason `rollback`, so its holder sees it in `lost` and stops; and a lease released after `to_seq` stays released (its holder was already told to stop). Id counters, agent host bindings and used `idem` keys are also kept.

A status commit already wrote `done` into the spec tree for a merge that you roll back this way (or with a `git revert` of the merge). Run `wavecoord spec status-sync --reset <task>` to set it back to `planned` (`specs.md`, Status).

## Invariant tested in CI

For random sequences of operations: `rebuild --at <sha>` gives the same state as folding the log prefix up to `<sha>` (apart from the two fencing exceptions), and replay of the full log gives the same state as the live cache.
