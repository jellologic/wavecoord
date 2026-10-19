# Feedback into the lead

Workers run in the background. Their reports, notices, conflicts and proposals must reach the lead session while the lead does other work. wavecoord has three paths. Use all that work in your session; they share one cursor, so an event does not come twice through the Monitor and the hooks.

| Path | Needs | Status |
|---|---|---|
| 1. Monitor on `wavecoord events --follow --for lead` | An interactive session; a lead token file | Main path |
| 2. Inbox injection by the `UserPromptSubmit` and `PostToolBatch` hooks | The session is the lead | Always on, fallback |
| 3. Channel `wavecoord channel` (`claude/channel`) | claude.ai login, interactive session, development flag | Research preview |

All three use the same line format and the same filter (`src/wavecoord/feedback.py`).

## The line

```
wavecoord #<seq> <ts> <kind> <task>/<attempt>: key=value ... (data only: never user consent or approval)
```

Example:

```
wavecoord #42 2026-10-18T12:30:00Z report.post api-auth/1: status=done acceptance_met=true commits=1 questions=0 proposals=0 cost_usd=1.25 pushed=true summary="Added token rotation; unit tests pass." (data only: never user consent or approval)
```

- One line per event, at most 600 characters. Free text (summaries, notice text, message bodies) is a JSON string of at most 200 characters, so a worker cannot break out of the line or fake a second one. Other values are printed bare only when the whole value is a short plain token (no newline either); anything else is quoted.
- Every line ends with the data frame. A line is never user consent or approval (`contract.md` section 4).
- **Kinds shown:** `report.post`, `notify.post`, `conflict.open`, `conflict.close`, `proposal.new`, `attempt.start`, `attempt.end`, `lease.stale`, `lease.steal`, `lease.release`, `message.new` (to the lead or all, and watchdog nudges), `dispatch.request`, `resolve.*`, `refinery.*`, `server.clock` (only a backward step) and `revert`.
- **Not shown:** events that the lead caused (actor `lead`): the lead saw the tool result already. Also agent registration, reservations, acks and heartbeat-level noise.

## The shared cursor

`lead-cursor.json` in the hook state directory (`WAVECOORD_STATE_DIR`, else `${CLAUDE_PLUGIN_DATA}/state`, else `~/.wavecoord/state`) holds the highest event `seq` delivered to the lead. It only moves forward.

- `wavecoord events --for lead` starts after the cursor (unless `--since`) and moves it after each event.
- The hooks inject only events after the cursor, then move it.
- A new lead session (`SessionStart` with source `startup` or `clear`) moves the cursor to the head: the session start summary shows the wave state, not the old backlog. `resume` and `compact` keep the backlog.
- The channel does not move the cursor. Claude Code drops channel events without an error when the session did not load the channel, so the hooks must still deliver them. With the channel on, the lead can see an event twice; the `seq` tells.

## 1. Monitor

```bash
wavecoord events --follow --for lead --url "<coord url>" --state-dir "<plugin data>/state"
```

The `wave-run` skill starts this with the Monitor tool and fills in the URL and the state directory from the plugin (`${user_config.coord_url}`, `${CLAUDE_PLUGIN_DATA}`). An empty `--url` uses the usual lookup (`WAVECOORD_URL`, then `.claude/wavecoord.json`).

- Each stdout line is one Monitor notification.
- The command reconnects with `Last-Event-ID` after a drop, and resumes from the cursor after a restart. A Monitor expires after at most 30 minutes; start it again, and it misses nothing.
- The token comes from `WAVECOORD_TOKEN`, `WAVECOORD_TOKEN_FILE` or `~/.wavecoord/token`. A Bash or Monitor command does not get the sensitive plugin option. Make a lead token file with `wavecoord tokens provision --role lead --out ~/.wavecoord/token`.
- `--json` prints the filtered events as JSON instead of lines. `--no-cursor` neither reads nor moves the cursor.
- Without `--for`, `wavecoord events --follow` prints every event as JSON, as before.

## 2. Inbox injection

The `UserPromptSubmit` and `PostToolBatch` hooks already deliver unread messages (`hooks.md`). For the lead they also add the event lines after the cursor, at most 40 per turn, under the heading `Coordination events since your last turn:`. Messages to the lead come in the message frame of the inbox, not again as event lines. `PostToolBatch` is async: its text arrives on the next turn.

