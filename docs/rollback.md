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

`wavecoord rebuild --at <sha>` rolls state back to a `coord/log` commit. It appends one `revert` event with `payload.to_seq` (the last seq in that commit). From then on the fold is: the state after `to_seq`, then the events after the `revert` event. The events in between stay on the branch as history. These exceptions keep fencing safe: `last_epoch` per task keeps its maximum seen value; any lease granted after `to_seq` is released with reason `rollback`, so its holder sees it in `lost` and stops; and a lease released after `to_seq` stays released (its holder was already told to stop). Id counters, agent host bindings and used `idem` keys are also kept. A revert marker is kept only for an event after `to_seq`: an event that `revert` undid before is back with the restored state, and can be reverted again (0.2.1).

A status commit already wrote `done` into the spec tree for a merge that you roll back this way (or with a `git revert` of the merge). Run `wavecoord spec status-sync --reset <task>` to set it back to `planned` (`specs.md`, Status).

## A forked log

Only one server may write `coord/log`. When two servers wrote it from two clones of the same `origin` (a second `wavecoord serve` on another machine), the branch forks: neither head contains the other. The server checks the remote branch on start and after a rejected push. On a fork it refuses to start, or stops taking writes (503, `GET /api/health` says `ok: false`), and prints both heads. Stop one server. Keep the history of the other (`origin` or local), move the stopped server's `~/.wavecoord/<project>/log` aside, and re-send what only it logged (messages, reports) through the server that stays. Leases and epochs come back from the kept history.

A failed local commit (a stale `index.lock`, a full disk) is not a fork: the server retries it with a backoff and takes writes again when it works.

## Invariant tested in CI

For random sequences of operations: `rebuild --at <sha>` gives the same state as folding the log prefix up to `<sha>` (apart from the two fencing exceptions), and replay of the full log gives the same state as the live cache.
