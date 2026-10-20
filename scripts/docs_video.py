#!/usr/bin/env python3
"""Animated SVGs, GIFs and narrated videos for the docs, built from the scenes that docs-screens.py records.

    python3 scripts/docs-screens.py --all                    # the demo, then everything below
    python3 scripts/docs-screens.py --anim                   # only the animated SVGs (no tools needed)
    python3 scripts/docs-screens.py --scenes s.json          # save the recorded scenes, then later:
    python3 scripts/docs-screens.py --from-scenes s.json --video --only steering

A scene is the commands of one capture with their real output and how long each took. From a scene:

- the timeline types each command at a natural speed, shows its output (at once, or line by line for a
  slow command) and pauses; `frames` samples it into deduplicated frames with durations;
- `anim_svg` writes it as one SVG with CSS keyframes (it loops; GitHub plays it in an <img>), and its static
  form (rsvg-convert, reduced motion) is the last frame;
- `--gif` renders the frames with rsvg-convert and encodes a two-pass palette GIF with ffmpeg;
- `--video` joins chapters (scenes plus narration from scripts/narration/<video>.txt) into an H.264 MP4 with
  an AAC voiceover and an .srt. Each sentence is synthesized on its own (macOS `say` by default), so its
  length is known: a chapter lasts at least as long as its narration, and the subtitles match the voice.

Dev tools only, stdlib only. GIFs and videos need rsvg-convert and ffmpeg; the default voice needs macOS `say`.
"""

import concurrent.futures
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NARRATION_DIR = os.path.join(HERE, "narration")


