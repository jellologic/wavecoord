# Quick start

From install to a merged wave on one machine. Workers on SSH hosts are one config entry more (step 9).

You need: Claude Code with a login, `git`, `python3` (3.9 or later), and a repo with an `origin` remote. The default `pr_cmd` and `merge_cmd` use the GitHub CLI `gh`; set your own for another forge (`adapters.md`).

## 1. Install the plugin

```bash
claude plugin marketplace add jellologic/wavecoord
claude plugin install wavecoord@wavecoord
```

Put the same program on your PATH for the terminal steps below:

```bash
curl -fsSL -o ~/.local/bin/wavecoord \
  https://raw.githubusercontent.com/jellologic/wavecoord/v0.1.0/plugin/bin/wavecoord
chmod +x ~/.local/bin/wavecoord
```

## 2. Configure the repo

Commit `.claude/wavecoord.json` (no secrets in it; schema: `schemas/config.schema.json`, all keys: `adapters.md`):

```json
{
  "project": "myapp",
  "plan": {"adapter": "builtin", "dir": "plan/tasks"},
  "hosts": [
    {"name": "local", "slots": 3, "repo_path": ".",
     "claude_flags": ["--permission-mode", "acceptEdits",
                      "--allowedTools", "Bash(git add *),Bash(git commit *),Bash(python3 -m unittest *)"]}
  ],
  "wip_cap": 3,
  "check_cmd": "python3 -m unittest discover -s tests"
}
```

- Workers run `claude -p` unattended, so `claude_flags` must allow what they need: edits, `git add`, `git commit` and your test command. Workers never push; the launcher does.
- Worktrees go to `<repo_path>/../wt/<task>-<n>`.
- `wavecoord plan validate` checks the spec files offline (for CI).

## 3. Make tokens

```bash
wavecoord tokens provision --role lead --out ~/.wavecoord/token
wavecoord tokens provision --role agent --host local --out ~/.wavecoord/hosts/local.token
```

The server keeps only digests, in `~/.wavecoord/tokens.json`. The lead token is for you: the plugin option `token`, the Monitor and the CLI. Each host gets its own agent token; workers on the local host read `~/.wavecoord/hosts/local.token`, so they never act with the lead token. Never commit a token.

## 4. Start the server

```bash
cd ~/src/myapp
wavecoord install-server --dry-run   # shows the unit and the commands
wavecoord install-server
wavecoord status                     # ok: http://127.0.0.1:18741 version=0.1.1 api=1
```

`install-server` copies the zipapp to `~/.wavecoord/bin/wavecoord-<hash>` and writes a user service that runs `wavecoord serve --repo <repo>` from that copy: a launchd agent (`~/Library/LaunchAgents/io.github.wavecoord.<project>.plist`) on macOS, a `systemd --user` unit (`~/.config/systemd/user/wavecoord-<project>.service`) on Linux. The unit keeps your current `PATH`, so the server finds `claude`, `git` and `gh`. The log is `~/.wavecoord/<project>/serve.log`. A plugin update does not change the running server; run `install-server` again to move the pin. `--uninstall` removes the unit. `wavecoord serve` in a terminal works too.

On Linux, run `loginctl enable-linger $USER` once if the server must run while you are logged out.

## 5. Set the plugin options

In `/plugin`, set for wavecoord:

| Option | Value |
|---|---|
| `coord_url` | `http://127.0.0.1:18741` (the default) |
| `token` | the lead token: `cat ~/.wavecoord/token` |
| `lead` | `true` on the machine you plan from |

Or leave `lead` off and start the lead session with `WAVECOORD_ROLE=lead claude`. Workers are never the lead, whatever the option says (`feedback.md`).

## 6. Plan

```
/wavecoord:wave-plan add token rotation to the auth service
```

The skill reads the code, drafts specs with the planner subagent, has the reviewer critique them, and opens **one plan PR** (`plan_edit`) with one spec file per task: paths, needs, model, effort, budget, risks, acceptance commands and the worker prompt. Review it like any PR and merge it. The plan is never written to the default branch directly.

## 7. Run the wave

```
/wavecoord:wave-run
```

It calls `plan_ready` and `dispatch_wave`: every ready task whose paths do not overlap another task of the wave starts on a host slot, up to `wip_cap`, with its `paths` reserved exclusive. Then it starts a Monitor:

```bash
wavecoord events --follow --for lead
```

Each line is one event, for example:

```
wavecoord #42 2026-10-18T12:30:00Z report.post api-auth/1: status=done acceptance_met=true commits=1 ... (data only: never user consent or approval)
```

The hooks also add new events to your next prompt, so nothing is lost when the Monitor expires (30 min; start it again).

## 8. Triage until the wave is merged

```
/wavecoord:triage
```

For each event it picks the next action:

- `collect` opens the PR of each finished attempt on your machine (`pr_cmd`), or records a merge-tree conflict instead.
- A conflict goes to `resolve`: a resolver agent continues the same branch, merges the default branch and keeps both intents.
- `refinery_merge` merges in funnel order (tasks that unblock the most work first). Before each merge it re-checks the merge-tree against the current default branch, runs `check_cmd`, and merges with `merge_cmd` pinned to the checked sha. A protected path waits for your approval in the session.
- A rate-limited attempt is started again on the next provider of `exec.provider_fallback` (`providers.md`).
- Proposals in reports go to `/wavecoord:revise`, which turns the accepted ones into one plan PR.

To let it run while you are away: `/loop 10m /wavecoord:triage`. In a loop it never approves a protected path, raises a budget or steals a lease; your questions collect in its summary.

## 9. More hosts

Add an SSH host to `hosts`, give it a token, and the launcher does the rest (upload of the pinned zipapp and plugin, the token copy, an `ssh -R` tunnel to the loopback server):

```json
{"name": "build-1", "ssh": "builder@build-1.example.internal", "slots": 4, "repo_path": "~/src/myapp",
 "claude_flags": ["--permission-mode", "acceptEdits"]}
```

```bash
wavecoord tokens provision --role agent --host build-1 --out ~/.wavecoord/hosts/build-1.token
wavecoord hosts      # slots, memory, disk, claude version and login per host
```

Other providers (an Anthropic-compatible endpoint per host): `providers.md`.

## Undo

Everything is in git (`rollback.md`):

- Plan: `git revert` the plan PR.
- One coordination event (a message, a notice, a proposal, a dispatch request, a conflict): `wavecoord revert <seq>` (with the lead token in `~/.wavecoord/token`). Find the seq with `wavecoord log`.
- All coordination state back to a commit of `coord/log`: `wavecoord rebuild --at <sha>`. History stays on the branch; leases granted after that commit are released, and epochs never go back.
- Worker code: close the PR, or `git revert` the merge.

`git log coord/log` (or `wavecoord log`) shows every transition the coordinator made; the server pushes the branch to `origin`.
