#!/usr/bin/env python3
"""Record a real, scripted Claude Code session for the video course.

    python3 scripts/course/rig.py run LESSON.json [--cwd DIR] [--out DIR]
    python3 scripts/course/rig.py check LESSON.json ...

The rig opens a dedicated Terminal.app window and runs its own driver in it.
The driver starts the lesson command (for example `claude --model haiku`) in a
pty sized to the window, mirrors the pty to the window and plays the lesson
steps: it types, presses keys and waits on the screen text. ffmpeg records only
that window's rectangle (cropped at capture time). Afterwards the rig renders
the narration with `say`, places each line at its step's timestamp, writes an
.srt and muxes the final MP4.

Stdlib only, Python >= 3.9, macOS. See README.md for the lesson format.
"""

import argparse
import codecs
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "voice": "Samantha",
    "rate": 180,            # words per minute for `say`
    "font": "Menlo",
    "font_size": 20,
    # x, y, w, h in points: left of the notification banners, above the Dock.
    "bounds": [16, 64, 1536, 864],
    "rows_margin": 1,                 # pty rows = window rows - this (blank bottom row)
    "canvas": [1920, 1080],           # final frame; the window is centered, not scaled
    "fps": 30,
    "type_cps": 16,          # typing speed, characters per second
    "lead_in": 1.0,          # seconds of recording before the first step
    "tail": 1.5,             # seconds of recording after the last step
    "say_gap": 0.3,          # pause after a blocking narration line
    "background": [22, 24, 29],
    "foreground": [230, 230, 230],
    "pad_color": "0x0d0f12",
    "title": "Claude Code",
    # Pixels cut from the window edge: top drops the title bar (it shows the user
    # name, paths and process titles); bottom and sides drop the rounded corners,
    # where whatever is behind the window would show through.
    "inset": [34, 2, 20, 2],          # top, right, bottom, left
    # Headless (the default): Claude Code runs in a pty of this size and agg renders its output.
    # Nothing on the screen is captured, so other windows and notifications never reach a take.
    "size": [118, 30],                # columns, rows: at agg_font_size this nearly fills a 1920x1080 frame
    "agg_font_size": 26,
    "agg_theme": "custom",
    "agg_colors": "16181d,e6e6e6,1d1f21,e06c75,98c379,e5c07b,61afef,c678dd,56b6c2,d4d4d4,"
                  "5c6370,e06c75,98c379,e5c07b,61afef,c678dd,56b6c2,ffffff",
    "agg_font": "Menlo,SF Mono,Apple Symbols,Apple Color Emoji",
    "agg_line_height": 1.3,
    # Answer Claude Code's permission prompts with their first option (Yes) once a prompt has been on screen
    # this many seconds, so the viewer sees it. 0 turns it off.
    "approve_after": 1.5,
}

KEYS = {
    "enter": "\r", "return": "\r", "tab": "\t", "shift-tab": "\x1b[Z",
    "escape": "\x1b", "esc": "\x1b", "backspace": "\x7f", "space": " ",
    "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
    "home": "\x1b[H", "end": "\x1b[F", "pageup": "\x1b[5~", "pagedown": "\x1b[6~",
    "ctrl-c": "\x03", "ctrl-d": "\x04", "ctrl-l": "\x0c", "ctrl-r": "\x12",
    "ctrl-o": "\x0f", "ctrl-t": "\x14", "ctrl-g": "\x07",
}

# ---------------------------------------------------------------- ANSI text

