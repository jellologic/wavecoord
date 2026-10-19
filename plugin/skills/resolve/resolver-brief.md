<!-- Template, not an agent. The resolve skill fills it in and passes it as the `brief` of the `resolve` tool.
     The server adds the rest (files, both intents, acceptance, steps). It lives here, not in agents/, because Claude
     Code loads every .md file in agents/ as an agent. -->

Conflict <conflict id>: <task>/<n> on <branch>, against <default branch>.

Decided by: <the user, in the lead session>

- Where the sides disagree, keep: <the behaviour that wins, per file or hunk>.
- From <default branch>, keep: <what the other side must not lose>.
- From this branch, keep: <what this task must not lose>.
- Mechanical overlaps (lists, imports, a changelog): keep both.

If this does not cover a hunk, do not guess: finish with report status `needs_decision` and name the hunk.
