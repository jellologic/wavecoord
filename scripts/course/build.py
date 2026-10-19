#!/usr/bin/env python3
"""Build the wavecoord course videos: one lesson, or all of them.

    python3 scripts/course/build.py --validate              # check every lesson script; records nothing
    python3 scripts/course/build.py --check                 # check the prerequisites of a recording
    python3 scripts/course/build.py --lesson 03             # record lesson 03
    python3 scripts/course/build.py --all                   # record every lesson, in order
    python3 scripts/course/build.py --lesson 00 --cards-only    # only the title and diagram cards

For each lesson (`scripts/course/lessons/NN-slug.json`) it:

1. checks the prerequisites (macOS `say` and Terminal, ffmpeg, rsvg-convert, a logged-in course profile, a
   free port);
2. builds the lesson's demo repo under /tmp/wavecoord-demo/ with a local bare origin (demos/demos.py);
3. resets the course state ($COURSE_HOME, default ~/.wavecoord-course): an isolated WAVECOORD_HOME with fresh
   tokens, the plugin options of the course profile (CLAUDE_CONFIG_DIR=$COURSE_HOME/claude), and a
   coordinator on its own port (18793) when the lesson needs one;
4. resolves the `${COURSE_*}` placeholders and runs the rig (rig.py), which records a real Claude Code
   session in a Terminal window and adds the narration;
5. renders the lesson's cards (titles, bullets, diagrams) with their narration, cuts the long waits marked
   `"cut": true` down to a short "later" card, joins everything and writes
   dist/course/course-NN-slug.mp4, its .srt and the token usage (lead and workers) as .usage.json.

The extra lesson keys this script reads (the rig ignores them): `slug`, `demo`, `plugin`, `server`,
`server_env`, `cards`, `estimate`; and on a step, `cut`. Stdlib only, Python >= 3.9.
"""

import argparse
import base64
import glob
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
LESSONS_DIR = os.path.join(HERE, "lessons")
DIST = os.path.join(ROOT, "dist", "course")
WAVECOORD = os.path.join(ROOT, "plugin", "bin", "wavecoord")
PLUGIN_DIR = os.path.join(ROOT, "plugin")
FORGE_BIN = os.path.join(HERE, "demos", "bin")
DEMO_ROOT = "/tmp/wavecoord-demo"
PORT = int(os.environ.get("COURSE_PORT", "18793"))
COURSE_HOME = os.path.expanduser(os.environ.get("COURSE_HOME", "~/.wavecoord-course"))
VIDEO_URL = "https://github.com/jellologic/wavecoord/releases/download/v0.3.0/course-%s.mp4"
CLI_URL = "https://raw.githubusercontent.com/jellologic/wavecoord/v0.3.0/plugin/bin/wavecoord"
MERMAID_CLI = "@mermaid-js/mermaid-cli@11.4.0"

BUILD_KEYS = ("slug", "demo", "plugin", "server", "server_env", "cards", "estimate")
STEP_KEYS = ("cut",)
CUT_KINDS = ("wait_for", "idle", "wait_shell", "pause")
CUT_HEAD, CUT_TAIL, CUT_MIN, LATER_S = 2.5, 2.0, 8.0, 1.6
CARD_LEAD, CARD_TAIL, CARD_MIN = 0.6, 1.2, 3.0
W, H = 1920, 1080
BG, FG, DIM, ACCENT, BOX, LINE = "#0d0f12", "#e6e6e6", "#9399b2", "#a6e3a1", "#1e2230", "#89b4fa"
FONT = "Helvetica Neue, Helvetica, Arial, sans-serif"
MONO = "Menlo, ui-monospace, monospace"