_CSI = re.compile(r"\x1b\[([0-?]*)([ -/]*)([@-~])")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# answers a terminal sends back: cursor position (CSI r;c R), device attributes (CSI ?...c / CSI >...c),
# kitty keyboard flags (CSI ?n u), XTVERSION (DCS > | ... ST) and OSC replies
_REPLY = re.compile(r"\x1b\[\??\d+;\d+R|\x1b\[[?>][\d;]*c|\x1b\[\?\d*u|\x1bP>\|[^\x1b]*\x1b\\|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# a Claude Code permission prompt, with its first option selected
_PROMPT = re.compile(r"Do you want to [^\n]*\?")
_YES = re.compile(r"^\s*❯\s*1\.\s*Yes", re.M)
_ESC = re.compile(r"\x1b[()][A-Za-z0-9]|\x1b[@-Z\\-_=>78]")


def _csi_text(m):
    final, params = m.group(3), m.group(1)
    if final == "C":                       # cursor forward: keep words apart
        n = params.split(";")[0]
        return " " * (int(n) if n.isdigit() else 1)
    if final == "G":                       # column jump: Ink uses it in place of spaces
        return " "
    if final in "ABEFHfd":                 # cursor jumps: treat as a line break
        return "\n"
    return ""


def strip_ansi(s):
    s = _OSC.sub("", s)
    s = _CSI.sub(_csi_text, s)
    s = _ESC.sub("", s)
    return s.replace("\r", "")


class Screen:
    """A small VT100 screen: enough of Ink's cursor moves, erases and scrolls to read what is shown."""

    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.grid = [[" "] * cols for _ in range(rows)]
        self.x = self.y = 0
        self.top, self.bottom = 0, rows - 1
        self.saved = (0, 0)
        self.pending = ""

    def _scroll_up(self, n=1):
        for _ in range(n):
            del self.grid[self.top]
            self.grid.insert(self.bottom, [" "] * self.cols)

    def _scroll_down(self, n=1):
        for _ in range(n):
            del self.grid[self.bottom]
            self.grid.insert(self.top, [" "] * self.cols)

    def _lf(self):
        if self.y == self.bottom:
            self._scroll_up()
        elif self.y < self.rows - 1:
            self.y += 1

    def _put(self, ch):
        if self.x >= self.cols:
            self.x = 0
            self._lf()
        self.grid[self.y][self.x] = ch
        self.x += 1

    def _clip(self):
        self.x = max(0, min(self.cols - 1, self.x))
        self.y = max(0, min(self.rows - 1, self.y))

    def _csi(self, params, inter, final):
        if params.startswith("?") or inter:
            if params in ("?1049h", "?1049l", "?47h", "?47l", "?1047h", "?1047l") or \
                    (params.startswith("?") and params[1:] in ("1049", "47", "1047")):
                self.grid = [[" "] * self.cols for _ in range(self.rows)]
            return
        args = [int(a) if a.isdigit() else 0 for a in params.split(";")] if params else []
        a = args[0] if args else 0
        n = a or 1
        if final in "Hf":
            self.y, self.x = (args[0] or 1) - 1 if args else 0, (args[1] or 1) - 1 if len(args) > 1 else 0
        elif final == "A":
            self.y -= n
        elif final == "B":
            self.y += n
        elif final == "C":
            self.x += n
        elif final == "D":
            self.x -= n
        elif final == "E":
            self.y, self.x = self.y + n, 0
        elif final == "F":
            self.y, self.x = self.y - n, 0
        elif final == "G" or final == "`":
            self.x = n - 1
        elif final == "d":
            self.y = n - 1
        elif final == "J":
            rows = {0: range(self.y + 1, self.rows), 1: range(0, self.y), 2: range(self.rows), 3: range(self.rows)}
            if a == 0:
                self.grid[self.y][self.x:] = [" "] * (self.cols - self.x)
            elif a == 1:
                self.grid[self.y][:self.x + 1] = [" "] * (self.x + 1)
            for r in rows.get(a, ()):
                self.grid[r] = [" "] * self.cols
        elif final == "K":
            row = self.grid[self.y]
            if a == 0:
                row[self.x:] = [" "] * (self.cols - self.x)
            elif a == 1:
                row[:self.x + 1] = [" "] * (self.x + 1)
            else:
                self.grid[self.y] = [" "] * self.cols
        elif final == "X":
            row = self.grid[self.y]
            for c in range(self.x, min(self.cols, self.x + n)):
                row[c] = " "
        elif final == "P":
            row = self.grid[self.y]
            del row[self.x:self.x + n]
            row.extend([" "] * (self.cols - len(row)))
        elif final == "@":
            row = self.grid[self.y]
            for _ in range(n):
                row.insert(self.x, " ")
            del row[self.cols:]
        elif final in "LM" and self.top <= self.y <= self.bottom:
            top = self.top
            self.top = self.y
            (self._scroll_down if final == "L" else self._scroll_up)(n)
            self.top = top
        elif final == "S":
            self._scroll_up(n)
        elif final == "T":
            self._scroll_down(n)
        elif final == "r":
            self.top = (args[0] or 1) - 1 if args else 0
            self.bottom = (args[1] or self.rows) - 1 if len(args) > 1 else self.rows - 1
            self.x = self.y = 0
        elif final == "s":
            self.saved = (self.x, self.y)
        elif final == "u":
            self.x, self.y = self.saved
        self._clip()

    def feed(self, data):
        s = self.pending + data
        self.pending = ""
        i, n = 0, len(s)
        while i < n:
            ch = s[i]
            if ch == "\x1b":
                if i + 1 >= n:
                    self.pending = s[i:]
                    return
                nxt = s[i + 1]
                if nxt == "[":
                    m = _CSI.match(s, i)
                    if not m:
                        if n - i < 64:
                            self.pending = s[i:]
                            return
                        i += 2
                        continue
                    self._csi(m.group(1), m.group(2), m.group(3))
                    i = m.end()
                elif nxt == "]":
                    m = _OSC.match(s, i)
                    if not m:
                        if n - i < 4096:
                            self.pending = s[i:]
                            return
                        i += 2
                        continue
                    i = m.end()
                elif nxt == "7":
                    self.saved = (self.x, self.y)
                    i += 2
                elif nxt == "8":
                    self.x, self.y = self.saved
                    i += 2
                elif nxt == "M":
                    if self.y == self.top:
                        self._scroll_down()
                    else:
                        self.y = max(0, self.y - 1)
                    i += 2
                elif nxt == "D":
                    self._lf()
                    i += 2
                elif nxt in "()" and i + 2 < n:
                    i += 3
                else:
                    i += 2
            elif ch == "\r":
                self.x = 0
                i += 1
            elif ch in "\n\x0b\x0c":             # Claude Code's renderer counts on a newline returning the carriage
                self.x = 0
                self._lf()
                i += 1
            elif ch == "\b":
                self.x = max(0, self.x - 1)
                i += 1
            elif ch == "\t":
                self.x = min(self.cols - 1, (self.x // 8 + 1) * 8)
                i += 1
            elif ch < " " or ch == "\x7f":
                i += 1
            else:
                self._put(ch)
                if unicodedata.east_asian_width(ch) in "WF" and self.x < self.cols:
                    self.grid[self.y][self.x] = ""
                    self.x += 1
                i += 1

    def text(self):
        return "\n".join("".join(r).rstrip() for r in self.grid)


# ---------------------------------------------------------------- the lesson format

TOP_KEYS = ("title", "cwd", "command", "prepare", "env", "config", "setup", "steps", "timeout", "setup_timeout")
# The action keys, in the order Driver.run_step tries them, and the options each one takes.
ACTIONS = (
    ("say", ("wait",)),
    ("type", ("enter", "cps")),
    ("key", ("repeat", "gap")),
    ("wait_for", ("timeout", "idle", "optional")),
    ("idle", ("timeout", "optional")),
    ("pause", ()),
    ("shell", ("cwd", "optional")),
    ("wait_shell", ("timeout", "interval", "cwd", "optional")),
    ("wait_exit", ()),
)
NUMBER_OPTS = ("timeout", "idle", "cps", "repeat", "gap", "interval", "pause", "wait_exit")


def step_action(step):
    """The action of a step: the first action key it has, as Driver.run_step picks it."""
    for name, _ in ACTIONS:
        if name in step:
            return name
    return None


def check_step(step, where, extra_keys=()):
    """-> a list of problems with one step. extra_keys None: other keys are fine (the rig ignores them)."""
    if not isinstance(step, dict):
        return ["%s: a step is a JSON object" % where]
    act = step_action(step)
    if act is None:
        return ["%s: no action (one of %s)" % (where, ", ".join(a for a, _ in ACTIONS))]
    errs = []
    allowed = set(dict(ACTIONS)[act]) | {act, "when"} | set(extra_keys or ())
    for k in step:
        if k not in allowed and extra_keys is not None:
            errs.append("%s: %s step: unknown key %r" % (where, act, k))
    for k in NUMBER_OPTS:
        if k in step and k in allowed and (isinstance(step[k], bool) or not isinstance(step[k], (int, float))
                                            or step[k] < 0):
            errs.append("%s: %s must be a number >= 0" % (where, k))
    for k in ("wait", "enter", "optional"):
        if k in step and not isinstance(step[k], bool):
            errs.append("%s: %s must be true or false" % (where, k))
    if act in ("say", "type", "shell", "wait_shell", "wait_for") and not isinstance(step[act], str):
        errs.append("%s: %s must be a string" % (where, act))
    elif act in ("say", "shell", "wait_shell", "wait_for") and not step[act].strip():
        errs.append("%s: %s is empty" % (where, act))
    if act == "key" and (not isinstance(step["key"], str) or (step["key"] not in KEYS and len(step["key"]) != 1)):
        errs.append("%s: unknown key %r (one of %s, or one character)" % (where, step["key"], ", ".join(sorted(KEYS))))
    for k in ("wait_for", "when"):
        if isinstance(step.get(k), str):
            try:
                re.compile(step[k], re.M)
            except re.error as e:
                errs.append("%s: %s is not a valid regex: %s" % (where, k, e))
    return errs


def check_lesson(lesson, extra_keys=(), extra_step_keys=()):
    """-> a list of problems with a lesson (the format in README.md); empty when the rig can play it.

    Keys the rig does not know are problems, except those in extra_keys and extra_step_keys; with None,
    any other key is fine (the rig ignores it)."""
    if not isinstance(lesson, dict):
        return ["a lesson is a JSON object"]
    errs = ["unknown key %r" % k for k in lesson
            if k not in TOP_KEYS and extra_keys is not None and k not in extra_keys]
    cmd = lesson.get("command", ["claude"])
    if not (isinstance(cmd, list) and cmd and all(isinstance(c, str) and c for c in cmd)):
        errs.append("command must be a list of strings")
    for k in ("env",):
        v = lesson.get(k, {})
        if not (isinstance(v, dict) and all(isinstance(a, str) and isinstance(b, str) for a, b in v.items())):
            errs.append("env must map names to strings")
    if not (isinstance(lesson.get("prepare", []), list) and all(isinstance(c, str) for c in lesson.get("prepare", []))):
        errs.append("prepare must be a list of shell commands")
    for k in lesson.get("config", {}) or {}:
        if k not in DEFAULTS:
            errs.append("config: unknown key %r" % k)
    if not isinstance(lesson.get("steps"), list) or not lesson.get("steps"):
        errs.append("steps must be a non-empty list")
    for part in ("setup", "steps"):
        steps = lesson.get(part, [])
        if not isinstance(steps, list):
            errs.append("%s must be a list" % part)
            continue
        for n, st in enumerate(steps):
            where = "%s[%d]" % (part, n)
            errs += check_step(st, where, extra_step_keys)
            if part == "setup" and isinstance(st, dict) and step_action(st) == "say":
                errs.append("%s: narration runs only in steps, not in setup" % where)
    return errs


# ---------------------------------------------------------------- helpers

def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout


def osascript(script):
    p = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError("osascript failed: " + p.stderr.strip())
    return p.stdout.strip()


def media_duration(path):
    out = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "default=nw=1:nk=1", path])
    return float(out.strip())


def wait_file(path, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(path):
            return
        time.sleep(0.05)
    raise RuntimeError("timed out waiting for " + what)


def touch(path):
    with open(path, "w") as f:
        f.write(str(time.time()))


def find_claude():
    local = os.path.expanduser("~/.local/bin/claude")
    if os.path.exists(local):
        return local
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, "claude")
        if "cli-shims" not in d and os.access(p, os.X_OK):
            return p
    return "claude"


def scale_factor():
    try:
        out = sh(["system_profiler", "SPDisplaysDataType"])
        return 2 if "Retina" in out else 1
    except Exception:
        return 1


# ---------------------------------------------------------------- driver (runs inside the Terminal window)

class Driver:
    def __init__(self, plan):
        self.plan = plan
        self.ctl = plan["ctl"]
        self.raw = ""            # all child output, decoded
        self.mark = 0            # offset in self.raw where the current search window starts
        self.last_out = time.time()
        self.events = []
        self.pid = None
        self.fd = None
        self.screen = None
        self.headless = bool(plan.get("headless"))
        self.prompt_since, self.approved_at = None, 0.0
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.cast = open(self.ctl + "/out.jsonl", "w") if self.headless else None

    # -- pty plumbing
    def start_child(self):
        import pty
        if self.headless:
            cols, rows = self.plan["size"]
        else:
            cols, rows = os.get_terminal_size(1)
            rows -= self.plan.get("rows_margin", 0)
        self.size = (cols, rows)
        self.screen = Screen(cols, rows)
        pid, fd = pty.fork()
        if pid == 0:
            import fcntl
            import struct
            import termios
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.chdir(self.plan["cwd"])
            env = dict(os.environ)
            for k in list(env):
                if k.startswith("CLAUDECODE") or k.startswith("CLAUDE_CODE_"):
                    env.pop(k)
            env.update(self.plan.get("env", {}))
            env.setdefault("TERM", "xterm-256color")
            os.execvpe(self.plan["command"][0], self.plan["command"], env)
        self.pid, self.fd = pid, fd

    def alive(self):
        if self.pid is None:
            return False
        try:
            r, _ = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            return False
        if r == 0:
            return True
        self.pid = None
        return False

    def pump(self, dt):
        """Read child output for up to dt seconds, mirroring it to the window."""
        end = time.time() + dt
        while True:
            left = end - time.time()
            if left <= 0 or self.fd is None:
                return
            r, _, _ = select.select([self.fd] + ([] if self.headless else [0]), [], [], left)
            if 0 in r:
                self.relay_replies()
            if self.fd not in r:
                continue
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                data = b""
            if not data:
                self.fd = None
                return
            text = self.decoder.decode(data)       # a character can be split across reads
            if self.headless:
                self.cast.write(json.dumps([time.time(), text]) + "\n")
                self.cast.flush()
            else:
                os.write(1, data)
            self.raw += text
            if self.screen is not None:
                self.screen.feed(text)
            if self.headless:
                self.answer_queries(text)
            self.maybe_approve()
            self.last_out = time.time()

    def maybe_approve(self):
        after = self.plan.get("approve_after", 0)
        if not after or self.screen is None or self.fd is None:
            return
        shown = self.screen.text()
        if not (_PROMPT.search(shown) and _YES.search(shown)):
            self.prompt_since = None
            return
        now = time.time()
        if self.prompt_since is None:
            self.prompt_since = now
        elif now - self.prompt_since >= after and now - self.approved_at >= after:
            os.write(self.fd, b"\r")
            self.approved_at = now
            self.prompt_since = None
            last = [l.strip() for l in shown.splitlines() if _PROMPT.search(l)]
            self.events.append({"i": -1, "kind": "approve", "t": now, "t_end": now,
                                "step": {"approve": last[-1] if last else ""}})

    def answer_queries(self, text):
        """Headless: answer the terminal queries Claude Code sends, as a real terminal would."""
        out = ""
        for m in _CSI.finditer(text):
            params, final = m.group(1), m.group(3)
            if final == "c" and params in ("", "0"):
                out += "\x1b[?62;22c"
            elif final == "n" and params in ("6", "?6"):
                out += "\x1b[%s%d;%dR" % ("?" if params == "?6" else "", self.screen.y + 1, self.screen.x + 1)
            elif final == "n" and params == "5":
                out += "\x1b[0n"
        if out and self.fd is not None:
            os.write(self.fd, out.encode())

    def relay_replies(self):
        """Pass the terminal's answers to Claude Code's queries (cursor position, device attributes, version,
        keyboard mode) on to the child. Keystrokes and mouse reports typed into the window are dropped."""
        try:
            data = os.read(0, 4096).decode("utf-8", "replace")
        except OSError:
            return
        buf = getattr(self, "reply_buf", "") + data
        out, last = [], 0
        for m in _REPLY.finditer(buf):
            out.append(m.group(0))
            last = m.end()
        esc = buf.rfind("\x1b", last)
        self.reply_buf = buf[esc:][:256] if esc >= 0 else ""   # an answer split across reads
        if out and self.fd is not None:
            os.write(self.fd, "".join(out).encode())

    def send(self, s):
        os.write(self.fd, s.encode())

    def shell_env(self):
        env = dict(os.environ)
        env.update(self.plan.get("env", {}))
        return env

    def text_since_mark(self):
        """What a pattern is matched against: the output since the last keystroke, then the screen as shown."""
        shown = self.screen.text() if self.screen is not None else ""
        return strip_ansi(self.raw[self.mark:]) + "\n" + shown

    # -- steps
    def log(self, i, step, kind, t0, **extra):
        ev = {"i": i, "kind": kind, "t": t0, "t_end": time.time(), "step": step}
        ev.update(extra)
        self.events.append(ev)

    def wait_idle(self, idle, timeout):
        end = time.time() + timeout
        while time.time() < end:
            self.pump(0.1)
            if time.time() - self.last_out >= idle:
                return True
        return False

    def run_step(self, i, step):
        t0 = time.time()
        when = step.get("when")
        if when and not re.search(when, self.text_since_mark(), re.M):
            self.log(i, step, "skipped", t0)
            return
        if "say" in step:
            dur = step.get("_duration", 0)
            if step.get("wait", True):
                self.pump(dur + self.plan["say_gap"])
            self.log(i, step, "say", t0)
        elif "type" in step:
            self.mark = len(self.raw)
            # a long prompt types faster, so it never takes more than about 6 seconds
            cps = step.get("cps") or max(self.plan["type_cps"], len(step["type"]) / 6.0)
            for ch in step["type"]:
                self.send(ch)
                self.pump(1.0 / cps)
            if step.get("enter"):
                self.pump(0.3)
                self.send("\r")
            self.log(i, step, "type", t0)
        elif "key" in step:
            self.mark = len(self.raw)
            seq = KEYS.get(step["key"], step["key"])
            for _ in range(step.get("repeat", 1)):
                self.send(seq)
                self.pump(step.get("gap", 0.15))
            self.log(i, step, "key", t0)
        elif "wait_for" in step:
            pat = re.compile(step["wait_for"], re.M)
            timeout = step.get("timeout", 120)
            end = t0 + timeout
            hit = False
            while time.time() < end:
                if pat.search(self.text_since_mark()):
                    hit = True
                    break
                self.pump(0.1)
            idle_ok = None
            if hit and step.get("idle"):
                idle_ok = self.wait_idle(step["idle"], max(1.0, end - time.time()))
            if not hit and not step.get("optional"):
                raise RuntimeError("wait_for timed out: %r" % step["wait_for"])
            self.log(i, step, "wait_for", t0, matched=hit, idle_ok=idle_ok)
        elif "idle" in step:
            ok = self.wait_idle(step["idle"], step.get("timeout", 120))
            if not ok and not step.get("optional"):
                raise RuntimeError("idle timed out")
            self.log(i, step, "idle", t0, idle_ok=ok)
        elif "pause" in step:
            self.pump(step["pause"])
            self.log(i, step, "pause", t0)
        elif "shell" in step:
            p = subprocess.run(step["shell"], shell=True, cwd=step.get("cwd", self.plan["cwd"]),
                               env=self.shell_env(), capture_output=True, text=True)
            self.log(i, step, "shell", t0, rc=p.returncode, out=(p.stdout + p.stderr)[-2000:])
            if p.returncode != 0 and not step.get("optional"):
                raise RuntimeError("shell step failed: %s" % step["shell"])
        elif "wait_shell" in step:
            # poll a command off camera until it exits 0, mirroring the screen in between
            end = t0 + step.get("timeout", 600)
            ok, p = False, None
            while True:
                p = subprocess.run(step["wait_shell"], shell=True, cwd=step.get("cwd", self.plan["cwd"]),
                                   env=self.shell_env(), capture_output=True, text=True)
                if p.returncode == 0:
                    ok = True
                    break
                if time.time() >= end:
                    break
                self.pump(min(step.get("interval", 5), max(0.1, end - time.time())))
            self.log(i, step, "wait_shell", t0, rc=p.returncode, matched=ok, out=(p.stdout + p.stderr)[-2000:])
            if not ok and not step.get("optional"):
                raise RuntimeError("wait_shell timed out: %s" % step["wait_shell"])
        elif "wait_exit" in step:
            end = t0 + step["wait_exit"]
            while time.time() < end and self.alive():
                self.pump(0.1)
            self.log(i, step, "wait_exit", t0, exited=not self.alive())
        else:
            raise RuntimeError("unknown step: %r" % step)

    def main(self):
        import termios
        import tty
        saved = None
        if not self.headless:
            saved = termios.tcgetattr(0)
            tty.setraw(0)                   # keystrokes in the window never reach the screen
        try:
            if not self.headless:
                os.write(1, b"\x1b[2J\x1b[3J\x1b[H")
            wait_file(self.ctl + "/start", 60, "start")
            self.start_child()
            for i, step in enumerate(self.plan["setup"]):
                self.run_step(-1 - i, step)
            self.events = []
            touch(self.ctl + "/ready")
            while not os.path.exists(self.ctl + "/go"):
                self.pump(0.05)
            for i, step in enumerate(self.plan["steps"]):
                self.run_step(i, step)
            self.pump(0.2)
        except Exception as e:  # report to the rig, never hang the window
            with open(self.ctl + "/error", "w") as f:
                f.write(repr(e))
        finally:
            with open(self.ctl + "/events.json", "w") as f:
                json.dump({"size": getattr(self, "size", None), "events": self.events}, f, indent=1)
            with open(self.ctl + "/screen.txt", "w") as f:
                f.write(self.screen.text() if self.screen is not None else strip_ansi(self.raw))
            with open(self.ctl + "/raw.txt", "w", newline="") as f:
                f.write(self.raw)
            with open(self.ctl + "/stream.txt", "w") as f:
                f.write(strip_ansi(self.raw))
            touch(self.ctl + "/done")
            end = time.time() + 60
            while time.time() < end and not os.path.exists(self.ctl + "/release"):
                self.pump(0.1)
            if self.alive():                # off camera now: ask nicely, then insist
                try:
                    self.send("\x03")
                    self.pump(0.3)
                    self.send("\x03")
                except OSError:
                    pass
                end = time.time() + 5
                while time.time() < end and self.alive():
                    self.pump(0.1)
            if self.alive():
                os.kill(self.pid, signal.SIGTERM)
            if saved is not None:
                termios.tcsetattr(0, termios.TCSADRAIN, saved)
            if self.cast:
                self.cast.close()


# ---------------------------------------------------------------- Terminal window

def open_window(cfg, driver_cmd):
    x, y, w, h = cfg["bounds"]
    bg = ", ".join(str(c * 257) for c in cfg["background"])
    fg = ", ".join(str(c * 257) for c in cfg["foreground"])
    script = '''
tell application "Terminal"
    if not (exists settings set "wavecoord-course") then
        make new settings set with properties {name:"wavecoord-course"}
    end if
    set s to settings set "wavecoord-course"
    set font name of s to "%(font)s"
    set font size of s to %(size)d
    set font antialiasing of s to true
    set background color of s to {%(bg)s}
    set normal text color of s to {%(fg)s}
    set bold text color of s to {65535, 65535, 65535}
    set cursor color of s to {%(fg)s}
    try
    set title displays custom title of s to true
    end try
    try
    set custom title of s to "%(title)s"
    end try
    try
    set title displays shell path of s to false
    end try
    try
    set title displays window size of s to false
    end try
    try
    set title displays device name of s to false
    end try
    try
    set title displays file name of s to false
    end try
    try
    set title displays settings name of s to false
    end try
    set t to do script "%(cmd)s"
    set current settings of t to s
    delay 0.3
    set w to missing value
    repeat with cand in windows
        if selected tab of cand is t then set w to cand
    end repeat
    if w is missing value then set w to window 1
    set bounds of w to {%(x)d, %(y)d, %(r)d, %(b)d}
    set index of w to 1
    activate
    delay 0.5
    set bb to bounds of w
    return ((id of w) as text) & "," & (item 1 of bb as text) & "," & (item 2 of bb as text) & "," & (item 3 of bb as text) & "," & (item 4 of bb as text)
end tell
''' % {"font": cfg["font"], "size": cfg["font_size"], "bg": bg, "fg": fg,
       "title": cfg["title"].replace('"', ""), "cmd": driver_cmd.replace("\\", "\\\\").replace('"', '\\"'),
       "x": x, "y": y, "r": x + w, "b": y + h}
    out = osascript(script)
    wid, l, t, r, b = [int(float(v)) for v in out.split(",")]
    return wid, (l, t, r - l, b - t)


def front_window(wid):
    osascript('tell application "Terminal"\nset index of window id %d to 1\nactivate\nend tell' % wid)


def close_window(wid):
    try:
        osascript('tell application "Terminal" to close window id %d saving no' % wid)
    except Exception:
        pass


# ---------------------------------------------------------------- recording

class Recorder:
    def __init__(self, rect, inset, out, fps, factor):
        x, y, w, h = [v * factor for v in rect]
        it, ir, ib, il = inset
        x, y, w, h = x + il, y + it, w - il - ir, h - it - ib
        w, h = w - w % 2, h - h % 2
        self.size = (w, h)
        self.path = out
        # The crop runs before any encoder: no full-screen frame is ever written.
        self.cmd = ["ffmpeg", "-hide_banner", "-y", "-f", "avfoundation", "-framerate", str(fps),
                    "-capture_cursor", "0", "-pix_fmt", "uyvy422", "-i", "0:none",
                    "-vf", "crop=%d:%d:%d:%d,format=yuv420p" % (w, h, x, y),
                    "-r", str(fps), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "14", out]
        self.first_frame = threading.Event()
        self.log = []

    def start(self):
        self.p = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        self.t_spawn = time.time()

        def reader():
            buf = b""
            while True:
                c = self.p.stderr.read(1)
                if not c:
                    break
                buf += c
                if c in b"\r\n":
                    line = buf.decode("utf-8", "replace").strip()
                    buf = b""
                    if line:
                        self.log.append(line)
                    if line.startswith("frame=") and not self.first_frame.is_set():
                        self.first_frame.set()
        threading.Thread(target=reader, daemon=True).start()
        if not self.first_frame.wait(20):
            self.p.kill()
            raise RuntimeError("ffmpeg produced no frames:\n" + "\n".join(self.log[-15:]))

    def stop(self):
        self.t_stop = time.time()
        try:
            self.p.stdin.write(b"q")
            self.p.stdin.flush()
        except Exception:
            pass
        try:
            self.p.wait(20)
        except subprocess.TimeoutExpired:
            self.p.send_signal(signal.SIGINT)
            self.p.wait(10)


# ---------------------------------------------------------------- narration, subtitles, mux

def tts(text, path, voice, rate):
    subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", path, text], check=True)
    return media_duration(path)


def srt_time(t):
    t = max(0.0, t)
    ms = int(round(t * 1000))
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


def wrap2(text, width=42):
    words, lines, cur = text.split(), [], ""
    for wd in words:
        if cur and len(cur) + 1 + len(wd) > width:
            lines.append(cur)
            cur = wd
        else:
            cur = (cur + " " + wd).strip()
    lines.append(cur)
    return "\n".join(lines)


def cues_for(text, start, dur, maxlen=84):
    """Split one narration line into subtitle cues, timed by length."""
    parts = []
    for sent in re.split(r"(?<=[.!?])\s+", text.strip()):
        cur = ""
        for wd in sent.split():
            if cur and len(cur) + 1 + len(wd) > maxlen:
                parts.append(cur)
                cur = wd
            else:
                cur = (cur + " " + wd).strip()
        if cur:
            parts.append(cur)
    total = sum(len(p) for p in parts) or 1
    t, out = start, []
    for p in parts:
        d = dur * len(p) / total
        out.append((t, t + d, wrap2(p)))
        t += d
    return out


# API list price, USD per million tokens: input, output (cache write = 1.25x input,
# cache read = 0.1x input). On a Claude subscription nothing is billed per token.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-haiku-4-5": (1.0, 5.0),
          "claude-sonnet-5-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0)}


def config_dir(env=None):
    """Claude Code's config dir: CLAUDE_CONFIG_DIR of `env` (the lesson's), else of this process, else ~/.claude."""
    d = (env or {}).get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    return os.path.expanduser(d)


def usage_of(paths):
    """Sum the token usage of Claude Code transcripts (.jsonl files), priced per model."""
    seen, tot, by_model = set(), {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}, {}
    for p in paths:
        with open(p, errors="replace") as f:
            lines = f.readlines()
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            msg = rec.get("message") or {}
            u = msg.get("usage")
            if rec.get("type") != "assistant" or not u or msg.get("id") in seen:
                continue
            seen.add(msg.get("id"))
            row = (u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("cache_creation_input_tokens", 0),
                   u.get("cache_read_input_tokens", 0))
            m = by_model.setdefault(msg.get("model") or "?", [0, 0, 0, 0])
            for k, v in enumerate(row):
                m[k] += v
            for k, v in zip(("input", "output", "cache_write", "cache_read"), row):
                tot[k] += v
    usd, priced = 0.0, True
    for model, (i, o, cw, cr) in by_model.items():
        if model not in PRICES:
            priced = False
            continue
        pin, pout = PRICES[model]
        usd += (i * pin + o * pout + cw * pin * 1.25 + cr * pin * 0.1) / 1e6
    return {"models": sorted(m for m in by_model if m != "?"), "tokens": tot, "requests": len(seen),
            "usd_at_api_list_price": round(usd, 5) if by_model and priced else None}


def transcripts(projects_dir, since, prefix=None):
    """The .jsonl transcripts under projects_dir written after `since`; with `prefix`, only the project
    folders whose name starts with it (Claude Code names a folder after the session's cwd)."""
    out = []
    if not os.path.isdir(projects_dir):
        return out
    for proj in sorted(os.listdir(projects_dir)):
        if prefix and not proj.startswith(prefix):
            continue
        d = os.path.join(projects_dir, proj)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if name.endswith(".jsonl") and os.path.getmtime(p) >= since:
                out.append(p)
    return out


def project_key(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def claude_usage(cwd, since, env=None):
    """Token usage of the sessions started in cwd after `since` (the lesson's CLAUDE_CONFIG_DIR)."""
    projects = os.path.join(config_dir(env), "projects")
    paths = []
    for key in sorted({project_key(os.path.realpath(cwd)), project_key(cwd)}):
        paths += [p for p in transcripts(projects, since) if os.path.basename(os.path.dirname(p)) == key]
    return usage_of(sorted(set(paths))) if paths else None


def mux(raw, clips, srt, out, cfg, rect_size):
    cw, ch = cfg["canvas"] or rect_size
    vw, vh = rect_size
    vf = "null" if (cw, ch) == (vw, vh) else \
        "scale=%d:%d:force_original_aspect_ratio=decrease:flags=lanczos,pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=%s" % (
            cw, ch, cw, ch, cfg["pad_color"]) if vw > cw or vh > ch else \
        "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=%s" % (cw, ch, cfg["pad_color"])
    cmd = ["ffmpeg", "-hide_banner", "-y", "-i", raw]
    for c in clips:
        cmd += ["-i", c["path"]]
    cmd += ["-i", srt]
    parts = ["[0:v]%s[v]" % vf]
    labels = ""
    for k, c in enumerate(clips, 1):
        ms = int(round(c["at"] * 1000))
        parts.append("[%d:a]aresample=48000,adelay=%d:all=1[a%d]" % (k, ms, k))
        labels += "[a%d]" % k
    if clips:
        parts.append("%samix=inputs=%d:normalize=0:dropout_transition=0,apad[a]" % (labels, len(clips)))
    cmd += ["-filter_complex", ";".join(parts), "-map", "[v]"]
    if clips:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
    cmd += ["-map", "%d:s" % (len(clips) + 1), "-c:s", "mov_text", "-metadata:s:s:0", "language=eng",
            "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-tune", "stillimage",
            "-pix_fmt", "yuv420p", "-r", str(cfg["fps"]), "-t", "%.3f" % media_duration(raw), "-movflags", "+faststart", out]
    subprocess.run(cmd, check=True, capture_output=True)


# ---------------------------------------------------------------- headless render

def render_cast(ctl, out, cfg, origin, end):
    """Turn the driver's output log into an asciicast (t=0 is `origin`; output before it shows in the first frame),
    render it with agg and convert the GIF to raw.mp4. Returns (path, (width, height))."""
    cols, rows = cfg["size"]
    cast = os.path.join(out, "session.cast")
    with open(cast, "w") as f:
        f.write(json.dumps({"version": 2, "width": cols, "height": rows, "env": {"TERM": "xterm-256color"}}) + "\n")
        with open(os.path.join(ctl, "out.jsonl")) as src:
            for line in src:
                t, text = json.loads(line)
                text = text.replace("\xa0", " ")       # agg draws a no-break space as a stray glyph
                f.write(json.dumps([round(max(0.0, t - origin), 4), "o", text]) + "\n")
        f.write(json.dumps([round(end - origin, 4), "o", "\x1b[0m"]) + "\n")
    gif = os.path.join(out, "raw.gif")
    cmd = ["agg", "--font-size", str(cfg["agg_font_size"]), "--line-height", str(cfg["agg_line_height"]),
           "--font-family", cfg["agg_font"], "--idle-time-limit", "100000", "--fps-cap", str(cfg["fps"]),
           "--last-frame-duration", "0", "--no-loop", "--theme", cfg["agg_theme"]]
    if cfg["agg_theme"] == "custom":
        cmd[-1] = cfg["agg_colors"]
    subprocess.run(cmd + [cast, gif], check=True, capture_output=True)
    raw = os.path.join(out, "raw.mp4")
    subprocess.run(["ffmpeg", "-hide_banner", "-y", "-i", gif, "-vf",
                    "fps=%d,scale=trunc(iw/2)*2:trunc(ih/2)*2:flags=neighbor,format=yuv420p" % cfg["fps"],
                    "-c:v", "libx264", "-preset", "slow", "-crf", "14", "-tune", "stillimage",
                    "-t", "%.3f" % (end - origin), raw], check=True, capture_output=True)
    os.remove(gif)
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", raw],
                       capture_output=True, text=True, check=True)
    w, h = (int(x) for x in p.stdout.strip().split(",")[:2])
    return raw, (w, h)


# ---------------------------------------------------------------- run

def run(args):
    lesson_path = os.path.abspath(args.lesson)
    lesson = json.load(open(lesson_path))
    cfg = dict(DEFAULTS)
    cfg.update(lesson.get("config", {}))
    base = os.path.dirname(lesson_path)
    cwd = args.cwd or os.path.expanduser(os.path.expandvars(lesson.get("cwd", ".")))
    cwd = os.path.abspath(os.path.join(base, cwd))
    name = os.path.splitext(os.path.basename(lesson_path))[0]
    out = os.path.abspath(args.out or os.path.join(os.getcwd(), "course-out", name))
    ctl = os.path.join(out, "ctl")
    shutil.rmtree(ctl, ignore_errors=True)
    os.makedirs(os.path.join(out, "voice"), exist_ok=True)
    os.makedirs(ctl)

    for cmd in lesson.get("prepare", []):
        subprocess.run(cmd, shell=True, cwd=cwd, check=True)

    command = lesson.get("command", ["claude"])
    if command and command[0] == "claude":
        command = [find_claude()] + command[1:]

    # 1. narration first, so the driver knows how long each line takes
    steps = lesson["steps"]
    for i, st in enumerate(steps):
        if "say" in st:
            p = os.path.join(out, "voice", "%03d.aiff" % i)
            st["_voice"] = p
            st["_duration"] = tts(st["say"], p, cfg["voice"], cfg["rate"])

    headless = not args.window
    plan = {"cwd": cwd, "command": command, "env": lesson.get("env", {}), "ctl": ctl,
            "setup": lesson.get("setup", []), "steps": steps,
            "say_gap": cfg["say_gap"], "type_cps": cfg["type_cps"], "rows_margin": cfg["rows_margin"],
            "headless": headless, "size": cfg["size"], "approve_after": cfg["approve_after"]}
    plan_path = os.path.join(ctl, "plan.json")
    json.dump(plan, open(plan_path, "w"), indent=1)
    if headless:
        return run_headless(args, lesson, cfg, cwd, name, out, ctl, plan_path)

    # 2. the dedicated window
    driver_cmd = "clear; exec %s %s _drive %s" % (shlex.quote(sys.executable),
                                                  shlex.quote(os.path.abspath(__file__)),
                                                  shlex.quote(plan_path))
    wid, rect = open_window(cfg, driver_cmd)
    print("window %d at %r" % (wid, rect), flush=True)
    rec = None
    t_start = time.time()
    try:
        touch(os.path.join(ctl, "start"))
        wait_ready = time.time() + lesson.get("setup_timeout", 120)
        while not os.path.exists(os.path.join(ctl, "ready")):
            if os.path.exists(os.path.join(ctl, "error")) or time.time() > wait_ready:
                raise RuntimeError("setup failed: " + open(os.path.join(ctl, "error")).read()
                                   if os.path.exists(os.path.join(ctl, "error")) else "setup timed out")
            time.sleep(0.1)
        front_window(wid)
        time.sleep(0.5)

        # 3. record only the window rectangle
        raw = os.path.join(out, "raw.mp4")
        rec = Recorder(rect, cfg["inset"], raw, cfg["fps"], args.scale_factor or scale_factor())
        rec.start()
        time.sleep(cfg["lead_in"])
        touch(os.path.join(ctl, "go"))
        wait_file(os.path.join(ctl, "done"), lesson.get("timeout", 900), "lesson end")
        time.sleep(cfg["tail"])
        rec.stop()
    finally:
        if rec and rec.p.poll() is None:
            rec.stop()
        touch(os.path.join(ctl, "release"))
        time.sleep(1.0)
        close_window(wid)

    if os.path.exists(os.path.join(ctl, "error")):
        print("driver error: " + open(os.path.join(ctl, "error")).read(), file=sys.stderr)
    dur = media_duration(raw)
    return finish(lesson, cfg, cwd, name, out, ctl, raw, rec.t_stop - dur, rec.size, rect, t_start)


def finish(lesson, cfg, cwd, name, out, ctl, raw, t0, size, rect, t_start):
    """Timeline, narration, subtitles and the final mux; t0 is the wall-clock time of the first video frame."""
    dur = media_duration(raw)
    events = json.load(open(os.path.join(ctl, "events.json")))["events"]
    timeline, clips, cues = [], [], []
    for ev in events:
        row = {"i": ev["i"], "kind": ev["kind"], "at": round(ev["t"] - t0, 3),
               "end": round(ev["t_end"] - t0, 3)}
        for k in ("matched", "idle_ok", "rc", "exited"):
            if k in ev:
                row[k] = ev[k]
        st = ev["step"]
        row["what"] = st.get("approve") or st.get("say") or st.get("type") or st.get("key") or st.get("wait_for") or \
            st.get("shell") or st.get("wait_shell") or ("idle %s" % st["idle"] if "idle" in st else "") or \
            ("pause %s" % st["pause"] if "pause" in st else "")
        timeline.append(row)
        if ev["kind"] == "say":
            clips.append({"path": st["_voice"], "at": row["at"], "dur": st["_duration"]})
            cues += cues_for(st["say"], row["at"], st["_duration"])
    srt = os.path.join(out, name + ".srt")
    with open(srt, "w") as f:
        for n, (a, b, text) in enumerate(cues, 1):
            f.write("%d\n%s --> %s\n%s\n\n" % (n, srt_time(a), srt_time(b), text))
    with open(os.path.join(out, "timeline.json"), "w") as f:
        json.dump({"video_duration": dur, "window": rect, "steps": timeline}, f, indent=1)

    final = os.path.join(out, name + ".mp4")
    mux(raw, clips, srt, final, cfg, size)
    usage = claude_usage(cwd, t_start, lesson.get("env"))
    if usage:
        json.dump(usage, open(os.path.join(out, "usage.json"), "w"), indent=1)
    for row in timeline:
        print("%7.2fs  %-9s %s" % (row["at"], row["kind"], row["what"][:70]))
    print("video   %s (%.1fs)" % (final, media_duration(final)))
    print("srt     %s" % srt)
    if usage:
        print("usage   %s" % json.dumps(usage))
    return 1 if os.path.exists(os.path.join(ctl, "error")) else 0


def run_headless(args, lesson, cfg, cwd, name, out, ctl, plan_path):
    t_start = time.time()
    drv = subprocess.Popen([sys.executable, os.path.abspath(__file__), "_drive", plan_path],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=open(os.path.join(ctl, "driver.log"), "w"))
    print("headless %dx%d" % tuple(cfg["size"]), flush=True)
    try:
        touch(os.path.join(ctl, "start"))
        wait_ready = time.time() + lesson.get("setup_timeout", 120)
        while not os.path.exists(os.path.join(ctl, "ready")):
            if os.path.exists(os.path.join(ctl, "error")) or time.time() > wait_ready or drv.poll() is not None:
                raise RuntimeError("setup failed: " + open(os.path.join(ctl, "error")).read()
                                   if os.path.exists(os.path.join(ctl, "error")) else "setup timed out")
            time.sleep(0.1)
        origin = time.time()
        time.sleep(cfg["lead_in"])
        touch(os.path.join(ctl, "go"))
        wait_file(os.path.join(ctl, "done"), lesson.get("timeout", 900), "lesson end")
        time.sleep(cfg["tail"])
        end = time.time()
    finally:
        touch(os.path.join(ctl, "release"))
        try:
            drv.wait(90)
        except subprocess.TimeoutExpired:
            drv.kill()
    if os.path.exists(os.path.join(ctl, "error")):
        print("driver error: " + open(os.path.join(ctl, "error")).read(), file=sys.stderr)
    raw, size = render_cast(ctl, out, cfg, origin, end)
    return finish(lesson, cfg, cwd, name, out, ctl, raw, origin, size, None, t_start)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="record a lesson")
    r.add_argument("lesson")
    r.add_argument("--cwd", help="working directory for the session (overrides the lesson)")
    r.add_argument("--out", help="output directory (default ./course-out/<lesson>)")
    r.add_argument("--scale-factor", type=int, help="screen pixels per point (default: auto)")
    r.add_argument("--window", action="store_true",
                   help="record a Terminal window on screen instead of rendering headless with agg")
    c = sub.add_parser("check", help="check lesson files against the format; exit 1 on a problem")
    c.add_argument("lessons", nargs="+")
    d = sub.add_parser("_drive")
    d.add_argument("plan")
    a = ap.parse_args()
    if a.cmd == "check":
        bad = 0
        for path in a.lessons:
            errs = check_lesson(json.load(open(path)), extra_keys=None, extra_step_keys=None)
            print("%s: %s" % (path, "ok" if not errs else "%d problems" % len(errs)))
            for e in errs:
                print("  " + e)
            bad += bool(errs)
        return 1 if bad else 0
    if a.cmd == "_drive":
        Driver(json.load(open(a.plan))).main()
        return 0
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