def _load_screens():
    if "docs_screens" in sys.modules:
        return sys.modules["docs_screens"]
    spec = importlib.util.spec_from_file_location("docs_screens", os.path.join(HERE, "docs-screens.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["docs_screens"] = mod
    spec.loader.exec_module(mod)
    return mod


S = _load_screens()  # the terminal look: palette, font, ANSI parsing and wrapping

# ---------------------------------------------------------------- what to build

# Animated SVGs for the docs: name -> the captures they play, in order. GIFS: the ones also written as GIFs.
ANIMS = {
    "quickstart": ["status"],
    "plan": ["spec-check", "spec-waves", "plan-ready"],
    "wave": ["dispatch", "attempts-wave1", "refinery", "gate-check"],
    "steer": ["steer-running", "steer-hint2", "steer-model", "steer-interrupt", "steer-logs"],
}
GIFS = ("wave", "plan", "steer")
ANIM_COLS = {"quickstart": 120, "wave": 110}
ANIM_TITLES = {"quickstart": "wavecoord status && wavecoord hosts", "plan": "Check the plan, see the waves",
               "wave": "Dispatch a wave, collect, merge", "steer": "Steer a running worker"}

# Videos: name -> (title, [(chapter key, heading, [captures])]). The narration of each chapter is the
# [key] section of scripts/narration/<name>.txt. A chapter with no captures is a title card.
VIDEOS = {
    "quickstart": ("wavecoord quickstart", [
        ("intro", "wavecoord quickstart", []),
        ("status", "Is the coordinator up? Which hosts can run workers?", ["status"]),
        ("tree", "The plan is a spec tree in git", ["spec-tree-start"]),
        ("check", "Check the tree", ["spec-check"]),
        ("waves", "Group the tasks into waves", ["spec-waves"]),
        ("ready", "What can start now", ["plan-ready"]),
        ("outro", "Next: dispatch the ready wave", []),
    ]),
    "spec-to-merge": ("From spec to merge", [
        ("intro", "From spec to merge", []),
        ("plan", "A checked plan", ["spec-check", "spec-waves"]),
        ("dispatch", "Dispatch wave 1", ["dispatch"]),
        ("attempts", "Workers run in parallel", ["attempts-wave1"]),
        ("events", "The lead's events feed", ["events-lead"]),
        ("merge", "Collect, then merge in order", ["refinery"]),
        ("gate", "Why one merge was blocked", ["gate-check"]),
        ("tree", "Status flows back to the tree", ["spec-tree"]),
    ]),
    "steering": ("Steering a running worker", [
        ("intro", "Steering a running worker", []),
        ("running", "A worker in flight", ["steer-running"]),
        ("hint", "Send a hint", ["steer-hint2"]),
        ("model", "Switch its model", ["steer-model"]),
        ("interrupt", "Interrupt with a note", ["steer-interrupt"]),
        ("logs", "The attempt log shows each action", ["steer-logs"]),
        ("feed", "The report reaches the lead", ["steer-feed"]),
    ]),
}

# How the TTS should say a few words; the subtitles keep the written form.
SAY_AS = [(r"\bwavecoord\b", "wave coord"), (r"\bREADME\.md\b", "read me"), (r"\bls\b", "L S"),
          (r"\bgate-check\b", "gate check"), (r"\bMCP\b", "M C P"), (r"\bSSH\b", "S S H"), (r"\bCLI\b", "C L I"),
          (r"\bPR\b", "P R"), (r"\bPRs\b", "P Rs")]

# ---------------------------------------------------------------- the timeline (pure)

FPS = 10              # frames are sampled at this rate (and deduplicated)
TYPE_S = 0.045        # one typed character
TYPE_SPACE_S = 0.03   # extra after a space or dash, so typing has a rhythm
LEAD_S = 0.6          # the prompt shows, then typing starts
ENTER_S = 0.35        # the last character, then Enter
FAST_S = 0.5          # output of a command quicker than this shows at once
LINE_S = (0.05, 0.14)  # per output line when it comes line by line
GAP_S = 1.6           # output, then the next command starts typing
HOLD_S = 3.5          # the end of an animation, before it loops


def out_lines(step):
    out = step.get("out") or ""
    return out.rstrip("\n").split("\n") if out.strip() else []


def char_delay(cmd, i):
    """Seconds after character i-1 that character i shows (deterministic, so re-runs give the same frames)."""
    prev = cmd[i - 1] if i else ""
    return TYPE_S + (TYPE_SPACE_S if prev in (" ", "-") else 0.0) + ((ord(cmd[i]) * 7) % 5) * 0.004


def timeline(steps, extra=0.0, gap=GAP_S, hold=HOLD_S, lead=LEAD_S, offset=0):
    """The scene as [(t, state)], sorted, and its total length. A state is (k, typed, shown): the steps before
    k are complete, step k shows `typed` characters of its command and `shown` lines of its output;
    k == len(steps) is the idle prompt at the end. `extra` seconds (narration that runs longer than the
    scene) are spread over the pauses after each command's output, so a command runs early and stays on
    screen while the voice explains it. `offset` shifts k, for a scene that continues earlier steps."""
    n = len(steps)
    per = extra / n if extra > 0 and n else 0.0
    ev, t = [], 0.0
    for k, st in enumerate(steps):
        ev.append((t, (offset + k, 0, 0)))
        t += lead if k == 0 else gap + per
        k += offset
        cmd = st["cmd"]
        for i in range(len(cmd)):
            t += char_delay(cmd, i)
            ev.append((t, (k, i + 1, 0)))
        t += ENTER_S
        lines = out_lines(st)
        secs = float(st.get("secs") or 0)
        if not lines:
            t += min(secs, 1.0)
        elif secs < FAST_S or len(lines) <= 2:
            t += max(min(secs, 1.0), 0.12)
            ev.append((t, (k, len(cmd), len(lines))))
        else:
            # real commands print in one go; show them line by line, at most ~1.5 s in all
            t += min(secs, 1.0) * 0.5
            dt = max(LINE_S[0], min(LINE_S[1], 1.5 / len(lines)))
            for j in range(len(lines)):
                ev.append((t, (k, len(cmd), j + 1)))
                t += dt
    ev.append((t, (offset + n, 0, 0)))
    total = t + hold + per
    return ev, total


def frames(events, total, fps=FPS):
    """Sample a timeline at `fps`: [(state, seconds)], consecutive equal states merged. Every time is snapped
    to the frame grid, so the durations add up to the total (rounded up to a whole frame)."""
    snapped = {}
    for t, state in events:
        snapped[int(round(t * fps))] = state  # a later event in the same frame wins
    n_total = max(int(math.ceil(total * fps - 1e-9)), max(snapped) + 1 if snapped else 1)
    out = []
    keys = sorted(snapped)
    for i, f in enumerate(keys):
        end = keys[i + 1] if i + 1 < len(keys) else n_total
        if end <= f:
            continue
        if out and out[-1][0] == snapped[f]:
            out[-1][1] += end - f
        else:
            out.append([snapped[f], end - f])
    return [(st, cnt / float(fps)) for st, cnt in out]


# ---------------------------------------------------------------- screen rows (pure)

def screen_rows(steps, state, cols, idle=True):
    """The rows a state shows and where the cursor is: (rows, (row, col) or None). Rows are span lists as
    docs-screens' wrap_spans makes them; a blank row separates the commands, as in the static SVGs."""
    k, typed, shown = state
    rows, cursor = [], None
    for i, st in enumerate(steps[:k + 1]):
        if i:
            rows.append([])
        full = i < k
        cmd = st["cmd"] if full else st["cmd"][:typed]
        crow = S.wrap_spans([("$ ", (S.PROMPT, True)), (cmd, (S.FG, True))], cols)
        rows.extend(crow)
        lines = out_lines(st) if full else out_lines(st)[:shown]
        if not full and shown == 0:
            cursor = (len(rows) - 1, sum(len(t) for t, _ in crow[-1]))
        for line in lines:
            rows.extend(S.wrap_spans(S.ansi_spans(line), cols))
    if k == len(steps) and idle:
        if steps:
            rows.append([])
        rows.append([("$ ", (S.PROMPT, True))])
        cursor = (len(rows) - 1, 2)
    return rows, cursor


def scene_size(steps, cols):
    """(width in columns, rows) of a scene's full layout, as the static renderer sizes a window."""
    rows, _ = screen_rows(steps, (len(steps), 0, 0), cols)
    return max([60] + [sum(len(t) for t, _ in r) for r in rows]), len(rows)


# ---------------------------------------------------------------- SVG (pure)

def window_px(width_cols, n_rows):
    return int(S.PAD * 2.5 + width_cols * S.CHAR_W), int(36 + S.PAD + n_rows * S.LINE_H + S.PAD - 4)


def chrome(width, height, title, width_cols):
    """The window: background, title bar, the three dots and the title (as docs-screens' render_svg)."""
    out = ['<rect width="%d" height="%d" rx="10" ry="10" fill="%s"/>' % (width, height, S.BG),
           '<path d="M0 10 a10 10 0 0 1 10 -10 h%d a10 10 0 0 1 10 10 v18 h-%d z" fill="%s"/>' % (
               width - 20, width, S.BAR)]
    for k, c in enumerate(("#f38ba8", "#f9e2af", "#a6e3a1")):
        out.append('<circle cx="%d" cy="14" r="6" fill="%s"/>' % (18 + k * 20, c))
    short = title if len(title) <= width_cols - 16 else title[:width_cols - 19] + "..."
    out.append('<text x="%d" y="18.5" fill="%s" font-family="%s" font-size="12" text-anchor="middle">%s</text>' % (
        width // 2, S.DIM, S.esc(S.FONT), S.esc(short)))
    return out


def row_text(row, y, attrs=""):
    parts = []
    for t, (color, bold) in row:
        a = ""
        if color and color != S.FG:
            a += ' fill="%s"' % color
        if bold:
            a += ' font-weight="bold"'
        parts.append('<tspan%s>%s</tspan>' % (a, S.esc(t)) if a else S.esc(t))
    return '<text x="%d" y="%d" xml:space="preserve"%s>%s</text>' % (S.PAD, y, attrs, "".join(parts))


def row_y(n):
    return 36 + S.PAD + n * S.LINE_H


def text_group_open():
    return '<g font-family="%s" font-size="%d" fill="%s" xml:space="preserve" style="white-space:pre">' % (
        S.esc(S.FONT), S.FONT_PX, S.FG)


# The advance of one character in the usual monospace fonts (0.6 em; Menlo and DejaVu Sans Mono 0.602 em).
# The window is sized with docs-screens' wider CHAR_W, so no font overflows; the cursor and the typing covers
# move by the real advance, so they stay on the text.
ADV = 0.602 * S.FONT_PX


def cursor_rect(row, col, extra=""):
    return '<rect x="%.1f" y="%d" width="%.1f" height="%d" fill="%s" opacity="0.8"%s/>' % (
        S.PAD + col * ADV, row_y(row) - S.FONT_PX + 1, ADV, S.LINE_H - 2, S.FG, extra)


def frame_body(steps, state, title, cols, width_cols, n_rows):
    """The elements of one terminal frame (scrolled to the last n_rows rows) and its pixel size."""
    rows, cursor = screen_rows(steps, state, cols)
    if len(rows) > n_rows:
        cut = len(rows) - n_rows
        rows = rows[cut:]
        cursor = (cursor[0] - cut, cursor[1]) if cursor and cursor[0] >= cut else None
    width, height = window_px(width_cols, n_rows)
    out = chrome(width, height, title, width_cols)
    out.append(text_group_open())
    for n, row in enumerate(rows):
        if row:
            out.append(row_text(row, row_y(n)))
    out.append("</g>")
    if cursor:
        out.append(cursor_rect(*cursor))
    return width, height, out


def frame_svg(steps, state, title, cols, width_cols, n_rows):
    width, height, body = frame_body(steps, state, title, cols, width_cols, n_rows)
    return "\n".join(['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">' % (
        width, height, width, height)] + body + ["</svg>"]) + "\n"


VIDEO_W, VIDEO_H, VIDEO_COLS, VIDEO_ROWS = 1920, 1080, 118, 24
CANVAS, HEAD = "#11111b", "#bac2de"


def video_frame_svg(steps, state, title, heading, cols=VIDEO_COLS, n_rows=VIDEO_ROWS):
    """A 1920x1080 video frame: the chapter heading over a terminal window scaled to fit."""
    width, height, body = frame_body(steps, state, title, cols, cols, n_rows)
    scale = min((VIDEO_W - 160) / float(width), (VIDEO_H - 220) / float(height))
    x, y = (VIDEO_W - width * scale) / 2, 170
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">' % (
        VIDEO_W, VIDEO_H, VIDEO_W, VIDEO_H),
        '<rect width="%d" height="%d" fill="%s"/>' % (VIDEO_W, VIDEO_H, CANVAS),
        '<text x="%d" y="105" fill="%s" font-family="%s" font-size="44" font-weight="bold" '
        'text-anchor="middle">%s</text>' % (VIDEO_W // 2, HEAD, S.esc(S.FONT), S.esc(heading)),
        '<g transform="translate(%.1f %.1f) scale(%.4f)">' % (x, y, scale)] + body + ["</g></svg>"]
    return "\n".join(out) + "\n"


def card_svg(title, subtitle):
    return "\n".join([
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">' % (
            VIDEO_W, VIDEO_H, VIDEO_W, VIDEO_H),
        '<rect width="%d" height="%d" fill="%s"/>' % (VIDEO_W, VIDEO_H, CANVAS),
        '<text x="%d" y="470" fill="%s" font-family="%s" font-size="40" text-anchor="middle">'
        '<tspan fill="%s" font-weight="bold">$</tspan> wavecoord</text>' % (
            VIDEO_W // 2, S.FG, S.esc(S.FONT), S.PROMPT),
        '<text x="%d" y="580" fill="%s" font-family="%s" font-size="72" font-weight="bold" '
        'text-anchor="middle">%s</text>' % (VIDEO_W // 2, S.FG, S.esc(S.FONT), S.esc(title)),
        '<text x="%d" y="660" fill="%s" font-family="%s" font-size="30" text-anchor="middle">%s</text>' % (
            VIDEO_W // 2, S.DIM, S.esc(S.FONT), S.esc(subtitle)),
        "</svg>"]) + "\n"


def anim_svg(steps, title, cols=100, hold=HOLD_S):
    """The scene as one looping SVG animated with CSS keyframes (no script), for an <img> on GitHub.

    Each row is drawn once and appears when the timeline shows it. A command row has a cover (a background
    block with the cursor at its left edge) that steps right one character per typed character, so the
    command looks typed; it fades once the output shows. Every animation has the loop's length and step-end
    timing. Without CSS animation (rsvg-convert, prefers-reduced-motion) the SVG shows the last frame."""
    events, total = timeline(steps, hold=hold)
    width_cols, n_rows = scene_size(steps, cols)
    width, height = window_px(width_cols, n_rows)
    final, _ = screen_rows(steps, (len(steps), 0, 0), cols)

    def pct(t):
        return "%.3f%%" % min(100.0, max(0.0, 100.0 * t / total))

    # when each row appears, and the typing of each command
    appear = [None] * len(final)
    typing = []  # (row index, start col, [(t, typed)], t_output)
    t_of = {}
    for t, st in events:
        t_of.setdefault(st, t)
    row = 0
    for k, st in enumerate(steps):
        if k:
            row += 1
        crow = S.wrap_spans([("$ ", (S.PROMPT, True)), (st["cmd"], (S.FG, True))], cols)
        t_prompt = t_of[(k, 0, 0)]
        lines = out_lines(st)
        t_done = t_of.get((k, len(st["cmd"]), 0), t_prompt)
        t_out = min([t for t, s in events if s[0] > k or (s[0] == k and s[2] > 0)] or [total])
        appear[row] = t_prompt
        for j in range(1, len(crow)):
            appear[row + j] = t_done  # a wrapped command: its later rows show when it is typed
        keys = [(t, s[1]) for t, s in events if s[0] == k and s[2] == 0 and s[1] > 0]
        typing.append((row, 2, min(len(st["cmd"]), sum(len(t) for t, _ in crow[0]) - 2), keys, t_out))
        row += len(crow)
        for j, line in enumerate(lines):
            t_line = t_of.get((k, len(st["cmd"]), j + 1), t_out)
            for _ in S.wrap_spans(S.ansi_spans(line), cols):
                appear[row] = t_line
                row += 1
    row += 1  # the blank row before the idle prompt
    appear[row] = t_of[(len(steps), 0, 0)]

    css, classes = [], {}

    def cls(frames_):
        """A class for this keyframe list [(t, declarations)] (shared by equal lists)."""
        key = tuple(frames_)
        if key not in classes:
            name = "k%d" % len(classes)
            classes[key] = name
            stops = ["0%%{%s}" % frames_[0][1]] + ["%s{%s}" % (pct(t), d) for t, d in frames_[1:]] + [
                "100%%{%s}" % frames_[-1][1]]
            css.append("@keyframes %s{%s}" % (name, "".join(stops)))
            css.append(".%s{animation-name:%s}" % (name, name))
        return classes[key]

    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img" '
           'aria-label="%s">' % (width, height, width, height, S.esc(title)),
           '<title>%s</title>' % S.esc(title), None]
    out += chrome(width, height, title, width_cols)
    out.append(text_group_open())
    covers = {r: (c0, n, keys, t_out) for r, c0, n, keys, t_out in typing}
    for n, r in enumerate(final):
        t = appear[n] if appear[n] is not None else 0.0
        c = cls([(0, "opacity:0"), (t, "opacity:1")]) if t > 0 else None
        body = []
        if r:
            body.append(row_text(r, row_y(n)))
        if n in covers:
            c0, count, keys, t_out = covers[n]
            x0 = S.PAD + c0 * ADV
            shift = count * ADV
            kf = [(0, "transform:translateX(0px);opacity:1")]
            for tk, typed in keys:
                kf.append((tk, "transform:translateX(%.1fpx);opacity:1" % (min(typed, count) * ADV)))
            kf.append((t_out, "transform:translateX(%.1fpx);opacity:0" % shift))
            body.append('<g class="a %s" transform="translate(%.1f 0)" opacity="0">'
                        '<rect x="%.1f" y="%d" width="%d" height="%d" fill="%s"/>%s</g>' % (
                            cls(kf), shift, x0, row_y(n) - S.FONT_PX, int(width - x0), S.LINE_H, S.BG,
                            cursor_rect(n, c0)))
        if n == len(final) - 1:
            body.append(cursor_rect(n, 2, ' class="blink"'))
        if not body:
            continue
        out.append('<g class="a %s">%s</g>' % (c, "".join(body)) if c else "".join(body))
    out.append("</g></svg>")
    style = ("<style>.a{animation-duration:%.2fs;animation-iteration-count:infinite;"
             "animation-timing-function:step-end}"
             ".blink{animation:blink 1s step-end infinite}@keyframes blink{0%%{opacity:.8}50%%{opacity:0}}"
             "%s@media (prefers-reduced-motion:reduce){.a,.blink{animation:none}}</style>" % (total, "".join(css)))
    out[2] = style
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- narration and subtitles (pure)

def parse_narration(text):
    """[key] sections of a narration file -> {key: paragraph}. Lines starting with # are comments."""
    out, key = {}, None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        m = re.match(r"^\[([\w-]+)\]$", s)
        if m:
            key = m.group(1)
            out[key] = ""
        elif key and s:
            out[key] = (out[key] + " " + s).strip()
    return out


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]


def say_as(text):
    for rx, repl in SAY_AS:
        text = re.sub(rx, repl, text)
    return text.replace("`", "")


def srt_time(t):
    ms = int(round(t * 1000))
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


def srt(cues):
    """cues: [(start, end, text)] -> SubRip text."""
    out = []
    for i, (a, b, text) in enumerate(cues, 1):
        out.append("%d\n%s --> %s\n%s\n" % (i, srt_time(a), srt_time(b), text))
    return "\n".join(out)


VOICE_LEAD_S, VOICE_GAP_S, VOICE_TAIL_S = 0.4, 0.35, 0.9


def voice_layout(durations):
    """Where each sentence starts in its chapter, and the least the chapter can last."""
    starts, t = [], VOICE_LEAD_S
    for d in durations:
        starts.append(t)
        t += d + VOICE_GAP_S
    return starts, (t - VOICE_GAP_S + VOICE_TAIL_S) if durations else 0.0


# ---------------------------------------------------------------- tools

def check_tools(args):
    """Fail early, with what to install, when a tool that this run needs is missing."""
    need = []
    if args.gif or args.video:
        need += [("rsvg-convert", "librsvg (brew install librsvg, apt install librsvg2-bin)"),
                 ("ffmpeg", "ffmpeg (brew install ffmpeg, apt install ffmpeg)")]
    if args.video and args.tts == "say":
        need.append(("say", "macOS `say`, or pass --tts openai or --tts elevenlabs"))
    missing = ["%s: install %s" % (t, how) for t, how in need if not shutil.which(t)]
    if args.video and args.tts == "openai" and not os.environ.get("OPENAI_API_KEY"):
        missing.append("--tts openai needs OPENAI_API_KEY")
    if args.video and args.tts == "elevenlabs" and not os.environ.get("ELEVENLABS_API_KEY"):
        missing.append("--tts elevenlabs needs ELEVENLABS_API_KEY")
    if missing:
        raise SystemExit("docs-video: missing tools:\n  " + "\n  ".join(missing))


def run(cmd, **kw):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, **kw)
    if p.returncode != 0:
        raise SystemExit("docs-video: %s failed (exit %d):\n%s" % (cmd[0], p.returncode, p.stderr[-2000:]))
    return p


def render_pngs(svgs, out_dir, width=None):
    """Render SVG strings to PNGs with rsvg-convert, in parallel; identical SVGs render once. Returns paths."""
    paths, todo = [], {}
    for svg in svgs:
        h = hashlib.sha1(svg.encode("utf-8")).hexdigest()[:16]
        p = os.path.join(out_dir, h + ".png")
        paths.append(p)
        todo[p] = svg

    def one(item):
        p, svg = item
        src = p[:-4] + ".svg"
        with open(src, "w") as f:
            f.write(svg)
        run(["rsvg-convert", "-o", p] + (["-w", str(width)] if width else []) + [src])
        os.unlink(src)

    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        list(ex.map(one, todo.items()))
    return paths


def concat_list(paths, durations, path):
    """An ffconcat file: each PNG for its duration (the last one repeated, as the demuxer wants)."""
    lines = ["ffconcat version 1.0"]
    for p, d in zip(paths, durations):
        lines += ["file '%s'" % p, "duration %.3f" % d]
    lines.append("file '%s'" % paths[-1])
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def make_gif(scene_steps, title, cols, path, work):
    events, total = timeline(scene_steps)
    fr = frames(events, total)
    width_cols, n_rows = scene_size(scene_steps, cols)
    w, _ = window_px(width_cols, n_rows)
    svgs = [frame_svg(scene_steps, st, title, cols, width_cols, n_rows) for st, _ in fr]
    pngs = render_pngs(svgs, work, width=min(1000, int(w * 1.2)))
    lst = os.path.join(work, "gif.ffconcat")
    concat_list(pngs, [d for _, d in fr], lst)
    pal = os.path.join(work, "palette.png")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst,
         "-vf", "palettegen=max_colors=48:stats_mode=full:reserve_transparent=0", pal])
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-i", pal,
         "-lavfi", "[0:v][1:v]paletteuse=dither=none:diff_mode=rectangle", "-fps_mode", "vfr", "-loop", "0", path])