def load_rig():
    if "course_rig" in sys.modules:
        return sys.modules["course_rig"]
    spec = importlib.util.spec_from_file_location("course_rig", os.path.join(HERE, "rig.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["course_rig"] = mod
    spec.loader.exec_module(mod)
    return mod


def load_demos():
    if "course_demos" in sys.modules:
        return sys.modules["course_demos"]
    spec = importlib.util.spec_from_file_location("course_demos", os.path.join(HERE, "demos", "demos.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["course_demos"] = mod
    spec.loader.exec_module(mod)
    return mod


RIG = load_rig()


# ---------------------------------------------------------------- lessons and placeholders

def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def read_json(path):
    return json.loads(read_text(path))


def lesson_files():
    return sorted(glob.glob(os.path.join(LESSONS_DIR, "[0-9][0-9]-*.json")))


def lesson_id(path):
    return os.path.splitext(os.path.basename(path))[0]


def find_lesson(nn):
    for p in lesson_files():
        if lesson_id(p).startswith("%02d-" % int(nn)):
            return p
    raise SystemExit("no lesson %s in %s" % (nn, LESSONS_DIR))


def course_paths(home=COURSE_HOME):
    return {"home": home, "claude": os.path.join(home, "claude"), "wc": os.path.join(home, "wc")}


def demo_dir(lesson):
    demos = load_demos()
    name = (lesson.get("demo") or {}).get("name", "fresh")
    return os.path.join(DEMO_ROOT, demos.DEMOS.get(name, ("demo",))[0])


def placeholders(lesson, home=COURSE_HOME, port=PORT):
    cp = course_paths(home)
    return {
        "COURSE_REPO": ROOT,
        "COURSE_HOME": home,
        "CLAUDE_CONFIG_DIR": cp["claude"],
        "WAVECOORD_HOME": cp["wc"],
        "COURSE_DEMO_DIR": demo_dir(lesson),
        "COURSE_PLUGIN": PLUGIN_DIR,
        "COURSE_PORT": str(port),
        "COURSE_URL": "http://127.0.0.1:%d" % port,
        "COURSE_WAIT": "%s %s" % (sys.executable, os.path.join(HERE, "wait.py")),
        "COURSE_BUILD": "%s %s" % (sys.executable, os.path.abspath(__file__)),
    }


PLACEHOLDER_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def substitute(obj, values, missing=None):
    """Replace ${NAME} in every string of obj (recursively). Unknown names stay and go into `missing`."""
    if isinstance(obj, str):
        def rep(m):
            if m.group(1) in values:
                return values[m.group(1)]
            if missing is not None:
                missing.add(m.group(1))
            return m.group(0)
        return PLACEHOLDER_RE.sub(rep, obj)
    if isinstance(obj, list):
        return [substitute(x, values, missing) for x in obj]
    if isinstance(obj, dict):
        return {k: substitute(v, values, missing) for k, v in obj.items()}
    return obj


def base_path(lesson):
    """PATH for the lesson: the course forge as `gh`, then (except for the install lesson) this repo's wavecoord,
    then ~/.local/bin and the directory of claude, then the PATH of this shell without CLI shims."""
    first = [FORGE_BIN]
    if lesson.get("plugin") != "marketplace":
        first.append(os.path.dirname(WAVECOORD))
    first.append(os.path.expanduser("~/.local/bin"))
    claude = RIG.find_claude()
    if os.path.isabs(claude):
        first.append(os.path.dirname(claude))
    rest = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and "cli-shims" not in d]
    out = []
    for d in first + rest + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]:
        if d not in out:
            out.append(d)
    return os.pathsep.join(out)


def lesson_env(lesson, home=COURSE_HOME, port=PORT):
    cp = course_paths(home)
    env = {
        "CLAUDE_CONFIG_DIR": cp["claude"],
        "WAVECOORD_HOME": cp["wc"],
        "WAVECOORD_TOKEN_FILE": os.path.join(cp["wc"], "token"),
        "WAVECOORD_TOKENS_FILE": os.path.join(cp["wc"], "tokens.json"),
        "WAVECOORD_BIN": WAVECOORD,
        "PATH": base_path(lesson),
        "PS1": "$ ",
        "HISTFILE": "/dev/null",
        "BASH_SILENCE_DEPRECATION_WARNING": "1",
        "COURSE_DEMO_DIR": demo_dir(lesson),
        "COURSE_HOME": home,
        "COURSE_PORT": str(port),
    }
    if lesson.get("plugin") != "marketplace":
        env["WAVECOORD_ROLE"] = "lead"
    env.update(lesson.get("env") or {})
    return env


def resolve(lesson, home=COURSE_HOME, port=PORT):
    """-> (the lesson as the rig gets it, the set of unknown placeholders)."""
    missing = set()
    values = placeholders(lesson, home, port)
    out = substitute(lesson, values, missing)
    out["env"] = substitute(lesson_env(lesson, home, port), values, missing)
    out["cwd"] = values["COURSE_DEMO_DIR"]
    out["timeout"] = lesson.get("timeout", 900)
    return out, missing


# ---------------------------------------------------------------- validation

def skill_names():
    d = os.path.join(PLUGIN_DIR, "skills")
    return sorted(x for x in os.listdir(d) if os.path.isfile(os.path.join(d, x, "SKILL.md")))


def narration(lesson):
    """Every narrated line of a lesson, cards first."""
    lines = []
    cards = lesson.get("cards") or {}
    for part in ("intro", "outro"):
        lines += [c.get("say", "") for c in cards.get(part, []) if isinstance(c, dict)]
    lines += [s["say"] for s in lesson.get("steps", []) if isinstance(s, dict) and isinstance(s.get("say"), str)]
    return [l for l in lines if l]


def speech_seconds(text, wpm=180):
    return len(text.split()) * 60.0 / wpm + 0.3


def estimate(lesson):
    """Seconds of narration and typing: the least a lesson's video lasts."""
    rate = (lesson.get("config") or {}).get("rate", 180)
    cps = (lesson.get("config") or {}).get("type_cps", 16)
    talk = sum(speech_seconds(t, rate) for t in narration(lesson))
    typing = sum(len(s["type"]) / float(s.get("cps", cps)) + 0.3 for s in lesson.get("steps", [])
                 if isinstance(s, dict) and isinstance(s.get("type"), str))
    pauses = sum(s.get("pause", 0) for s in lesson.get("steps", []) if isinstance(s, dict) and "pause" in s)
    return talk + typing + pauses


FORGE_SKILLS = ("spec", "spec-import", "wave-plan", "wave-run", "triage", "resolve", "revise")


def uses_forge(lesson):
    """Whether a lesson opens or reads pull requests (the course forge stands in for GitHub)."""
    for st in lesson.get("steps") or []:
        t = st.get("type") if isinstance(st, dict) else None
        if not isinstance(t, str):
            continue
        m = re.match(r"^/wavecoord:([a-z-]+)", t)
        if (m and m.group(1) in FORGE_SKILLS) or re.search(r"(^|[!&|;]\s*)gh |--pr\b", t):
            return True
    return False


def video_seconds(lesson):
    """A rough length of the finished video: narration, typing and pauses, plus what each wait shows."""
    total = estimate(lesson)
    for st in lesson.get("steps") or []:
        act = RIG.step_action(st) if isinstance(st, dict) else None
        if act not in ("wait_for", "idle", "wait_shell"):
            continue
        if st.get("cut"):
            shown = CUT_HEAD + CUT_TAIL + LATER_S
        elif st.get("timeout", 0) >= 300 and act != "wait_shell":
            shown = 40.0        # a Claude turn shown whole, such as a dispatch
        else:
            shown = 3.0
        total += shown + st.get("idle", 0)
    cards = lesson.get("cards") or {}
    total += (CARD_LEAD + CARD_TAIL) * sum(len(cards.get(p, [])) for p in ("intro", "outro"))
    return total


def validate(path):
    """-> a list of problems with one lesson file (empty: the rig can play it and the build can cut it)."""
    demos = load_demos()
    errs = []
    lid = lesson_id(path)
    if not re.match(r"^\d\d-[a-z0-9-]+$", lid):
        errs.append("file name must be NN-slug.json")
    try:
        with open(path) as f:
            lesson = json.load(f)
    except ValueError as e:
        return ["not JSON: %s" % e]
    if lesson.get("slug") != lid:
        errs.append("slug must be %r (the file name)" % lid)
    demo = lesson.get("demo") or {}
    if demo.get("name") not in demos.DEMOS:
        errs.append("demo.name must be one of %s" % ", ".join(sorted(demos.DEMOS)))
    elif demo.get("state") and demo["state"] not in demos.DEMOS[demo["name"]][2]:
        errs.append("demo %s has no state %r" % (demo["name"], demo["state"]))
    if lesson.get("plugin") not in ("dir", "marketplace"):
        errs.append("plugin must be dir or marketplace")
    resolved, missing = resolve(lesson)
    for m in sorted(missing):
        errs.append("unknown placeholder ${%s}" % m)
    errs += RIG.check_lesson(resolved, extra_keys=BUILD_KEYS, extra_step_keys=STEP_KEYS)
    steps = lesson.get("steps") or []
    for n, st in enumerate(steps):
        if not isinstance(st, dict):
            continue
        if st.get("cut") and RIG.step_action(st) not in CUT_KINDS:
            errs.append("steps[%d]: cut is only for a wait (%s)" % (n, ", ".join(CUT_KINDS)))
        t = st.get("type")
        if isinstance(t, str):
            m = re.match(r"^/wavecoord:([a-z-]+)", t)
            if m and m.group(1) not in skill_names():
                errs.append("steps[%d]: no skill %s in plugin/skills" % (n, m.group(1)))
            if t.startswith("/") and not re.match(r"^/(wavecoord:[a-z-]*|plugin|exit|help)(\s|$)", t):
                errs.append("steps[%d]: unexpected slash command %r" % (n, t.split()[0]))
        if RIG.step_action(st) == "wait_for":
            typed = [s.get("type", "") for s in steps[:n] if isinstance(s, dict) and "type" in s]
            if typed and isinstance(st.get("wait_for"), str) and re.search(st["wait_for"], typed[-1]):
                errs.append("steps[%d]: wait_for %r matches the text typed just before it (the echo)"
                            % (n, st["wait_for"]))
        if RIG.step_action(st) in ("wait_for", "idle", "wait_shell") and "timeout" not in st:
            errs.append("steps[%d]: give every wait a timeout" % n)
    if not steps or RIG.step_action(steps[-1] if isinstance(steps[-1], dict) else {}) != "wait_exit":
        errs.append("the last step must be wait_exit")
    cmd = lesson.get("command") or []
    if cmd[:1] == ["claude"]:
        setup = lesson.get("setup") or []
        if not setup or not re.search("trust", json.dumps(setup)):
            errs.append("setup must answer the folder-trust dialog (a when-guarded step)")
        if "--permission-mode" not in cmd:
            errs.append("start claude with --permission-mode default (README.md)")
        if lesson.get("plugin") == "dir" and "--plugin-dir" not in cmd:
            errs.append("plugin dir: start claude with --plugin-dir ${COURSE_PLUGIN}")
    text = " ".join(narration(lesson)).lower()
    if uses_forge(lesson) and "stand-in for github" not in text:
        errs.append("say once that the demo uses a local stand-in for GitHub")
    for part in ("intro", "outro"):
        for k, c in enumerate((lesson.get("cards") or {}).get(part, [])):
            where = "cards.%s[%d]" % (part, k)
            if not isinstance(c, dict) or not isinstance(c.get("title"), str) or not c.get("say"):
                errs.append("%s: a card needs a title and a say" % where)
                continue
            for key in c:
                if key not in ("title", "subtitle", "bullets", "diagram", "code", "say"):
                    errs.append("%s: unknown key %r" % (where, key))
            if c.get("diagram") and c["diagram"] not in DIAGRAMS:
                errs.append("%s: no diagram %r (one of %s)" % (where, c["diagram"], ", ".join(sorted(DIAGRAMS))))
    est = lesson.get("estimate") or {}
    if not isinstance(est.get("video_min"), (int, float)):
        errs.append("estimate.video_min is missing")
    elif abs(video_seconds(lesson) / 60.0 - est["video_min"]) > 2:
        errs.append("estimate.video_min %s is far from the computed %.1f min" % (est["video_min"],
                                                                                  video_seconds(lesson) / 60.0))
    return errs


# ---------------------------------------------------------------- cuts and subtitles

def plan_cuts(rows, steps, clips, head=CUT_HEAD, tail=CUT_TAIL, min_cut=CUT_MIN):
    """The intervals of the recording to cut: the middle of each wait step marked "cut", never over a voice.

    rows: the rig's timeline steps ({"i", "kind", "at", "end"}); steps: the lesson steps (by index i);
    clips: [(start, duration)] of the narration. -> sorted, merged [(a, b)]."""
    cuts = []
    for r in rows:
        i = r.get("i", -1)
        if i < 0 or i >= len(steps) or not steps[i].get("cut") or r.get("kind") not in CUT_KINDS:
            continue
        a, b = r["at"] + head, r["end"] - tail
        for start, dur in clips:
            end = start + dur
            if start <= a < end + 0.5:
                a = end + 0.5
            elif a < start < b:
                b = min(b, start - 0.5)
        if b - a >= min_cut:
            cuts.append((round(a, 3), round(b, 3)))
    merged = []
    for a, b in sorted(cuts):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def pieces(duration, cuts, later=LATER_S):
    """The recording as pieces: [("rig", a, b, dst) or ("later", seconds_cut, None, dst)], dst = start in the
    output. Each cut becomes a short 'later' card."""
    out, t, dst = [], 0.0, 0.0
    for a, b in cuts:
        if a > t:
            out.append(("rig", t, a, round(dst, 3)))
            dst += a - t
        out.append(("later", round(b - a, 3), None, round(dst, 3)))
        dst += later
        t = b
    if duration > t:
        out.append(("rig", t, duration, round(dst, 3)))
    return out


def pieces_duration(parts, later=LATER_S):
    if not parts:
        return 0.0
    kind, a, b, dst = parts[-1]
    return round(dst + (b - a if kind == "rig" else later), 3)


def map_time(t, parts):
    """A time of the recording -> its time in the cut output, or None when it falls in a cut."""
    for kind, a, b, dst in parts:
        if kind == "rig" and a <= t <= b:
            return round(dst + t - a, 3)
    return None


def later_text(seconds):
    if seconds < 90:
        return "%d seconds later" % int(round(seconds))
    return "%d minutes later" % int(round(seconds / 60.0))


def parse_srt(text):
    """SubRip text -> [(start, end, text)]."""
    def secs(s):
        h, m, rest = s.strip().split(":")
        sec, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000.0
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.strip().splitlines()
        if len(lines) >= 3 and "-->" in lines[1]:
            a, b = lines[1].split("-->")
            cues.append((secs(a), secs(b), "\n".join(lines[2:])))
    return cues


def write_srt(cues, path):
    with open(path, "w") as f:
        for n, (a, b, text) in enumerate(cues, 1):
            f.write("%d\n%s --> %s\n%s\n\n" % (n, RIG.srt_time(a), RIG.srt_time(b), text))


def shift_cues(cues, parts, offset):
    """Recording cues -> output cues: mapped through the cuts and moved by `offset`; cues in a cut are dropped."""
    out = []
    for a, b, text in cues:
        ma, mb = map_time(a, parts), map_time(b, parts)
        if ma is None:
            continue
        if mb is None:
            mb = ma + (b - a)
        out.append((round(offset + ma, 3), round(offset + mb, 3), text))
    return out


# ---------------------------------------------------------------- cards and diagrams

def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# Each diagram: the Mermaid source (rendered with mmdc when available) and a layout drawn in Python when not:
# rows of [node id, label] from top to bottom, and edges (from, to, label).
DIAGRAMS = {
    "tree": {
        "mermaid": 'flowchart TD\n  cap["capability: todo"] --> feat["feature: todo/due-dates"]\n'
                   '  feat --> r1["requirement: store-due<br/>Statement, Scenarios, Verify"]\n'
                   '  feat --> r2["requirement: overdue"]\n  r1 --> t1["task: store<br/>paths, exec, Acceptance, Prompt"]\n'
                   '  r2 --> t2["task: dates"]\n  r2 --> t3["task: cli"]\n  t3 -.->|needs| t1\n  t3 -.->|needs| t2\n',
        "rows": [[("cap", "capability: todo")], [("feat", "feature: todo/due-dates")],
                 [("r1", "requirement: store-due"), ("r2", "requirement: overdue")],
                 [("t1", "task: store"), ("t2", "task: dates"), ("t3", "task: cli")]],
        "edges": [("cap", "feat", ""), ("feat", "r1", ""), ("feat", "r2", ""), ("r1", "t1", ""), ("r2", "t2", ""),
                  ("r2", "t3", "")],
    },
    "loop": {
        "mermaid": 'flowchart LR\n  idea(["an idea"]) --> spec["/wavecoord:spec<br/>requirements"]\n'
                   '  spec --> plan["/wavecoord:wave-plan<br/>tasks"]\n  plan --> run["/wavecoord:wave-run<br/>workers"]\n'
                   '  run --> triage["/wavecoord:triage<br/>PRs, merges"]\n  triage -->|next wave| run\n'
                   '  triage -->|proposals| revise["/wavecoord:revise"]\n  revise --> plan\n',
        "rows": [[("idea", "an idea")], [("spec", "/wavecoord:spec  (requirements, plan PR)")],
                 [("plan", "/wavecoord:wave-plan  (tasks, plan PR)")], [("run", "/wavecoord:wave-run  (workers)")],
                 [("triage", "/wavecoord:triage  (PRs, merges)"), ("revise", "/wavecoord:revise")]],
        "edges": [("idea", "spec", ""), ("spec", "plan", "you merge"), ("plan", "run", "you merge"),
                  ("run", "triage", "reports"), ("triage", "revise", "proposals")],
    },
    "waves": {
        "mermaid": 'flowchart LR\n  subgraph w1["Wave 1"]\n    a["store"]\n    b["dates"]\n  end\n'
                   '  subgraph w2["Wave 2"]\n    c["cli"]\n  end\n  a -->|needs| c\n  b -->|needs| c\n',
        "rows": [[("a", "wave 1: store  (todo/store.py)"), ("b", "wave 1: dates  (todo/dates.py)")],
                 [("c", "wave 2: cli  (todo/cli.py)")]],
        "edges": [("a", "c", "needs"), ("b", "c", "needs")],
    },
    "lead-workers": {
        "mermaid": 'flowchart LR\n  you(["You"]) --> lead["Lead session<br/>Claude Code + plugin"]\n'
                   '  lead <--> coord["Coordinator"]\n  coord --> w1["worker 1<br/>own worktree"]\n'
                   '  coord --> w2["worker 2<br/>own worktree"]\n  coord --> git[("git: specs, coord/log")]\n',
        "rows": [[("you", "You")], [("lead", "Lead: your Claude Code session")],
                 [("coord", "Coordinator (wavecoord serve)"), ("git", "git: specs, code, coord/log")],
                 [("w1", "worker 1: own worktree"), ("w2", "worker 2: own worktree")]],
        "edges": [("you", "lead", ""), ("lead", "coord", "MCP tools"), ("coord", "git", "every event"),
                  ("coord", "w1", "lease"), ("coord", "w2", "lease")],
    },
    "fence": {
        "mermaid": 'flowchart LR\n  e1["E1 edit hook"] --> e2["E2 pre-commit"] --> e3["E3 push check"]'
                   ' --> e4["E4 report"] --> e5["E5 gate-check, refinery"]\n',
        "rows": [[("e1", "E1 edit hook"), ("e2", "E2 pre-commit"), ("e3", "E3 push check")],
                 [("e4", "E4 changed paths in the report"), ("e5", "E5 gate-check and refinery")]],
        "edges": [("e1", "e2", ""), ("e2", "e3", ""), ("e3", "e4", ""), ("e4", "e5", "")],
    },
    "refinery": {
        "mermaid": 'flowchart TD\n  pr["PR of a done report"] --> mt["merge-tree clean?"] --> prot["protected paths?"]'
                   ' --> fence["fence: Lease, paths, config, specs"] --> check["check_cmd"]'
                   ' --> verify["Acceptance and Verify"] --> merge["merge_cmd at the checked sha"]\n',
        "rows": [[("pr", "PR of a done report"), ("mt", "merge-tree: still clean?"), ("prot", "protected paths?")],
                 [("fence", "fence: lease, config, specs"), ("check", "check_cmd"), ("verify", "Acceptance, Verify")],
                 [("merge", "merge_cmd, pinned to the checked sha")]],
        "edges": [("pr", "mt", ""), ("mt", "prot", ""), ("prot", "fence", ""), ("fence", "check", ""),
                  ("check", "verify", ""), ("verify", "merge", "")],
    },
}


def diagram_svg_group(name, x0=110, y0=250, width=1700, height=760):
    """A diagram drawn in Python: boxes per row, arrows between them. -> SVG elements (no <svg> wrapper)."""
    d = DIAGRAMS[name]
    rows = d["rows"]
    pos, out = {}, []
    row_h = height / float(len(rows))
    for r, row in enumerate(rows):
        n = len(row)
        bw = min(520, (width - 60 * (n - 1)) / float(n))
        bh = min(110, row_h * 0.55)
        total = n * bw + (n - 1) * 60
        y = y0 + r * row_h + (row_h - bh) / 2
        for k, (nid, label) in enumerate(row):
            x = x0 + (width - total) / 2 + k * (bw + 60)
            pos[nid] = (x, y, bw, bh)
            size = 30 if len(label) < 30 else 25
            out.append('<rect x="%.0f" y="%.0f" width="%.0f" height="%.0f" rx="18" fill="%s" stroke="%s" '
                       'stroke-width="3"/>' % (x, y, bw, bh, BOX, LINE))
            out.append('<text x="%.0f" y="%.0f" fill="%s" font-family="%s" font-size="%d" text-anchor="middle" '
                       'dominant-baseline="middle">%s</text>' % (x + bw / 2, y + bh / 2, FG, esc(FONT), size,
                                                                 esc(label)))
    for a, b, label in d["edges"]:
        ax, ay, aw, ah = pos[a]
        bx, by, bw, bh = pos[b]
        if abs(ay - by) < 1:                           # same row: side to side
            x1, y1, x2, y2 = ax + aw, ay + ah / 2, bx, by + bh / 2
            if bx < ax:
                x1, x2 = ax, bx + bw
        else:
            x1, y1, x2, y2 = ax + aw / 2, ay + ah, bx + bw / 2, by
        out.append('<line x1="%.0f" y1="%.0f" x2="%.0f" y2="%.0f" stroke="%s" stroke-width="3" '
                   'marker-end="url(#arrow)"/>' % (x1, y1, x2, y2, DIM))
        if label:
            lx, ly, anchor = ((x1 + x2) / 2, y1 - 40, "middle") if abs(y1 - y2) < 1 else \
                ((x1 + x2) / 2 + 14, (y1 + y2) / 2 + 8, "start")
            out.append('<text x="%.0f" y="%.0f" fill="%s" font-family="%s" font-size="24" text-anchor="%s">%s'
                       '</text>' % (lx, ly, ACCENT, esc(FONT), anchor, esc(label)))
    return out


def card_svg(card, lesson_title="", image=None):
    """One 1920x1080 card: a title, then bullets, a code block, a diagram or an image (a data URI)."""
    out = ['<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="%d" '
           'height="%d" viewBox="0 0 %d %d">' % (W, H, W, H),
           '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" '
           'orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="%s"/></marker></defs>' % DIM,
           '<rect width="%d" height="%d" fill="%s"/>' % (W, H, BG)]
    if lesson_title:
        out.append('<text x="110" y="90" fill="%s" font-family="%s" font-size="28">%s</text>'
                   % (DIM, esc(FONT), esc(lesson_title)))
    body = card.get("bullets") or card.get("code") or card.get("diagram") or image
    ty = 200 if body else 500
    out.append('<text x="%d" y="%d" fill="%s" font-family="%s" font-size="%d" font-weight="bold" '
               'text-anchor="%s">%s</text>' % (110 if body else W // 2, ty, FG, esc(FONT), 64 if body else 80,
                                               "start" if body else "middle", esc(card["title"])))
    if card.get("subtitle"):
        out.append('<text x="%d" y="%d" fill="%s" font-family="%s" font-size="36" text-anchor="%s">%s</text>'
                   % (110 if body else W // 2, ty + 70, ACCENT, esc(FONT), "start" if body else "middle",
                      esc(card["subtitle"])))
    y = ty + (150 if card.get("subtitle") else 110)
    for b in card.get("bullets") or []:
        lines = wrap(b, 62)
        for k, line in enumerate(lines):
            if k == 0:
                out.append('<text x="150" y="%d" fill="%s" font-family="%s" font-size="40">•</text>'
                           % (y, ACCENT, esc(FONT)))
            out.append('<text x="190" y="%d" fill="%s" font-family="%s" font-size="40">%s</text>'
                       % (y, FG, esc(FONT), esc(line)))
            y += 56
        y += 18
    if card.get("code"):
        lines = card["code"].splitlines()
        out.append('<rect x="110" y="%d" width="1700" height="%d" rx="14" fill="%s"/>'
                   % (y - 50, 60 * len(lines) + 50, BOX))
        for line in lines:
            out.append('<text x="150" y="%d" fill="%s" font-family="%s" font-size="34" xml:space="preserve">%s'
                       '</text>' % (y, ACCENT, esc(MONO), esc(line)))
            y += 60
    if image:
        out.append('<image x="110" y="%d" width="1700" height="%d" preserveAspectRatio="xMidYMid meet" '
                   'xlink:href="%s"/>' % (y - 20, H - y - 40, image))
    elif card.get("diagram"):
        out += diagram_svg_group(card["diagram"], y0=y - 20, height=H - y - 20)
    out.append("</svg>")
    return "\n".join(out) + "\n"


def later_svg(text):
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">'
            '<rect width="%d" height="%d" fill="%s"/><text x="%d" y="%d" fill="%s" font-family="%s" font-size="64" '
            'text-anchor="middle">%s</text></svg>\n' % (W, H, W, H, W, H, BG, W // 2, H // 2, DIM, esc(FONT),
                                                       esc("… " + text)))


def wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for wd in words:
        if cur and len(cur) + 1 + len(wd) > width:
            lines.append(cur)
            cur = wd
        else:
            cur = (cur + " " + wd).strip()
    if cur:
        lines.append(cur)
    return lines


def mermaid_png(name, work, use_npx=True):
    """Render a diagram's Mermaid source to PNG with mmdc (or npx), or None when no renderer works."""
    src = os.path.join(work, "%s.mmd" % name)
    png = os.path.join(work, "%s.png" % name)
    with open(src, "w") as f:
        f.write(DIAGRAMS[name]["mermaid"])
    cmds = []
    if shutil.which("mmdc"):
        cmds.append(["mmdc"])
    if use_npx and shutil.which("npx"):
        cmds.append(["npx", "-y", MERMAID_CLI])
    for c in cmds:
        try:
            p = subprocess.run(c + ["-i", src, "-o", png, "-t", "dark", "-b", "transparent", "-w", "1700",
                                    "-s", "2"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if p.returncode == 0 and os.path.isfile(png):
            return png
    return None


# ---------------------------------------------------------------- media

def run(cmd, **kw):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, **kw)
    if p.returncode != 0:
        raise SystemExit("course: %s failed (exit %d):\n%s" % (cmd[0], p.returncode, (p.stderr or p.stdout)[-2000:]))
    return p.stdout


ENC = ["-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p", "-r", "30",
       "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]


def svg_to_png(svg, path):
    with open(path[:-4] + ".svg", "w") as f:
        f.write(svg)
    run(["rsvg-convert", "-w", str(W), "-h", str(H), "-o", path, path[:-4] + ".svg"])
    return path


def still_segment(png, voice, dur, path):
    """A still image for `dur` seconds, with the voice (or silence) starting after CARD_LEAD."""
    cmd = ["ffmpeg", "-hide_banner", "-y", "-loop", "1", "-framerate", "30", "-t", "%.3f" % dur, "-i", png]
    if voice:
        cmd += ["-i", voice, "-filter_complex",
                "[1:a]aresample=48000,aformat=channel_layouts=stereo,adelay=%d:all=1,apad[a]" % int(CARD_LEAD * 1000),
                "-map", "0:v", "-map", "[a]"]
    else:
        cmd += ["-f", "lavfi", "-t", "%.3f" % dur, "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v", "-map", "1:a"]
    run(cmd + ["-tune", "stillimage"] + ENC + ["-t", "%.3f" % dur, path])
    return path


def cut_piece(src, a, b, path):
    run(["ffmpeg", "-hide_banner", "-y", "-ss", "%.3f" % a, "-to", "%.3f" % b, "-i", src, "-map", "0:v",
         "-map", "0:a?", "-af", "aresample=48000,aformat=channel_layouts=stereo"] + ENC + [path])
    return path


def concat(segments, srt, out):
    cmd = ["ffmpeg", "-hide_banner", "-y"]
    for s in segments:
        cmd += ["-i", s]
    cmd += ["-i", srt]
    n = len(segments)
    graph = "".join("[%d:v][%d:a]" % (k, k) for k in range(n)) + "concat=n=%d:v=1:a=1[v][a]" % n
    run(cmd + ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-map", "%d:s" % n] + ENC +
        ["-c:s", "mov_text", "-metadata:s:s:0", "language=eng", "-movflags", "+faststart", out])


def render_cards(cards, lesson, work, cfg, use_npx):
    """-> [(segment path, duration, cues relative to the segment)]"""
    out = []
    for k, card in enumerate(cards):
        image = None
        if card.get("diagram"):
            png = mermaid_png(card["diagram"], work, use_npx) if use_npx is not None else None
            if png:
                with open(png, "rb") as f:
                    image = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
        png = svg_to_png(card_svg(card, lesson.get("title", ""), image), os.path.join(work, "card-%02d.png" % k))
        voice = os.path.join(work, "card-%02d.aiff" % k)
        vdur = RIG.tts(card["say"], voice, cfg["voice"], cfg["rate"])
        dur = max(CARD_MIN, CARD_LEAD + vdur + CARD_TAIL)
        seg = still_segment(png, voice, dur, os.path.join(work, "card-%02d.mp4" % k))
        out.append((seg, dur, RIG.cues_for(card["say"], CARD_LEAD, vdur)))
    return out


def media_duration(path):
    return RIG.media_duration(path)


# ---------------------------------------------------------------- the course state

def http_ok(url, timeout=2):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 - any failure means "not up"
        return False


def port_free(port):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def wc(args, env, cwd=None, input=None):
    return run([sys.executable, WAVECOORD] + list(args), env=env, cwd=cwd, input=input)


def pid_file(home):
    return os.path.join(course_paths(home)["wc"], "serve.pid")


def stop_server(home=COURSE_HOME):
    path = pid_file(home)
    try:
        pid = int(read_text(path).strip())
    except (OSError, ValueError):
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        pass
    end = time.time() + 15
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.2)
    os.remove(path)
    return True


def start_server(repo, env, home=COURSE_HOME, port=PORT, extra_env=None):
    """Start `wavecoord serve` for the demo, detached, and wait until it answers."""
    stop_server(home)
    wch = course_paths(home)["wc"]
    os.makedirs(wch, exist_ok=True)
    senv = dict(os.environ)
    for k in list(senv):
        if k.startswith(("CLAUDECODE", "CLAUDE_CODE_", "WAVECOORD_", "CLAUDE_PLUGIN_")):
            senv.pop(k)
    senv.update(env)
    senv.update(extra_env or {})
    senv.pop("WAVECOORD_ROLE", None)
    log = open(os.path.join(wch, "serve.log"), "a")
    p = subprocess.Popen([sys.executable, WAVECOORD, "serve", "--repo", repo, "--home", os.path.join(wch, "server"),
                          "--port", str(port)], env=senv, cwd=repo, stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    with open(pid_file(home), "w") as f:
        f.write(str(p.pid))
    end = time.time() + 30
    while time.time() < end:
        if http_ok("http://127.0.0.1:%d/api/health" % port):
            return p.pid
        if p.poll() is not None:
            break
        time.sleep(0.3)
    raise SystemExit("course: the coordinator did not start; see %s" % os.path.join(wch, "serve.log"))


def claude_cmd(env, *args, input=None, check=False):
    p = subprocess.run([RIG.find_claude()] + list(args), env=env, input=input, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True, timeout=180)
    if check and p.returncode != 0:
        raise SystemExit("course: claude %s failed: %s" % (" ".join(args[:3]), (p.stderr or p.stdout)[-500:]))
    return p


def prepare(lesson, env, home=COURSE_HOME, port=PORT, log=print):
    """Off camera: the demo repo, fresh tokens, the plugin options, the coordinator."""
    demos = load_demos()
    stop_server(home)
    wch = course_paths(home)["wc"]
    shutil.rmtree(wch, ignore_errors=True)
    os.makedirs(os.path.join(wch, "hosts"), mode=0o700)
    os.makedirs(course_paths(home)["claude"], exist_ok=True)
    demo = lesson.get("demo") or {}
    p = demos.build(demo["name"], demo_dir(lesson), port=port, state=demo.get("state"),
                    with_config=demo.get("config", True))
    log("demo %s%s in %s" % (demo["name"], " (%s)" % demo["state"] if demo.get("state") else "", p["repo"]))
    cenv = dict(os.environ, **env)
    for k in list(cenv):
        if k.startswith(("CLAUDECODE", "CLAUDE_CODE_")):
            cenv.pop(k)
    if lesson.get("plugin") == "marketplace":
        # the install lesson starts with no wavecoord plugin and no marketplace in the course profile
        claude_cmd(cenv, "plugin", "uninstall", "wavecoord@wavecoord")
        claude_cmd(cenv, "plugin", "marketplace", "remove", "wavecoord")
        return p
    claude_cmd(cenv, "plugin", "uninstall", "wavecoord@wavecoord")
    wc(["tokens", "provision", "--role", "lead", "--out", env["WAVECOORD_TOKEN_FILE"]], cenv)
    wc(["tokens", "provision", "--role", "agent", "--host", "local", "--out",
        os.path.join(wch, "hosts", "local.token")], cenv)
    with open(env["WAVECOORD_TOKEN_FILE"]) as f:
        token = f.read().strip()
    values = json.dumps({"coord_url": "http://127.0.0.1:%d" % port, "lead": "true", "token": token})
    claude_cmd(cenv, "--plugin-dir", PLUGIN_DIR, "plugin", "configure", "wavecoord@inline", "--values-stdin",
               input=values, check=True)
    if lesson.get("server"):
        start_server(p["repo"], env, home, port, lesson.get("server_env"))
        log("coordinator on port %d" % port)
    return p


# ---------------------------------------------------------------- prerequisites

def prereqs(lessons, home=COURSE_HOME, port=PORT, cards_only=False):
    """-> (problems, notes). Problems stop a recording; notes are worth a look."""
    problems, notes = [], []
    if sys.platform != "darwin":
        problems.append("recording needs macOS (Terminal, `say`, avfoundation); --validate works anywhere")
    for tool, how in (("ffmpeg", "brew install ffmpeg"), ("ffprobe", "brew install ffmpeg"),
                      ("rsvg-convert", "brew install librsvg"), ("say", "macOS"), ("osascript", "macOS"),
                      ("git", "xcode-select --install")):
        if not shutil.which(tool):
            problems.append("%s is missing (%s)" % (tool, how))
    if sys.version_info < (3, 9):
        problems.append("Python 3.9 or later")
    if cards_only:
        return problems, notes
    claude = RIG.find_claude()
    if not (os.path.isabs(claude) or shutil.which(claude)):
        problems.append("claude is not on PATH")
    else:
        env = dict(os.environ, CLAUDE_CONFIG_DIR=course_paths(home)["claude"])
        try:
            p = claude_cmd(env, "auth", "status", "--json")
            st = json.loads(p.stdout or "{}")
        except (ValueError, OSError, subprocess.TimeoutExpired):
            st = {}
        if not st.get("loggedIn"):
            problems.append("the course profile is not logged in: CLAUDE_CONFIG_DIR=%s claude auth login"
                            % course_paths(home)["claude"])
    if not port_free(port) and not os.path.exists(pid_file(home)):
        problems.append("port %d is in use; set COURSE_PORT to a free port" % port)
    if not shutil.which("npx") and not shutil.which("mmdc"):
        notes.append("no Mermaid renderer (mmdc or npx): diagrams are drawn by this script instead")
    if any((l.get("plugin") == "marketplace") for l in lessons):
        try:
            req = urllib.request.Request(CLI_URL, method="HEAD")
            urllib.request.urlopen(req, timeout=10)
        except Exception:  # noqa: BLE001
            notes.append("lesson 01 downloads %s: it is not reachable yet (publish the v0.3.0 tag first)" % CLI_URL)
        notes.append("lesson 01 installs the plugin from the jellologic/wavecoord marketplace and the CLI into "
                     "~/.local/bin/wavecoord")
        if os.path.exists(os.path.expanduser("~/.wavecoord/hosts/local.token")):
            notes.append("~/.wavecoord/hosts/local.token exists: wavecoord init keeps it and doctor reports it as "
                         "unknown to the course coordinator (doctor and init ignore WAVECOORD_HOME)")
    return problems, notes


# ---------------------------------------------------------------- one lesson

def usage_split(config_dir, since, repo):
    """Token usage since `since`: the lead (sessions in the demo repo) and the workers (in its worktrees)."""
    projects = os.path.join(config_dir, "projects")
    keys_lead = {RIG.project_key(repo), RIG.project_key(os.path.realpath(repo))}
    wt = os.path.join(os.path.dirname(repo), "wt")
    keys_wt = (RIG.project_key(wt), RIG.project_key(os.path.realpath(wt)))
    lead, workers = [], []
    for p in RIG.transcripts(projects, since):
        k = os.path.basename(os.path.dirname(p))
        if k in keys_lead:
            lead.append(p)
        elif k.startswith(keys_wt):
            workers.append(p)
    return {"lead": RIG.usage_of(lead), "workers": RIG.usage_of(workers)}


def build_lesson(path, args):
    lesson = read_json(path)
    lid = lesson_id(path)
    errs = validate(path)
    if errs:
        raise SystemExit("course: %s is not valid:\n  %s" % (lid, "\n  ".join(errs)))
    resolved, _ = resolve(lesson)
    cfg = dict(RIG.DEFAULTS)
    cfg.update(lesson.get("config") or {})
    work = os.path.join(DIST, "work", lid)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(os.path.join(work, "cards"))
    use_npx = None if args.no_mermaid else True
    cards = lesson.get("cards") or {}
    print("== %s: %s" % (lid, lesson.get("title")), flush=True)
    intro = render_cards(cards.get("intro", []), lesson, os.path.join(work, "cards"), cfg, use_npx)
    outro_dir = os.path.join(work, "cards", "outro")
    os.makedirs(outro_dir)
    outro = render_cards(cards.get("outro", []), lesson, outro_dir, cfg, use_npx)
    segments, cues, t = [], [], 0.0
    for seg, dur, cs in intro:
        segments.append(seg)
        cues += [(t + a, t + b, x) for a, b, x in cs]
        t += dur
    t0 = time.time()
    usage = None
    if not args.cards_only:
        rig_out = os.path.join(work, "rig")
        lesson_path = os.path.join(work, lid + ".json")
        with open(lesson_path, "w") as f:
            json.dump(resolved, f, indent=1)
        repo = prepare(lesson, resolved["env"])["repo"]
        # `wavecoord init` writes the local host's token to ~/.wavecoord/hosts/ whatever WAVECOORD_HOME says:
        # remove it after the take when the take made it
        stray = os.path.expanduser("~/.wavecoord/hosts/local.token")
        had_stray = os.path.lexists(stray)
        try:
            rc = subprocess.call([sys.executable, os.path.join(HERE, "rig.py"), "run", lesson_path, "--out", rig_out]
                                 + (["--scale-factor", str(args.scale_factor)] if args.scale_factor else []))
        finally:
            stop_server()
            if not had_stray and os.path.lexists(stray):
                os.remove(stray)
        if rc != 0:
            print("course: the rig reported a problem with %s (see %s/ctl/error); the video is kept" % (lid, rig_out),
                  file=sys.stderr)
        raw = os.path.join(rig_out, lid + ".mp4")
        if not (os.path.exists(raw) and os.path.exists(os.path.join(rig_out, "timeline.json"))):
            raise SystemExit("course: %s: the rig made no video (see the output above and %s/ctl/)" % (lid, rig_out))
        with open(os.path.join(rig_out, "timeline.json")) as f:
            timeline = json.load(f)
        steps = lesson["steps"]
        clips = []
        for row in timeline["steps"]:
            if row["kind"] == "say":
                v = os.path.join(rig_out, "voice", "%03d.aiff" % row["i"])
                clips.append((row["at"], media_duration(v) if os.path.exists(v) else 0.0))
        dur = media_duration(raw)
        cuts = plan_cuts(timeline["steps"], steps, clips)
        parts = pieces(dur, cuts)
        for n, (kind, a, b, _) in enumerate(parts):
            if kind == "rig":
                segments.append(cut_piece(raw, a, b, os.path.join(work, "piece-%02d.mp4" % n)))
            else:
                png = svg_to_png(later_svg(later_text(a)), os.path.join(work, "later-%02d.png" % n))
                segments.append(still_segment(png, None, LATER_S, os.path.join(work, "later-%02d.mp4" % n)))
        cues += shift_cues(parse_srt(read_text(os.path.join(rig_out, lid + ".srt"))), parts, t)
        t += pieces_duration(parts)
        usage = usage_split(course_paths()["claude"], t0, repo)
        usage["real_minutes"] = round((time.time() - t0) / 60.0, 1)
        usage["cut_minutes"] = round(sum(b - a for a, b in cuts) / 60.0, 1)
    for seg, d, cs in outro:
        segments.append(seg)
        cues += [(t + a, t + b, x) for a, b, x in cs]
        t += d
    os.makedirs(DIST, exist_ok=True)
    name = "course-" + lid
    srt = os.path.join(DIST, name + ".srt")
    write_srt(cues, srt)
    out = os.path.join(DIST, name + ".mp4")
    concat(segments, srt, out)
    if usage:
        with open(os.path.join(DIST, name + ".usage.json"), "w") as f:
            json.dump(usage, f, indent=1)
    print("video   %s (%.1f min)" % (out, media_duration(out) / 60.0))
    print("srt     %s" % srt)
    if usage:
        print("usage   %s" % json.dumps(usage))
    return out


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--lesson", action="append", default=[], help="NN: record this lesson (repeatable)")
    ap.add_argument("--all", action="store_true", help="record every lesson")
    ap.add_argument("--validate", action="store_true", help="check every lesson script (or the --lesson ones)")
    ap.add_argument("--check", action="store_true", help="check the prerequisites of a recording")
    ap.add_argument("--cards-only", action="store_true", help="render only the cards (no Claude Code)")
    ap.add_argument("--no-mermaid", action="store_true", help="draw the diagrams here; never call mmdc or npx")
    ap.add_argument("--scale-factor", type=int, help="screen pixels per point, for the rig")
    ap.add_argument("--start-server", action="store_true", help="(lesson step) start the demo's coordinator")
    ap.add_argument("--stop-server", action="store_true", help="(lesson step) stop the demo's coordinator")
    a = ap.parse_args(argv)
    if a.start_server or a.stop_server:
        if a.stop_server:
            stop_server()
            return 0
        repo = os.environ.get("COURSE_DEMO_DIR") or os.getcwd()
        env = {k: v for k, v in os.environ.items() if k.startswith(("WAVECOORD_", "CLAUDE_CONFIG_DIR"))
               or k == "PATH"}
        start_server(repo, env)
        return 0
    paths = [find_lesson(n) for n in a.lesson] if a.lesson else lesson_files()
    if a.validate or not (a.all or a.lesson or a.check):
        bad = 0
        total = 0.0
        for p in paths:
            errs = validate(p)
            lesson = read_json(p)
            est = lesson.get("estimate") or {}
            total += est.get("video_min") or 0
            print("%-24s %s video ~%s min (computed %.1f), recording ~%s min, lead %s, cost %s" % (
                lesson_id(p), "ok " if not errs else "BAD", est.get("video_min"), video_seconds(lesson) / 60.0,
                est.get("real_min", "?"), est.get("lead", "-"), est.get("cost", "-")))
            for e in errs:
                print("    " + e)
            bad += bool(errs)
        print("%d lessons, %d with problems, about %.0f minutes of video" % (len(paths), bad, total))
        return 1 if bad else 0
    lessons = [read_json(p) for p in paths]
    problems, notes = prereqs(lessons, cards_only=a.cards_only)
    for n in notes:
        print("note: " + n)
    if problems:
        print("course: cannot record yet:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    if a.check:
        print("ready to record")
        return 0
    failed = []
    for p in paths:
        try:
            build_lesson(p, a)
        except SystemExit as e:  # one lesson's failure must not stop the others
            print(str(e), file=sys.stderr)
            failed.append(lesson_id(p))
    if failed:
        print("course: no video for %s" % ", ".join(failed), file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
