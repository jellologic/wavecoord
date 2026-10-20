# The LLM execution plan of a task

Each spec carries the plan for the worker that will run it: which model, how hard it thinks, how much it may spend and how long it may take. The launcher passes these values to `claude -p` (`--model`, `--fallback-model`, `--max-budget-usd`, `--max-turns`, the effort setting) and kills the attempt at `timeout_min`.

## Starting points by size

Start here, then adjust for the risks of the task.

| Size | model | effort | max_turns | budget_usd | timeout_min | autocompact_window |
|---|---|---|---|---|---|---|
| XS | sonnet (haiku for pure text edits) | low | 25 | 1 | 20 | omit |
| S | sonnet | medium | 50 | 2 | 40 | omit |
| M | sonnet, or opus for unfamiliar code | high | 90 | 5 | 75 | 160000 |
| L | opus | high | 150 | 10 | 150 | 160000 |

Adjust:

- Class `A`, or a task where a wrong design is expensive: `opus`, effort `xhigh`, a design-note task first.
- Long builds or slow tests: raise `timeout_min` to at least 3 times one full build plus test run. Do not raise `max_turns` for this: waiting does not cost turns.
- Large or generated files in `paths`: raise `budget_usd`; tell the prompt to read them with ranges, not whole.
- Many small mechanical edits across files: `sonnet`, effort `medium`; a high effort only adds cost.
- Always set `fallback_model` one step down (`opus` -> `sonnet`), so an overload does not end the attempt.

## Provider

- `provider` names a profile from `config.providers` (docs/providers.md). Leave it out to use `default_provider`.
- `provider_fallback` lists profiles to use next after a rate limit or overload, in order. Give one when the wave has more tasks than one provider's rate limit allows.
- `host_needs` lists capabilities the host must have (`python3`, `docker`, a label). Placement uses it; a task with no matching host waits.
- A non-Anthropic model behind a profile may differ: weaker tool use, a smaller context window, no extended thinking, other limits. For such a profile choose smaller tasks, a lower `autocompact_window`, explicit file lists in the prompt and acceptance commands that check the result, not the process.
- A non-Anthropic model through an Anthropic-compatible endpoint (for example GLM) uses about 1.5 to 2 times the turns of the table. Set `max_turns` to about 2 times the starting point for such a profile. Without that margin the worker hits `error_max_turns` before it writes the report.

## Budget and turns

- `budget_usd` is a hard stop. Total for the wave = the sum; show it in the PR body.
- `max_turns` caps the loop. A worker that hits it ends with a `failed` or `blocked` report; triage then splits the task or raises the cap. When the worker hits the cap before it writes its report but after it committed, the launcher pushes the commits and writes a `failed` report with `plan_feedback: "continue: <n> commits pushed"` and `continue_suggested: true` on the attempt. Triage then continues the run (`/wavecoord:triage`).
- `split_if` is the sign that the task is bigger than planned ("needs a schema change", "touches more than 8 files", "the API has no endpoint for X"). The worker stops, reports `needs_decision` and proposes a split.

## Acceptance commands

- They run from the repo root on the worker host, in the worktree, without user input.
- They need no secrets or network unless every host in `host_needs` has them.
- They fail when the work is missing (a new test, not only the old suite).
- Keep the slowest command last. When the full suite is slow, run the task's tests here and leave the full suite to `check_cmd` in the refinery.

## Worker prompt

The worker sees the prompt, the spec and the repo, nothing from this conversation. Write:

1. The goal in one sentence.
2. The files to change, and the files and interfaces not to change.
3. The tests to write first.
4. The acceptance commands, and "list each one you ran in `checks_run`".
5. When to stop: `split_if`, an ambiguous requirement (report `needs_decision` with the question), a needed change outside `paths` (send a message to the lead).
6. "Commit your work before you end. Do not push and do not merge: the launcher pushes your branch." (A launched worker is denied `git push`; a prompt that asks for a push makes it report `blocked`.)
7. For a turn budget: "Batch shell commands into one call. Do not read a file again that you already read." The worker rules of the launcher also tell the worker to stop, commit and write the report when fewer than 15% of its turns remain.
