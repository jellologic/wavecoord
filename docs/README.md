# wavecoord docs

Start with the [quickstart](quickstart.md) to get a coordinator running, then read the [guide](guide.md) to learn the workflow. The other pages are reference.

## Learn

| Page | What |
|---|---|
| [quickstart.md](quickstart.md) | Install, set up with `wavecoord init` and `/wavecoord:setup`, and merge a first wave on one machine |
| [guide.md](guide.md) | Using wavecoord well: the model, one feature from idea to merged code, good specs, safe waves, steering, triage, recovery, FAQ and upgrade notes |

## Plan

| Page | What |
|---|---|
| [specs.md](specs.md) | The spec tree: capabilities, features, requirements and tasks as Markdown records; checks, waves and status commits |
| [task-spec.md](task-spec.md) | The flat task format of `plan/tasks`, its fields, and the "ready" rules |
| [adapters.md](adapters.md) | Every key of `.claude/wavecoord.json`; the plan adapters; `pr_cmd`, `pr_state_cmd`, `merge_cmd`, `prepare_cmd` |

## Run

| Page | What |
|---|---|
| [launcher.md](launcher.md) | Hosts and placement, attempts and worktrees, push and PR flow, listening to and steering workers |
| [providers.md](providers.md) | Provider profiles (Anthropic-compatible endpoints), rate limits and fallback |
| [feedback.md](feedback.md) | How events reach the lead: the Monitor, the hooks, the channel; the watchdog |
| [hooks.md](hooks.md) | The Claude Code hooks, the git guards and the edit fence points E1 to E5 |

## Trust and recovery

| Page | What |
|---|---|
| [security.md](security.md) | Network, tokens and roles, what the lead runs at merge, and why coordination text is never consent |
| [rollback.md](rollback.md) | Revert one event, roll state back to a commit of `coord/log`, rebuild the cache, a forked log |
| [contract.md](contract.md) | The REST, SSE and MCP contract: state machines, tools, constants |

## Design notes

| Page | What |
|---|---|
| [design/edit-fence.md](design/edit-fence.md) | The edit fence: the invariant, the five points, failure modes, rollout stages |
| [design/spec-layer.md](design/spec-layer.md) | The spec tree: the record model, the rules across records, waves, status commits |

Terminal captures used in these pages are in [img/](img/).
