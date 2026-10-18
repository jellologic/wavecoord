# Provider profiles

A provider profile makes the workers of an attempt run Claude Code against an Anthropic-compatible provider instead of the host's default login. The lead session keeps its own login. A profile is only a name and a path in git; the secrets stay in a file on each host.

## Config

In `.claude/wavecoord.json` (`schemas/config.schema.json`):

```json
{
  "providers": {
    "zai": {
      "env_file": "~/.config/wavecoord/providers/zai.env",
      "models": {"opus": "glm-5.3", "sonnet": "glm-5.3", "haiku": "glm-5.3-flash", "subagent": "glm-5.3-flash"},
      "cost_basis": "none"
    },
    "openrouter": {
      "env_file": "~/.config/wavecoord/providers/openrouter.env",
      "models": {"opus": "~anthropic/claude-opus-latest[1m]", "sonnet": "~anthropic/claude-sonnet-latest[1m]",
                 "haiku": "~anthropic/claude-haiku-latest"}
    }
  },
  "default_provider": "zai",
  "hosts": [
    {"name": "build-1", "ssh": "builder@build-1.example.internal", "slots": 4, "repo_path": "/srv/repo",
     "providers": ["zai", "openrouter"]}
  ]
}
```

| Key | Meaning |
|---|---|
| `providers.<name>.env_file` | Path **on the host** of the profile's env file (`~` is the host user's home) |
| `providers.<name>.models` | Optional model ids for the aliases: `opus`, `sonnet`, `haiku`, `fable` set `ANTHROPIC_DEFAULT_<ALIAS>_MODEL`; `subagent` sets `CLAUDE_CODE_SUBAGENT_MODEL`. They win over the same variables in the env file |
| `providers.<name>.cost_basis` | What `total_cost_usd` means: `anthropic_estimate` (default: Claude Code's estimate at Anthropic prices, not the provider's bill), `provider`, or `none` |
| `default_provider` | The profile when neither the launch nor the task names one. Default `anthropic` |
| `hosts[].providers` | The profiles that host has. Absent: every configured profile, plus `anthropic` |

`anthropic` is built in: the host's default environment and login, no file.

A task picks its profile with `exec.provider` and lists the next ones with `exec.provider_fallback` (`task-spec.md`). `wavecoord launch --provider P --provider-fallback A,B` does the same from the CLI.

## The env file

One `KEY=VALUE` per line; values are shell words, so quote a value with spaces (`KEY='a b'`, `KEY=''`). Blank lines, `#` comments and a leading `export ` are allowed. No `$VAR` expansion.

```sh
# ~/.config/wavecoord/providers/zai.env   (chmod 600)
ANTHROPIC_BASE_URL='https://api.z.ai/api/anthropic'
ANTHROPIC_AUTH_TOKEN='<your z.ai key>'
API_TIMEOUT_MS=3000000
```

Rules the launcher enforces:

- The file lives on the host. It is never in git, never uploaded by the launcher, never printed or logged. Create it on each host yourself, for example `install -m 600 /dev/null ~/.config/wavecoord/providers/zai.env`, then edit it.
- It must be a regular file owned by the host user with no group or other permission bits (mode 600 or 400). Otherwise the attempt does not start: placement reports the host as not eligible, `_remote launch` refuses, and the wrapper checks again before it starts `claude`.
- `wavecoord hosts` shows, per profile, whether the file is present and usable. The probe uses `stat` only and never reads the values.

## What the worker gets

Only the `claude` child process gets the profile; `prepare_cmd` and `pr_cmd` do not. For a profile other than `anthropic`, the wrapper:

1. removes the parent's provider variables: every `ANTHROPIC_*` and `CLAUDE_CODE_USE_*` variable, `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CODE_SUBAGENT_MODEL`, `API_TIMEOUT_MS` and `AWS_BEARER_TOKEN_BEDROCK`;
2. sets `ANTHROPIC_API_KEY=""`, so a key of the parent can never be used;
3. loads the env file;
4. sets the model variables from `models`.

