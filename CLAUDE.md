# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

wavecoord is a Claude Code plugin for plan-driven parallel agent work. A **lead** session plans tasks in git (specs with owned paths, deps and an exec plan) and dispatches them to Claude Code **workers** on local or SSH hosts. Each worker runs in its own worktree with its paths leased. The lead then opens PRs, resolves conflicts and merges, all through MCP. All state is in git: the plan is on the default branch, and every coordination event goes on an append-only `coord/log` branch.

## Commands

```bash
python3 -m unittest discover -s tests              # full suite (includes ~1 min e2e)
cd tests && python3 -m unittest test_globs         # one module (run from tests/)
cd tests && python3 -m unittest test_globs.GlobsTest.test_cases
cd tests && python3 -m unittest e2e.test_wave      # localhost end-to-end only
python3 scripts/build-zipapp.py                    # rebuild plugin/bin/wavecoord
python3 scripts/build-zipapp.py --check            # CI: fail if the zipapp is stale
claude plugin validate plugin --strict
claude plugin validate . --strict
```

Tests import `src/` through `tests/_paths.py`. There is no install step, linter config or pyproject.

Fence test knobs: `WAVECOORD_FENCE_SEED=<n>` reruns one seed, `WAVECOORD_FENCE_RUNS=<n>` adds random seeds, `WAVECOORD_FENCE_OPS=<n>` sets operations per seed (default 80, 300 for a soak run), `WAVECOORD_FENCE_DEFECT_SEEDS` sets the seeds per seeded defect, and `WAVECOORD_PERF=1` turns on the perf test. Any contract constant can be overridden with `WAVECOORD_<NAME>`, for example `WAVECOORD_LEASE_TTL_S=5` (see `const.py`).

## Hard rules (CONTRIBUTING.md)

- **Stdlib only, Python >= 3.9.** No runtime or test dependencies. CI runs Ubuntu and macOS on Python 3.9 and 3.12.
- **`plugin/bin/wavecoord` is a committed, deterministic zipapp** built from `src/wavecoord/` plus embedded `schemas/*.schema.json`. After any change in `src/` or `schemas/`, rebuild it and commit it. CI fails if it is stale. It is binary, so do not read or edit it.
- Change a schema, its fixtures (`tests/fixtures/`) and `docs/contract.md` in the same PR.
- **Hooks never break a session.** Every hook exits 0 with no output unless it has a reason to act. The one deliberate exception is the edit fence (`PreToolUse`, `onFailure: block`), which fails closed.
- **Tests never call a model.** Use the fakes: `tests/fake-claude/claude` (steered per task by `FAKE_CLAUDE_MODES`), `fake-gh`, `fake-ssh` and `fake-plan`.
- **Generic and public.** No secrets, real hostnames or project-specific names. `tests/test_generic.py` checks for these.
- The `version` in `plugin/.claude-plugin/plugin.json` must equal `wavecoord.__version__` (a test checks this). Bump both and add a `CHANGELOG.md` entry in every release PR.
- Branch names: `p<N>/<name>` for milestone work, `fix/<name>` for fixes. Keep docs short and plain, in the style of the existing pages.

## Architecture

One program, `wavecoord` (`cli.py` dispatches), plays every role: coordinator server, launcher, hook handler, git guard and lead CLI.

**Coordinator (runs on the lead machine).**
- `server.py`: REST, SSE and MCP on one port. It checks the peer address, the Host allowlist, the body size and a bearer token.
- `core.py` `Coord`: every write runs under one lock in this order: validate → build events → `log.append` → `State.apply` → `store.save`. It answers only after the group commit.
- `log.py`: the event log, a single-writer hash chain on the orphan branch.
- `store.py` `State`: a pure, deterministic fold of the log (no clock, no randomness), cached in sqlite. Rollback and `rebuild` are a replay.
- `mcp.py`: exposes the lead's tools and calls the same core operations as REST.
- `watchdog.py` nudges idle leases and marks them stale. `feedback.py` turns events into one-line lead messages for the Monitor (`events --follow --for lead`), the channel (`channel.py`) and the hooks.

**Lead tools** (`lead/`, run inside the server process):
- `base.py`: plan reads and edits, and dispatch.
- `collect.py`: PRs, conflicts and resolvers.
- `refinery.py`: one serialized merge loop. It re-checks the merge-tree, protected paths, `check_cmd` and `merge_cmd`.

`plan/` is plan-as-code: `spec.py` parses task-spec Markdown with YAML-subset frontmatter, `adapters.py` has the built-in store and the command adapter, `graph.py` handles readiness, cycles and the critical path, and `edit.py` applies anchored plan edits as a PR.

**Launcher** (`launcher/`): `client.py` does placement and launch on the lead side. `transport.py` runs `wavecoord _remote <cmd>` locally or over ssh, and uploads the hash-pinned zipapp on exit 86. `hostside.py`, `worktrees.py` and `wrapper.py` are the host side: a detached wrapper per attempt that runs `claude -p`, parses its stream (`stream.py`), heartbeats and writes the report. `providers.py` handles Anthropic-compatible provider profiles, whose env files stay on the host.

**Worker side:** `hooks.py` handles `wavecoord hook <Event>` (wired in `plugin/hooks/hooks.json`). `guard.py` is the git pre-commit and pre-push guard. `client.py` and `launcher/coord.py` are REST clients that spool writes when the server is unreachable.

**Edit fence** (P6, `docs/design/edit-fence.md`): workers may only write paths covered by their lease. The fence has several points: E1 `PreToolUse` hook, E2 pre-commit, E3 pre-push, and E5 `gate-check`/refinery at merge.
- Client: `fence.py`, `fencecli.py`, `gatecheck.py`, `launcher/fencehost.py`.
- Server predicate: `fencecore.py` `evaluate` = `ok(W)` in design section 2.2.
- Path overlap is conservative: `globs.py` may report a false overlap but must never miss a real one.
- The fence mode and the overrides come only from places the agent cannot write: the default branch's config and the launcher env. Never use the worktree copy.
- Fence changes are design-first. Tests (`tests/fence/README.md`) are a randomized invariant sim with an independent oracle (`fenceworld.py`, `fencesim.py`), seeded defects D01–D24 (`fencedefects.py`; each needs a catching test), and table tests (`test_fence_points.py`). Add a failing seed to the fixed list together with its fix.

**Plugin surface:** `plugin/skills/` (`wave-plan`, `wave-run`, `triage`, `revise`, `resolve`, `coord-status`) and `plugin/agents/` (`planner`, `reviewer`) are the lead's prompts. They drive the tools above through MCP and the CLI.

## Docs

`docs/contract.md` is the authority for the REST, SSE and MCP shapes, state machines and constants. Other pages: `task-spec.md`, `adapters.md` (consumer `.claude/wavecoord.json`), `launcher.md`, `providers.md`, `feedback.md`, `hooks.md`, `security.md` and `rollback.md`.
