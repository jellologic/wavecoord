# Contributing

## Flow

1. Branch from `main`: `p<N>/<short-name>` for milestone work, `fix/<short-name>` for fixes.
2. Make the change with its tests. Change a schema, its fixtures and `docs/contract.md` in the same PR.
3. If you changed anything in `src/wavecoord/`, rebuild the zipapp and commit it:
   ```bash
   python3 scripts/build-zipapp.py
   ```
   Plugins install from git, so `plugin/bin/wavecoord` is committed. CI fails if it is stale.
4. Run the checks locally:
   ```bash
   python3 -m unittest discover -s tests
   python3 scripts/build-zipapp.py --check
   claude plugin validate plugin --strict
   claude plugin validate . --strict
   ```
   The suite includes the localhost end-to-end test `tests/e2e/test_wave.py` (about a minute): a real server, MCP calls as Claude makes them, and the fake `claude` steered per task by `FAKE_CLAUDE_MODES` (see `tests/fake-claude/claude`). Run it alone with `cd tests && python3 -m unittest e2e.test_wave`.
   A change to how the launcher talks to `claude` (stdin, the stream, hooks, the inbox socket) also gets the live smoke test, a manual release check that calls a real model (a few cents on haiku, about 5 minutes):
   ```bash
   python3 scripts/smoke-live.py                     # this host
   python3 scripts/smoke-live.py --ssh build-1=build-1.lan:wc-smoke --origin <a remote both can push>
   ```
   It prints a table to paste into the PR, with the Claude Code version of each host.
5. Open a PR. CI (`.github/workflows/ci.yml`) runs the tests and the zipapp check on Ubuntu and macOS with Python 3.9 and 3.12. A PR merges after review and green CI.

## Rules

- **Stdlib only**, Python >= 3.9. No runtime dependencies; the plugin must run on any host with `python3`.
- **Generic and public.** No secrets, no hostnames or addresses of a real deployment, no project-specific names. `tests/test_generic.py` checks common cases.
- **Hooks never break a session.** A hook exits 0 with no output unless it has a reason to act. The fence (P6) is the only deliberate exception, and it is built design first: a design note with the invariant as a checkable predicate, a randomized invariant test and seeded defects.
- **Tests do not call a model.** Use `tests/fake-claude/claude`.
- Keep docs short and plain.

## Versioning

`plugin/.claude-plugin/plugin.json` `version` equals `wavecoord.__version__` (a test checks it). Users stay on a version until it changes, so bump it in every release PR.