`wrapper.log` names the variables it set, never their values. Hooks that the worker runs inherit the worker's environment, so treat hook output like the worker's own.

Some providers make `claude` print warnings such as `[claude-code:unrecognized_model] {...}` on stderr. The launcher reads only stdout (`stream.jsonl`) and keeps stderr in `stderr.log`, so these lines never break the stream.

## Cost, usage and rate limits

`total_cost_usd` is Claude Code's estimate at Anthropic prices. The attempt result and the report post therefore also carry `usage` and `model_usage` (token counts from the final result) and `cost_basis` from the profile.

### Budgets

For a profile with `cost_basis: none` (a provider with its own prices behind an Anthropic-compatible endpoint, for example GLM), `total_cost_usd` means nothing, so a dollar limit means nothing too. The launcher then does not pass `--max-budget-usd` (`exec.budget_usd`, `--max-budget-usd`), and `wrapper.log` says that the budget is not enforced. Limit such a run with `exec.max_tokens` (`--max-tokens` on `wavecoord launch`): the wrapper counts the input, output and cache-write tokens of the main conversation from the stream (cache reads are not counted) and interrupts the run when the count reaches the limit (`reason: token_budget`, state `failed`). Without `exec.max_tokens`, only `exec.max_turns` and `exec.timeout_min` limit the run. For the other cost bases (`anthropic`, `anthropic_estimate`, `provider`) `--max-budget-usd` is passed as before, and `exec.max_tokens` applies too when it is set. The result has `tokens`, the count of the run.

The wrapper marks a run `throttled` on an `api_retry` with error `rate_limit` or `overloaded`, a rejected `rate_limit_event`, a `StopFailure` hook event, or an error result that names a rate limit or overload. The result and the report post carry `provider`, `throttled`, `throttle` (the reasons) and `next_provider` (the profile after the current one in `provider_fallback`). The launcher does not switch profiles itself.

## Fallback

The lead tools do:

- `collect` (the tool, the CLI and the dispatcher loop) starts the next attempt of a task on `next_provider` when an attempt ended throttled and not `reported`. The new attempt is a new branch (`<task>-<n+1>`); its result has `actions: [..., "fallback"]` and the new attempt in `fallback`.
- `dispatch` and `dispatch_wave` skip a provider for `PROVIDER_BACKOFF_S` (900 s) after a throttled attempt or a `rate_limit` notice that names it, and take the next one of `[exec.provider] + exec.provider_fallback` that a candidate host has. The `reason` of the decision lists the skipped ones (`alt: backed off (...)`). An explicit `overrides.provider` is never skipped.
- No provider left gives the refusal `rate_limited` (all backed off) or `no_provider`.

`tests/e2e/test_wave.py` runs this flow: the first provider is rate limited, the attempt is dispatched again on the fallback and merges.

## Known profiles

| Profile | `ANTHROPIC_BASE_URL` | Token variable | Notes |
|---|---|---|---|
| `anthropic` | (default) | the host's login or `ANTHROPIC_API_KEY` | Built in, no file |
| Z.ai GLM | `https://api.z.ai/api/anthropic` | `ANTHROPIC_AUTH_TOKEN` | Model ids such as `glm-5.3-flash`. Set `API_TIMEOUT_MS=3000000`. `--json-schema` structured output works. `claude` prints an `unrecognized_model` warning on stderr |
| OpenRouter | `https://openrouter.ai/api` | `ANTHROPIC_AUTH_TOKEN` | `ANTHROPIC_API_KEY` must be empty (the launcher sets it). Model ids such as `~anthropic/claude-opus-latest[1m]`; a `[1m]` suffix tells Claude Code that the model has a 1M context window. See the OpenRouter Claude Code guide (`openrouter.ai/docs/cookbook/coding-agents/claude-code-integration`) |

A host where `claude` is also logged in with a subscription still uses the profile: the `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` environment takes precedence over the stored login.
