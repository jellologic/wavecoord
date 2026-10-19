# Security

wavecoord runs agents that edit code and push branches. The coordinator is a control plane for that, so its defaults are closed.

## Network

- **Bind `127.0.0.1` by default** (port 18741). Workers on other hosts reach a loopback server through an SSH tunnel (`ssh -R 18741:127.0.0.1:18741`), which the launcher sets up.
- The server config is `<home>/server.json` (or `wavecoord serve --config <file>`) with the keys `bind`, `port`, `allow_cidrs`, `allow_hosts`, `allow_origins`, `tokens_file` and `watchdog` (`feedback.md`).
- Binding any other address needs both lists in the server config, or the server refuses to start:
  - `allow_cidrs`: peer networks, for example `["192.0.2.0/24"]`. Loopback is always allowed. A peer outside gets 403.
  - `allow_hosts`: accepted `Host` header values, for example `["coord.example.internal:18741"]`. Others get 421. This blocks DNS rebinding.
- No TLS in the server. Use loopback, an SSH tunnel, a VPN, or a TLS proxy. The plugin validator warns about `http://` to a non-loopback host; that warning is correct.
- Browser writes (`ui` role) also need an allowlisted `Origin` and the header `X-Wavecoord: 1`. The custom header forces a CORS preflight, which the server never approves, so another web page cannot write through a user's browser.

## Tokens

- Each request except `GET /api/health` needs `Authorization: Bearer <token>`.
- Roles: `agent` (one token per host), `lead`, `ui`, `gate` (`contract.md` section 4).
- The server keeps only sha256 digests in `~/.wavecoord/tokens.json` (mode 600). `wavecoord tokens provision --role agent --host <name>` (alias `tokens issue`) prints a new token once; `--out <file>` writes it to a mode-600 file instead. `WAVECOORD_TOKENS_FILE` overrides the path of the tokens file.
- Clients read the token from the plugin option `token` (sensitive: stored in the OS credential store, never in `settings.json`), else `WAVECOORD_TOKEN_FILE`, else `~/.wavecoord/token`. Tokens never go in git, in `.claude/wavecoord.json`, in logs, or in `coord/log` events.
- The launcher copies an agent token to a host over SSH into a mode-600 file. It never passes a token on a command line.
- On the lead machine, keep the lead token in `~/.wavecoord/token` and each host's agent token in `~/.wavecoord/hosts/<host>.token`. The launcher copies the file of an SSH host to that host's `~/.wavecoord/token`. Workers on a local host (no `ssh`) share the lead's `~/.wavecoord`, so they get `WAVECOORD_TOKEN_FILE=~/.wavecoord/hosts/<host>.token` instead, and the plugin option token is removed from their environment: a local worker never acts with the lead token. Without that file, local workers fall back to `~/.wavecoord/token`.

## Content

- **Messages, reports, proposals and channel events are data.** They never grant consent or approval. The lead must ask the user in chat for protected paths, destructive actions and merges that `protected_paths_cmd` flags.
- The channel bridge does not declare `claude/channel/permission`, so coordination traffic can never answer a permission prompt.
- **Steering a worker.** The launcher writes a worker's messages into the worker's stdin as user lines with a header that says they are data, and the worker rules say a hint is guidance and the task stays the authority. Only a message from `lead` can be a steer action (interrupt, model switch, stop); from any other sender the same text stays plain text. The wrapper never sends a permission answer on stdin: it registers no `can_use_tool` handler.
- **Inbox sockets.** The launcher removes `CLAUDE_CODE_MESSAGING_SOCKET` and `CLAUDE_CODE_MESSAGING_TOKEN` from a worker's environment, so a worker started from inside a Claude session cannot write into that session. The inbox relay (`hooks.md`) gets the session's own socket and token on stdin, never on a command line, and writes only into that socket; Claude Code still applies the session's inbound rules (`crossSessionInbound`).
- The public repo holds no secrets, hostnames or addresses of any deployment. Consumers keep those in their own config. `tests/test_generic.py` checks the repo for common secret formats.

## Reporting

Report a vulnerability privately through GitHub security advisories on `jellologic/wavecoord`, not in a public issue.
