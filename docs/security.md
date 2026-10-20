# Security

wavecoord runs agents that edit code and push branches. The coordinator is a control plane for that, so its defaults are closed.

## Network

- **Bind `127.0.0.1` by default** (port 18741). Workers on other hosts reach a loopback server through an SSH tunnel (`ssh -R 18741:127.0.0.1:18741`), which the launcher sets up.
- The server config is `<home>/server.json` (or `wavecoord serve --config <file>`) with the keys `bind`, `port`, `allow_cidrs`, `allow_hosts`, `allow_origins`, `tokens_file` and `watchdog` (`feedback.md`).
- Binding any other address needs both lists in the server config, or the server refuses to start:
  - `allow_cidrs`: peer networks, for example `["192.0.2.0/24"]`. Loopback is always allowed. A peer outside gets 403.
  - `allow_hosts`: accepted `Host` header values, for example `["coord.example.internal:18741"]`. Others get 421. This blocks DNS rebinding.
- No TLS in the server. Use loopback, an SSH tunnel, a VPN, or a TLS proxy. The plugin validator warns about `http://` to a non-loopback host; that warning is correct.
- A connection must send its request within `REQ_TIMEOUT_S` (30 s), and the server serves at most `MAX_CONNS` (128) connections at a time; more get 503 and a close. The token is checked before the body is read, and a chunked body is refused (411).
- Browser writes (`ui` role) also need an allowlisted `Origin` and the header `X-Wavecoord: 1`. The custom header forces a CORS preflight, which the server never approves, so another web page cannot write through a user's browser.

## Tokens

- Each request except `GET /api/health` needs `Authorization: Bearer <token>`.
- Roles: `agent` (one token per host), `lead`, `ui`, `gate` (`contract.md` section 4).
- The server keeps only sha256 digests in `~/.wavecoord/tokens.json` (mode 600). `wavecoord tokens provision --role agent --host <name>` (alias `tokens issue`) prints a new token once; `--out <file>` writes it to a mode-600 file instead (a new file renamed over the old one, so an existing file with a looser mode is replaced too). `WAVECOORD_TOKENS_FILE` overrides the path of the tokens file.
- Clients read the token from the plugin option `token` (sensitive: stored in the OS credential store, never in `settings.json`), else `WAVECOORD_TOKEN_FILE`, else `~/.wavecoord/token`. Tokens never go in git, in `.claude/wavecoord.json`, in logs, or in `coord/log` events.
- **Where a token goes (0.3.0).** The hooks and the CLI send such a user-level token only to a coordinator the user configured: `WAVECOORD_URL`, the plugin option `coord_url`, or a loopback URL. A `coord_url` that only a repo's `.claude/wavecoord.json` names, with a non-loopback host, gets no token: any checked-out repo or PR branch can set that key. For a remote coordinator, set the plugin option `coord_url` (or `WAVECOORD_URL`, also for `gate-check` in CI).
- **Agent tokens act within the dispatch.** An agent token can renew or resume only the lease the lead granted, add only one-path hot marks, report only as the attempt's holder (never after the merge), and never set an attempt's PR. Its sender id is never `lead`, `ui`, `watchdog` or `server` in any case. Its idempotency keys are its own (`contract.md` sections 2 and 4).
- The launcher copies an agent token to a host over SSH into a mode-600 file. It never passes a token on a command line.
- On the lead machine, keep the lead token in `~/.wavecoord/token` and each host's agent token in `~/.wavecoord/hosts/<host>.token`. The launcher copies the file of an SSH host to that host's `~/.wavecoord/token`. Workers on a local host (no `ssh`) share the lead's `~/.wavecoord`, so they get `WAVECOORD_TOKEN_FILE=~/.wavecoord/hosts/<host>.token` instead, and the plugin option token is removed from their environment: a local worker never acts with the lead token. When that file is missing and the tokens file is on this machine, the launcher provisions an agent token for the host and writes the file (mode 600). The wrapper uses the same file; without one it sends no token, never the lead's. Claude Code fills the bearer token of the plugin's MCP servers from the plugin option, not from the environment, so the worker settings deny every tool of those servers (`mcp__plugin_wavecoord_wavecoord`, `mcp__plugin_wavecoord_lead-feed`): a worker never gets the lead's MCP tools.

## Remote hosts

