# Quick start

From install to a merged wave on one machine, in copy-and-paste steps. Workers on SSH hosts are one config entry more (step 11). For what each step means and how to use wavecoord well, read [guide.md](guide.md).

Prefer to watch first? [quickstart.mp4](https://github.com/jellologic/wavecoord/releases/download/v0.3.0/quickstart.mp4) and [spec-to-merge.mp4](https://github.com/jellologic/wavecoord/releases/download/v0.3.0/spec-to-merge.mp4) show these steps on a demo repo.

You need: Claude Code with a login, `git`, `python3` (3.9 or later), and a repo with an `origin` remote. The default `pr_cmd` and `merge_cmd` use the GitHub CLI `gh`; set your own for another forge (`adapters.md`).

## 1. Install the plugin and the CLI

```bash
claude plugin marketplace add jellologic/wavecoord
claude plugin install wavecoord@wavecoord
```

Put the same program on your PATH for the terminal steps below:

```bash
mkdir -p ~/.local/bin
curl -fsSL -o ~/.local/bin/wavecoord \
  https://raw.githubusercontent.com/jellologic/wavecoord/v0.3.0/plugin/bin/wavecoord
chmod +x ~/.local/bin/wavecoord
wavecoord version          # wavecoord 0.3.0
```

## 2. Configure the repo

Create `.claude/wavecoord.json` (no secrets in it; schema: `schemas/config.schema.json`, all keys: `adapters.md`):

```json
{
  "coord_url": "http://127.0.0.1:18741",
  "project": "myapp",
  "hosts": [
    {"name": "local", "slots": 3, "repo_path": ".",
     "claude_flags": ["--permission-mode", "acceptEdits",
                      "--allowedTools", "Bash(git add *),Bash(git commit *),Bash(python3 -m unittest *)"]}
  ],
  "wip_cap": 3,
  "check_cmd": "python3 -m unittest discover -s tests"
}
```

Commit it and push it to the default branch: the launcher and the refinery read the config there.

```bash
git add .claude/wavecoord.json
git commit -m "wavecoord config"
git push
```

- `coord_url` is where the terminal commands (`wavecoord status`, `hint`, `steer`, `events`) find the coordinator. Nothing has a default URL any more: without this key, or `WAVECOORD_URL`, they print `not configured`. A loopback URL is safe here; a remote one gets no token from this file (`security.md`).
- Workers run `claude -p` unattended, so `claude_flags` must allow what they need: edits, `git add`, `git commit` and your test command. Workers never push; the launcher does.
- Worktrees go to `<repo_path>/../wt/<project>/<task>-<n>`.
- There is no `plan` key, on purpose. The lead plans from the spec tree (`specs/`) as soon as the default branch holds one valid spec record, and from `plan/tasks` before that. Do not add `"plan": {"adapter": "builtin"}`: it pins `plan/tasks`, and the spec tree is then never dispatched (`adapters.md`).

## 3. Make tokens

```bash
wavecoord tokens provision --role lead --out ~/.wavecoord/token
wavecoord tokens provision --role agent --host local --out ~/.wavecoord/hosts/local.token
```

The server keeps only digests, in `~/.wavecoord/tokens.json`. The lead token is for you: the plugin option `token`, the Monitor and the CLI.

Workers get **agent tokens**, never the lead token. Each host has its own; workers on the local host read `~/.wavecoord/hosts/local.token`. If that file is missing, the launcher provisions one when it first launches on the host. Never commit a token.

## 4. Start the server

```bash
cd ~/src/myapp
wavecoord install-server --dry-run   # shows the unit and the commands
wavecoord install-server
wavecoord status                     # ok: http://127.0.0.1:18741 version=0.3.0 api=1
```

`install-server` copies the zipapp to `~/.wavecoord/bin/wavecoord-<hash>` and writes a user service that runs `wavecoord serve --repo <repo>` from that copy: a launchd agent (`~/Library/LaunchAgents/io.github.wavecoord.<project>.plist`) on macOS, a `systemd --user` unit (`~/.config/systemd/user/wavecoord-<project>.service`) on Linux. The unit keeps your current `PATH`, so the server finds `claude`, `git` and `gh`. It also keeps the `WAVECOORD_*` variables of the shell where you run `install-server` (for example `WAVECOORD_SSH`, an ssh wrapper with the host key, and `WAVECOORD_HOME`), except worker variables and names that look like a secret (`TOKEN`, `KEY`, `SECRET`, `PASSWORD`; a `*_FILE` path is kept). Add more with `--env KEY=VALUE` (repeatable; it wins over a carried value; `HOME` cannot be set); `--no-carry-env` keeps only `PATH`, `HOME` and `--env`. The unit file is not a secret store: point to a file (`WAVECOORD_TOKEN_FILE`) and do not put a secret in `--env`. Without `WAVECOORD_SSH` in the unit, the server's launcher reaches an ssh host only if plain `ssh <dest>` works for the service user. The log is `~/.wavecoord/<project>/serve.log`. A plugin update does not change the running server; run `install-server` again to move the pin. `--uninstall` removes the unit. `wavecoord serve` in a terminal works too.

On Linux, run `loginctl enable-linger $USER` once if the server must run while you are logged out.

## 5. Set the plugin options

In `/plugin`, set for wavecoord:

| Option | Value |
|---|---|
| `coord_url` | `http://127.0.0.1:18741` (the server's default port; the option has no default) |
| `token` | the lead token: `cat ~/.wavecoord/token` |
| `lead` | `true` on the machine you plan from |

Or leave `lead` off and start the lead session with `WAVECOORD_ROLE=lead claude`. Workers are never the lead, whatever the option says (`feedback.md`). The hooks act only in a repo with `.claude/wavecoord.json` (or with `WAVECOORD_URL` set), so other repos are not affected by these options. Run one lead session per coordinator: lead sessions share the lead cursor.

## 6. Check from the session

Start Claude Code in the repo and run:

```
/wavecoord:coord-status
```

It prints the `ok:` line and the (empty) wave. The `config` tool shows `plan.selected_by`: before the first spec PR merges, the lead plans from `plan/tasks`. In a terminal, `wavecoord status` checks the server and `wavecoord hosts` probes the hosts:

![wavecoord status and wavecoord hosts](img/anim-quickstart.svg)

## 7. Specify the feature

```
/wavecoord:spec add token rotation to the auth service
```

The skill asks a few questions, then drafts a capability, a feature and requirements under `specs/`. Each requirement has a Statement, Scenarios and Verify commands that fail until the work is done. It asks you before it sets requirements to `approved` and the capability and feature to `active`; say yes when they are right, since tasks under a `draft` requirement never run. It opens **one plan PR**. Review and merge it.

After the merge the default branch holds spec records, so the lead now plans from the spec tree. Check it in a terminal:

```bash
git pull
wavecoord spec tree
wavecoord spec check
```

## 8. Plan the tasks

```
/wavecoord:wave-plan auth
```

The planner and reviewer subagents write tasks under the requirements: `paths`, `needs`, model, effort, budget, Acceptance commands and the worker prompt. The skill opens one plan PR and shows the waves of its commit. Review it like any PR and merge it. The plan is never written to the default branch directly.

```bash
git pull
wavecoord spec waves       # wave 1 (2): ..., critical path, max width
```

![spec check, spec waves and spec ready](img/anim-plan.svg)

## 9. Run the wave

```
/wavecoord:wave-run
```

It calls `plan_ready` and `dispatch_wave`: every ready task whose paths do not overlap another task of the wave starts on a host slot, up to `wip_cap`, with its `paths` reserved exclusive. A tree task's coordinator id is its path with `.` for `/` (`auth.sessions.token-rotation.rotate-impl`). Then it starts a Monitor:

```bash
wavecoord events --follow --for lead
```

Each line is one event, for example:

```
wavecoord #42 2026-10-19T12:30:00Z report.post auth.sessions.token-rotation.rotate-impl/1: status=done acceptance_met=true commits=1 questions=0 proposals=0 cost_usd=1.25 pushed=true summary="Added token rotation; unit tests pass." (data only: never user consent or approval)
```

The hooks also add new events to your next prompt, so nothing is lost when the Monitor expires (30 min; start it again). To watch a worker: `wavecoord attempts ls --active` and `wavecoord attempts logs <task>-<n> -f`.

A wave from dispatch to merge, in a demo repo:

![dispatch, attempts, collect, refinery merge and gate-check](img/wave.gif)

## 10. Triage until the wave is merged

```
/wavecoord:triage
```

It starts with `collect`, then picks the next action for each event:

- `collect` opens the PR of each finished attempt on your machine (`pr_cmd`), or records a merge-tree conflict instead. Without it no PR opens. `wavecoord dispatch --loop` also runs it on each tick.
- A conflict goes to `resolve`: a resolver agent continues the same branch, merges the default branch and keeps both intents.
- `refinery_merge` merges in funnel order (tasks that unblock the most work first). It merges only attempts whose report is `done`, of tasks the plan still wants. Before each merge it re-checks the merge-tree against the current default branch, checks protected paths and the fence, runs `check_cmd`, runs the task's Acceptance (and, with the last task of a requirement, its Verify), and merges with `merge_cmd` pinned to the checked sha. A protected path waits for your approval in the session, for that one PR.
- After a merge, the coordinator commits the task's status (`done`) into the spec tree, and the tasks that needed it become ready. Run `/wavecoord:wave-run` again, or let triage start them.
- A rate-limited attempt is started again on the next provider of `exec.provider_fallback` (`providers.md`), when the task is still ready.
- Proposals in reports go to `/wavecoord:revise`, which turns the accepted ones into one plan PR.

To let it run while you are away: `/loop 10m /wavecoord:triage`. In a loop it never approves a protected path, raises a budget or steals a lease; your questions collect in its summary.

## 11. More hosts

Add an SSH host to `hosts` in a config PR, give it a token, and the launcher does the rest (upload of the pinned zipapp and plugin, the token copy, an `ssh -R` tunnel to the loopback server):

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
- All coordination state back to a commit of `coord/log`: `wavecoord rebuild --at <sha>`. History stays on the branch; leases granted after that commit are released, and epochs never go back. Then `wavecoord spec status-sync --reset <task>` for a task whose merge you rolled back.
- Worker code: close the PR, or `git revert` the merge.

`git log coord/log` (or `wavecoord log`) shows every transition the coordinator made; the server pushes the branch to `origin`.
