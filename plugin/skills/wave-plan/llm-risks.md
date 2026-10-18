# Checklist: common LLM risks in a task

Go through the list for each task. For each risk that applies, add a `## Risks` item with `kind: llm` (or `technical`) and a concrete mitigation in the spec: a field, a prompt line or an acceptance command. A mitigation that only says "be careful" does not count.

| Risk | Signs | Mitigation in the spec |
|---|---|---|
| Context overflow | Large files, long logs, many files to read, long sessions | Smaller task or `split_if`; `autocompact_window`; prompt: "read large files with line ranges; pipe long command output through `tail`" |
| Long builds | Build or full suite takes more than a few minutes | `timeout_min` >= 3 x build+test; acceptance runs the targeted tests; prompt: "run the build in the background and do not rerun it without a change" |
| Flaky tests | Timing, network, ports, order-dependent tests | Acceptance names the stable tests; prompt: "if a test fails without your change, rerun it once, then report it in `risks_hit`; do not change the test to pass" |
| Ambiguous spec | Two valid readings, an undefined format or interface | Resolve it in the plan, or ask the user now. In the prompt: "if X is not clear, stop and report `needs_decision` with the question" |
| Scope creep | Refactors, renames, "improvements" outside the goal | Narrow `paths`; prompt: "change only what the goal needs"; reviewer checks the diff size |
| Edits outside `paths` | A needed fix in a file another task owns | Prompt: "send a message to the lead instead of editing it"; the hooks warn, the fence (when on) denies |
| Fake results | Tests weakened or skipped, `checks_run` claims what did not run | Acceptance commands that fail without the work; prompt: "never skip, delete or weaken a test"; the refinery runs `check_cmd` again |
| Protected paths | CI config, release files, permission settings, ownership files | Keep them out of `paths`, or say in the PR body that the user must approve at merge. A worker never asks for approval itself |
| Large or generated files | Lockfiles, fixtures, snapshots, binaries | Regenerate with a command, never edit by hand; list the command in the prompt; raise `budget_usd` |
| Secrets | Tokens in env, config, logs | Prompt: "never print or commit a secret"; acceptance needs no secret |
| Provider rate limits | Many tasks on one provider, long tasks | `provider_fallback`; `fallback_model`; fewer parallel tasks on that provider |
| Non-Anthropic model quirks | A profile that maps to another vendor's model | Smaller tasks, explicit file lists, strict acceptance commands, lower `autocompact_window`; do not rely on extended thinking |
| Tool or host gaps | A tool missing on a host (docker, a compiler) | `host_needs`; an acceptance command that checks the tool first |
| Stale base | The task needs a change that another task in the wave makes | `needs`; or `shared_paths` and an order note in the prompt |
| Merge conflicts | Two tasks edit the same file region | Disjoint `paths`; one owner for shared files; `needs` for order |
| Lost work | Session ends before commit or push | Prompt: "commit after each green step; push before you end"; the Stop hook blocks once on unpushed work |