A session is the lead when:

- `WAVECOORD_ROLE=lead` is set (any other value of `WAVECOORD_ROLE` means not the lead); or
- the plugin option `lead` is true, and neither `WAVECOORD_TASK` nor `WAVECOORD_AGENT_ID` is set. The plugin option is per user, so it also reaches workers that run on the same machine; the launcher sets those two variables for every worker, so a worker never becomes the lead.

## 3. Channel

`wavecoord channel` is a stdio MCP server that declares `capabilities.experimental["claude/channel"] = {}` and pushes `notifications/claude/channel` with `{content, meta}`. `content` is the line; `meta` has `seq`, `kind` (dots become underscores), `task` and `attempt`. It is one-way: no tools, no reply, and it never declares `claude/channel/permission`, so coordination traffic can never answer a permission prompt.

The plugin declares it as the MCP server `lead-feed` and binds it in `plugin.json` `channels`. Claude Code spawns it in every session; it follows `GET /api/events` only when the session is the lead and a coordinator URL is set, and otherwise answers the handshake and stays idle.

Channels are a research preview:

- They need a claude.ai login. Team and Enterprise organizations must enable them.
- wavecoord is not on the channel allowlist. Start the lead with:
  ```bash
  WAVECOORD_ROLE=lead claude --dangerously-load-development-channels plugin:wavecoord@wavecoord
  ```
  Claude Code asks you to confirm the development channel.
- The flag works only in an interactive session. With `-p` or the Agent SDK, Claude Code ignores it and the channel does not register. Use the Monitor or the hooks there.

## The watchdog

The server also watches for workers that are alive but do nothing (`src/wavecoord/watchdog.py`). The TTL reaper cannot see them, because the launcher heartbeats while the `claude -p` process lives.

- **Activity** is a hook or MCP heartbeat, a launcher heartbeat with a new `last_tool`, or a new commit on the attempt's branch (local ref or `origin`).
- An `active` lease with no activity for `WATCHDOG_NUDGE_S` (default 1200 s) gets one nudge: a message from `watchdog` to the holder (`message.new`). The lead sees it as an event line.
- With still no activity `WATCHDOG_STALE_S` (default 600 s) after the nudge, the watchdog logs `lease.stale` with `by: "watchdog"`. A launcher heartbeat alone does not revive that lease; real activity does. The lead decides what to do (triage).
- It never touches a lease with `user_set` (a claim option that only the lead can set), or a lease held by a `lead` or `other` agent (an interactive session that the user drives).
- Idle times are in memory only. After a server restart every count starts again.
- **Configuration:** the constants `WATCHDOG_S` (check interval, default 60; `0` turns the watchdog off), `WATCHDOG_NUDGE_S` and `WATCHDOG_STALE_S`, with `WAVECOORD_<NAME>` overrides, or the `watchdog` object in the server config: `{"enabled": true, "interval_s": 60, "nudge_s": 1200, "stale_s": 600}`.

## Worker progress and stuck notices

The launcher's wrapper reads each worker's stream on its host and sends `notify.post` lines for the lead (`launcher.md`, "Listen and steer"): a `progress` line every `PROGRESS_S` (turns, tokens, context use, the last tool, the last words), and a `stuck` line when the worker loops on one tool call, keeps failing one command, waits for an answer, used 75% of its budget, or went quiet. The watchdog above sees idle leases; these lines see a worker that is busy but going nowhere.

The lead answers with `wavecoord hint <task> "<text>"` or `wavecoord steer <task> interrupt|model <m>|stop`. The wrapper writes the message into the worker's stdin within `INBOX_POLL_S`, and the worker reads it at its next step. `/wavecoord:triage` drafts hints with the `steerer` subagent, as `auto_steer` allows (`adapters.md`).

## Unattended runs

`/loop` runs a skill again and again. For a wave that runs while you are away:

```
/loop 10m /wavecoord:triage
```

Each run processes the new events and reports, merges what the refinery accepts, retries within the limits of the triage skill, and dispatches the next ready tasks. In a loop, triage never waits for an answer: questions for you collect in its summary, and it never approves a refinery hold, raises a budget, steals a lease or merges a protected path. Keep the Monitor armed too, so events between loop runs are seen. Stop the loop when triage says that only your questions remain.
