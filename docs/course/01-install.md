# Lesson 1: Install wavecoord

**Video:** [course-01-install.mp4](https://github.com/jellologic/wavecoord/releases/download/v0.3.0/course-01-install.mp4) (about 5 minutes)

## Goals

- Install the wavecoord plugin in Claude Code and the `wavecoord` command line.
- Set up a repo with `wavecoord init`.
- Read `wavecoord doctor` and fix what it reports, also from Claude Code with `/wavecoord:setup`.

## Why it matters

wavecoord has two halves that must agree: the plugin in your Claude Code session (skills, hooks, the MCP tools of the lead) and a coordinator process that hands out leases and starts workers. Both are the same program. Setup connects them: a config file in the repo, tokens on your machine, and plugin options that tell Claude Code where the coordinator is. Doctor checks every link, so you never have to guess which one is broken.

## Steps

### 1. Check the prerequisites

```bash
claude --version && git --version && python3 --version
gh auth status
```

You need Claude Code logged in, `git`, Python 3.9 or later, and the GitHub CLI `gh` logged in (`gh auth login`, once). wavecoord opens and merges pull requests with `gh`. Your repo needs an `origin` remote with a default branch (`git push -u origin main`).

> The video uses a demo repo whose origin is a folder on the same machine, and a local stand-in for GitHub that answers the same `gh` commands. Your own setup uses the real `gh`.

### 2. Install the plugin

In Claude Code, in any folder:

```
/plugin marketplace add jellologic/wavecoord
/plugin install wavecoord@wavecoord
```

The marketplace and the plugin are both named `wavecoord`. If Claude Code asks for a scope, pick the user scope. Then exit (`/exit`): a new plugin loads when a session starts. From a terminal, the same is `claude plugin marketplace add jellologic/wavecoord` and `claude plugin install wavecoord@wavecoord`.

### 3. Install the command line

```bash
mkdir -p ~/.local/bin
curl -fsSL -o ~/.local/bin/wavecoord \
  https://raw.githubusercontent.com/jellologic/wavecoord/v0.3.0/plugin/bin/wavecoord
chmod +x ~/.local/bin/wavecoord
wavecoord version          # wavecoord 0.3.0
```

It is one Python file with no dependencies. `~/.local/bin` must be on your PATH (see Troubleshooting).

### 4. Set up the repo

In your repo:

```bash
wavecoord init --server --configure-plugin --pr
```

What you see: one line per action. It writes `.claude/wavecoord.json`, makes a lead token and a token for the local worker host under `~/.wavecoord/`, installs the coordinator as a user service (`--server`), sets the plugin options `coord_url`, `lead` and `token` in Claude Code (`--configure-plugin`), and opens a pull request with the config (`--pr`). It is safe to run again: it never changes a key or a token that is there. `--dry-run` shows the plan first.

Merge the config pull request and pull: the launcher and the merge queue read the config from the default branch.

```bash
gh pr merge <number> --merge && git pull
```

In the video we pass `--coord-url http://127.0.0.1:18793` and start the coordinator by hand, because the default port 18741 is busy on the recording machine. You do not need that.

### 5. Check with doctor

```bash
wavecoord doctor
```

Each line is `ok`, `warn` or `fail`, and every problem has a `fix:` line with the command to run. The last line counts them: `0 failed, 0 warnings, 18 ok`. `--quick` skips the host probes, `--json` prints JSON, and the exit code is 1 when a check fails.

### 6. Check from Claude Code

Restart Claude Code in the repo (the plugin options load at start) and type:

```
/wavecoord:setup
```

What you see: the skill runs `wavecoord doctor`, shows a short table of the problems in plain words, and offers to run `wavecoord init` for what is missing. It asks before it writes anything, and it never prints the token. When everything is `ok`, it points you to the first spec.

## Troubleshooting

| Doctor line or symptom | What it means | Fix |
|---|---|---|
| `wavecoord: command not found` | `~/.local/bin` is not on PATH | Add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc` or `~/.bashrc`, open a new terminal |
| `python` fail | Python older than 3.9 | Install Python 3.9 or later |
| `repo`, `origin`, `default branch` fail | Not a git repo, no `origin`, or nothing pushed | `git init`; `git remote add origin <url> && git push -u origin HEAD`; `git remote set-head origin --auto` |
| `gh` fail | `gh` missing or not logged in | Install gh, then `gh auth login` (or set `pr_cmd`, `merge_cmd` and `pr_state_cmd` for another forge: [adapters.md](../adapters.md)) |
| `claude` fail | Claude Code missing or not logged in | `claude auth login` |
| `plugin` fail or warn | Not installed, disabled, or older than the CLI | Step 2; `claude plugin enable wavecoord@wavecoord`; `claude plugin update wavecoord@wavecoord`, then restart Claude Code |
| `plugin options` fail | Claude Code does not know `coord_url` or `token` | `wavecoord init --configure-plugin` (or `/plugin configure wavecoord@wavecoord`), then restart Claude Code |
| `plugin options` warn: lead is off | This machine is not the lead | Set `lead` to true, or start with `WAVECOORD_ROLE=lead claude` |
| `config` fail: not on origin | The config is only in your working tree | Merge the config PR (or commit and push it) |
| `coord_url` fail, or `wavecoord status` says `not configured` | No coordinator URL | `wavecoord init` (adds `coord_url`) or `export WAVECOORD_URL=http://127.0.0.1:18741` |
| `coordinator` fail, or `unreachable` | No server answers | `wavecoord install-server`; if it is installed, read its log (doctor names it) |
| `coordinator` warn: version | The service runs an older build | `wavecoord install-server` again |
| `token` fail: refuses the token | The plugin's token is not one this server made | `wavecoord init --configure-plugin`, restart Claude Code |
| `lead token` fail: mode | Others can read the token file | `chmod 600 ~/.wavecoord/token` |
| `host local` fail | The worker host cannot run Claude Code | `claude auth login` on that host; `wavecoord hosts` for details |
| `service` warn | No user service | `wavecoord install-server` (or run `wavecoord serve` in a terminal) |
| The server stops when you log out (Linux) | systemd user services end with the session | `loginctl enable-linger $USER` once |

## Common mistakes

- **Not restarting Claude Code** after setting the plugin options. The options load at session start.
- **Leaving the config PR unmerged.** Doctor says `not on origin/main`; workers would not see it.
- **Two lead sessions** on one coordinator. They share one event cursor and split the events. Run one.
- **Pasting the token into the chat.** Never needed: `init --configure-plugin` moves it to Claude Code's secure storage.

## Exercise

1. Install the plugin and the CLI.
2. In a repo of your own (or a practice repo from the [course page](README.md#practice-repos)), run `wavecoord init --dry-run --server --configure-plugin --pr` and read the plan. Then run it without `--dry-run`.
3. Run `wavecoord doctor` until it reports `0 failed`. For each `warn`, say in one sentence why it is there.
4. Restart Claude Code and run `/wavecoord:setup`.

Next: [Lesson 2: Your first spec](02-first-spec.md).