# ---------------------------------------------------------------- TTS

def tts(text, backend, voice, cache):
    """Synthesize one sentence; returns a 24 kHz mono 16-bit WAV path (cached by backend, voice and text)."""
    spoken = say_as(text)
    key = hashlib.sha1(("%s|%s|%s" % (backend, voice, spoken)).encode("utf-8")).hexdigest()[:20]
    wav = os.path.join(cache, key + ".wav")
    if os.path.exists(wav):
        return wav
    raw = os.path.join(cache, key + ".raw")
    if backend == "say":
        txt = os.path.join(cache, key + ".txt")
        with open(txt, "w") as f:
            f.write(spoken)
        raw += ".aiff"
        run(["say", "-v", voice, "-o", raw, "-f", txt])
        os.unlink(txt)
    elif backend == "openai":
        req = urllib.request.Request("https://api.openai.com/v1/audio/speech", method="POST", headers={
            "Authorization": "Bearer " + os.environ["OPENAI_API_KEY"], "Content-Type": "application/json"},
            data=json.dumps({"model": os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"), "voice": voice,
                             "input": spoken, "response_format": "wav"}).encode("utf-8"))
        raw += ".wav"
        with urllib.request.urlopen(req, timeout=120) as r, open(raw, "wb") as f:
            f.write(r.read())
    else:
        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/text-to-speech/%s?output_format=mp3_44100_128" % voice, method="POST",
            headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"], "Content-Type": "application/json",
                     "Accept": "audio/mpeg"},
            data=json.dumps({"text": spoken, "model_id": "eleven_multilingual_v2"}).encode("utf-8"))
        raw += ".mp3"
        with urllib.request.urlopen(req, timeout=120) as r, open(raw, "wb") as f:
            f.write(r.read())
    run(["ffmpeg", "-y", "-v", "error", "-i", raw, "-ac", "1", "-ar", "24000", "-sample_fmt", "s16", wav + ".tmp.wav"])
    os.replace(wav + ".tmp.wav", wav)
    os.unlink(raw)
    return wav


