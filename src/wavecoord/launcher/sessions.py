"""Claude Code sessions of an attempt: where a transcript is, and which session a --continue run resumes.

Claude Code keeps the transcript of a session in
`<config dir>/projects/<encoded cwd>/<session id>.jsonl`. The config dir is $CLAUDE_CONFIG_DIR, else
`~/.claude`; the encoded cwd is the absolute path with every character that is not a letter or a
digit replaced by `-` (`/home/u/wt/t-1` -> `-home-u-wt-t-1`). `--resume <id>` keeps writing into
that file under the same id (only `--fork-session` makes a new id), so the first run's id is the
one that a later --continue run must resume. A --resume of an id with no transcript fails with
"No conversation found with session ID: <id>" and does nothing.
"""

import glob
import os
import re

from .common import read_json

NO_CONVERSATION = "No conversation found"


def config_dir(env=None):
    env = os.environ if env is None else env
    if env.get("CLAUDE_CONFIG_DIR"):
        return env["CLAUDE_CONFIG_DIR"]
    return os.path.join(env.get("HOME") or os.path.expanduser("~"), ".claude")


def encode_cwd(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def transcript_path(session_id, cwd, env=None):
    """The transcript file of session_id (for a run in cwd), or None when there is none."""
    if not session_id or not re.match(r"^[A-Za-z0-9-]+$", session_id):
        return None
    projects = os.path.join(config_dir(env), "projects")
    seen = []
    for p in (cwd, os.path.realpath(cwd) if cwd else None):
        if p and p not in seen:
            seen.append(p)
            f = os.path.join(projects, encode_cwd(p), session_id + ".jsonl")
            if os.path.isfile(f):
                return f
    # a long path is shortened by Claude Code; `--resume <id>` also finds other projects
    hits = sorted(glob.glob(os.path.join(projects, "*", session_id + ".jsonl")))
    return hits[0] if hits else None


def _uniq(ids):
    out = []
    for i in ids:
        if i and isinstance(i, str) and i not in out:
            out.append(i)
    return out


def archives(d):
    """The prev-<ms>/ dirs of an attempt dir, oldest first."""
    names = [n for n in os.listdir(d) if n.startswith("prev-") and os.path.isdir(os.path.join(d, n))] \
        if os.path.isdir(d) else []

    def key(n):
        try:
            return int(n[5:])
        except ValueError:
            return 0
    return [os.path.join(d, n) for n in sorted(names, key=key)]


def lineage(d, prev_task, prev_result):
    """The session ids of the attempt's earlier runs, in the order a --continue run tries them.

    First the session whose transcript the last run wrote (`transcript_session`), then the
    lineage root (the first run's id), then the ids of every earlier run, oldest first (for
    attempts of wavecoord 0.1.4 and older, which recorded only a fresh id per run), and last the
    ids that the results reported."""
    ids = [prev_task.get("transcript_session"), prev_task.get("session_root")]
    ids += list(prev_task.get("session_lineage") or [])
    results = []
    for a in archives(d):
        t = read_json(os.path.join(a, "task.json"), {}) or {}
        ids += [t.get("transcript_session"), t.get("session_root"), t.get("resume"), t.get("session_id")]
        results.append((read_json(os.path.join(a, "result.json"), {}) or {}).get("session_id"))
    ids += [prev_task.get("resume"), prev_task.get("session_id")]
    ids += results + [prev_result.get("session_id")]
    return _uniq(ids)


def first_prompt(d, prev_task):
    """The full prompt of the attempt's first run (a fresh session after a lost one needs it)."""
    if prev_task.get("spec_prompt"):
        return prev_task["spec_prompt"]
    for a in archives(d):
        t = read_json(os.path.join(a, "task.json"), {}) or {}
        if t.get("spec_prompt"):
            return t["spec_prompt"]
        try:
            with open(os.path.join(a, "prompt.md"), encoding="utf-8") as f:
                return f.read()
        except OSError:
            continue
    return None


def no_conversation(stderr_text, result):
    """True when claude says that the resumed session does not exist."""
    if NO_CONVERSATION in (stderr_text or ""):
        return True
    r = result or {}
    text = " ".join(str(x) for x in ([r.get("result")] + list(r.get("errors") or [])) if x)
    return NO_CONVERSATION in text


def fresh_prompt(spec_prompt, continue_prompt, commits, lost):
    """The prompt of a fresh session that replaces a --continue run whose session is gone."""
    lines = ["# Note from wavecoord: this run continues the attempt in a new session",
             "",
             "The Claude Code session of the earlier runs could not be resumed (%s). This is a new" % lost,
             "session in the same worktree and branch. The work of the earlier runs is in the branch."]
    if commits:
        lines += ["", "Commits on the branch since the base (oldest first):", ""]
        lines += ["- %s" % c for c in commits[:50]]
        if len(commits) > 50:
            lines.append("- ... and %d more" % (len(commits) - 50))
    else:
        lines += ["", "The branch has no commits since the base."]
    lines += ["", "Read the branch state (git log, git diff against the base) before you change anything."]
    parts = ["\n".join(lines)]
    sp = (spec_prompt or "").strip()
    cp = (continue_prompt or "").strip()
    if sp:
        parts.append("# The task (the prompt of the first run)\n\n" + sp)
    if cp and cp != sp:
        parts.append("# The continue prompt\n\n" + cp)
    return "\n\n".join(parts) + "\n"