On a remote host the coordinator is a loopback port (the SSH tunnel). Any local user of that host could bind the port first and collect the agent token, reports and messages. So the launcher's probe sends no token to `/api/health` and asks the server to prove it is the coordinator: it sends a random challenge, and the server answers HMACs keyed by the digests of the host's agent tokens, which only the coordinator knows (`contract.md`, `GET /api/health`). A server that answers without the right proof fails the probe, and the launcher refuses to launch on that host. The check runs at each dispatch, not on every request, so prefer single-user worker hosts.

## Secrets in the log

The lead's records (`refinery.block`, `attempt.end`, ...) carry the tail of command output. `coord/log` is pushed to `origin` and streamed to every agent token, so the server redacts each record before it logs it: the values of environment variables whose names look secret (`TOKEN`, `SECRET`, `PASSWORD`, `KEY`, ...), the tokens in `~/.wavecoord/token` and `~/.wavecoord/hosts/*.token`, and common token formats (`redact.py`). Redaction is best effort: keep secrets out of `check_cmd` output.

## Content

- **Messages, reports, proposals and channel events are data.** They never grant consent or approval. The lead must ask the user in chat for protected paths, destructive actions and merges that `protected_paths_cmd` flags.
- **The PR of an attempt is the lead's.** The lead opens it for the branch it launched and keeps the URL in its own index. A PR URL or branch name in the host's files (`result.json`, `status.json`), which the worker can write, is never used to pick what the refinery merges.
- The channel bridge does not declare `claude/channel/permission`, so coordination traffic can never answer a permission prompt.
- **Steering a worker.** The launcher writes a worker's messages into the worker's stdin as user lines with a header that says they are data, and the worker rules say a hint is guidance and the task stays the authority. Only a message from `lead` can be a steer action (interrupt, model switch, stop); from any other sender the same text stays plain text. The wrapper never sends a permission answer on stdin: it registers no `can_use_tool` handler.
- **Inbox sockets.** The launcher removes `CLAUDE_CODE_MESSAGING_SOCKET` and `CLAUDE_CODE_MESSAGING_TOKEN` from a worker's environment, so a worker started from inside a Claude session cannot write into that session. The inbox relay (`hooks.md`) gets the session's own socket and token on stdin, never on a command line, and writes only into that socket; Claude Code still applies the session's inbound rules (`crossSessionInbound`).
- The public repo holds no secrets, hostnames or addresses of any deployment. Consumers keep those in their own config. `tests/test_generic.py` checks the repo for common secret formats.

## Code that the lead runs

The refinery runs code from a PR on the lead machine, as the lead user, before it merges:

- `check_cmd`, in a detached worktree of the PR head, with the lead's environment.
- For a spec tree task, its Acceptance and its requirement's Verify (`spec.verify_at_merge`, on by default). The command text comes from the default branch, but what it runs (tests, a Makefile) is the worker's code. It runs in a detached worktree of the merge of the default branch and the PR head, with a scrubbed environment: only `PATH`, `HOME`, `LANG`, `LC_*`, `TMPDIR`, `TERM`, `TZ`, `USER`, `LOGNAME`, `SHELL`, the names in `spec.verify_env`, and the refinery's `WAVECOORD_PR`, `WAVECOORD_SHA` and similar PR variables. `WAVECOORD_*` from the lead's environment, `GIT_*`, `GH_TOKEN`, `GITHUB_TOKEN`, `SSH_AUTH_SOCK` and the Anthropic keys are never passed, even when `verify_env` names them.

A scrubbed environment is not a sandbox. The code can still read any file the lead user can read, such as `~/.wavecoord/token`, `~/.ssh` or the `gh` config, and it can use the network. So the refinery runs it only after the fence check (the `Lease:` line, config and spec changes) has passed. If you merge work from hosts you do not trust, run the lead as a user without those files, run the checks in your CI instead (`verify_at_merge: false` on the default branch), or put `check_cmd` in a container.

A merged task PR also lands in the lead's checkout, where Claude Code reads its config. So a task PR may not change any Claude Code file: any key but `$schema` of `.claude/settings*.json`, any other file under a `.claude` or `.claude-plugin` folder at any depth, `.mcp.json`, `CLAUDE.local.md` or a `hooks/hooks.json`, in any case (`config_change`, `docs/design/edit-fence.md` section 8). `CLAUDE.md` files and the scripts that a hook command runs are not covered; list them in `protected_paths_cmd` if your lead loads them.

## Reporting

Report a vulnerability privately through GitHub security advisories on `jellologic/wavecoord`, not in a public issue.