def wav_secs(path):
    with wave.open(path) as w:
        return w.getnframes() / float(w.getframerate())


DEFAULT_VOICE = {"say": "Samantha", "openai": "alloy", "elevenlabs": "21m00Tcm4TlvDq8ikWAM"}


# ---------------------------------------------------------------- build

def steps_of(scenes, names):
    missing = [n for n in names if n not in scenes]
    if missing:
        raise SystemExit("docs-video: no recorded scene %s (re-run the demo)" % ", ".join(missing))
    return [st for n in names for st in scenes[n]["steps"]]


def build_anims(scenes, out_dir, only=()):
    written = []
    for name, caps in ANIMS.items():
        if only and name not in only:
            continue
        steps = steps_of(scenes, caps)
        title = ANIM_TITLES.get(name, name)
        svg = anim_svg(steps, title, cols=ANIM_COLS.get(name, 100))
        path = os.path.join(out_dir, "anim-%s.svg" % name)
        with open(path, "w") as f:
            f.write(svg)
        written.append(path)
        sys.stdout.write("anim %s: %d KB\n" % (path, len(svg) // 1024))
    return written


def build_gifs(scenes, out_dir, work, only=()):
    for name in GIFS:
        if only and name not in only:
            continue
        steps = steps_of(scenes, ANIMS[name])
        d = os.path.join(work, "gif-" + name)
        os.makedirs(d)
        path = os.path.join(out_dir, "%s.gif" % name)
        title = ANIM_TITLES.get(name, name)
        make_gif(steps, title, ANIM_COLS.get(name, 100), path, d)
        sys.stdout.write("gif %s: %d KB\n" % (path, os.path.getsize(path) // 1024))


def build_video(name, scenes, args, work):
    title, chapters = VIDEOS[name]
    with open(os.path.join(NARRATION_DIR, name + ".txt")) as f:
        narration = parse_narration(f.read())
    backend = args.tts
    voice = args.voice or DEFAULT_VOICE[backend]
    cache = os.path.join(args.video_out, ".tts-cache")
    os.makedirs(cache, exist_ok=True)
    svgs, durs, cues, clips, history = [], [], [], [], []
    t0 = 0.0
    for key, heading, caps in chapters:
        if key not in narration:
            raise SystemExit("docs-video: scripts/narration/%s.txt has no [%s] section" % (name, key))
        sents = sentences(narration[key])
        wavs = [tts(s, backend, voice, cache) for s in sents]
        lens = [wav_secs(w) for w in wavs]
        starts, need = voice_layout(lens)
        if caps:
            # one terminal session through the video: earlier commands stay above, scrolling up
            steps = steps_of(scenes, caps)
            off = len(history)
            history.extend(steps)
            kw = dict(hold=1.2, lead=0.8, offset=off)
            ev, natural = timeline(steps, **kw)
            ev, total = timeline(steps, extra=max(0.0, need - natural), **kw)
            fr = frames(ev, total)
            for st, d in fr:
                svgs.append(video_frame_svg(history, st, "~/demo", heading))
                durs.append(d)
            length = sum(d for _, d in fr)
        else:
            length = math.ceil(max(need, 3.0) * FPS) / FPS
            svgs.append(card_svg(heading, title if heading != title else "plan-driven parallel agent work"))
            durs.append(length)
        for s, a, d, w in zip(sents, starts, lens, wavs):
            cues.append((t0 + a, t0 + a + d, s))
            clips.append((t0 + a, w))
        t0 += length
    total = sum(durs)
    # the voice track: each sentence at its start, silence between (exact, in samples)
    rate = 24000
    pcm = bytearray(int(math.ceil(total * rate)) * 2)
    for start, w in clips:
        with wave.open(w) as f:
            data = f.readframes(f.getnframes())
        i = int(round(start * rate)) * 2
        data = data[:max(0, len(pcm) - i)]
        pcm[i:i + len(data)] = data
    audio = os.path.join(work, name + ".wav")
    with wave.open(audio, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(bytes(pcm))
    frame_dir = os.path.join(work, "v-" + name)
    os.makedirs(frame_dir)
    pngs = render_pngs(svgs, frame_dir)
    lst = os.path.join(frame_dir, "list.ffconcat")
    concat_list(pngs, durs, lst)
    mp4 = os.path.join(args.video_out, name + ".mp4")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-i", audio,
         "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-tune", "stillimage",
         "-c:a", "aac", "-b:a", "128k", "-t", "%.3f" % total, "-movflags", "+faststart",
         "-metadata", "title=%s" % title, mp4])
    with open(os.path.join(args.video_out, name + ".srt"), "w") as f:
        f.write(srt(cues))
    sys.stdout.write("video %s: %.1f s, %d KB (+ .srt)\n" % (mp4, total, os.path.getsize(mp4) // 1024))
    return mp4


def build(scenes, args):
    only = set(args.only or ())
    unknown = only - set(ANIMS) - set(VIDEOS)
    if unknown:
        raise SystemExit("docs-video: --only %s: not an animation (%s) or a video (%s)" % (
            ", ".join(sorted(unknown)), ", ".join(ANIMS), ", ".join(VIDEOS)))
    out = os.path.abspath(args.out)
    work = tempfile.mkdtemp(prefix="wc-video-")
    try:
        if args.anim:
            build_anims(scenes, out, only & set(ANIMS))
        if args.gif:
            build_gifs(scenes, out, work, only & set(ANIMS))
        if args.video:
            os.makedirs(args.video_out, exist_ok=True)
            for name in VIDEOS:
                if not only or name in only:
                    build_video(name, scenes, args, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
