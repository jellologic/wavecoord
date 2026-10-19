#!/usr/bin/env python3
"""Terminal screenshots for the docs: real `wavecoord` output, rendered as SVG.

    python3 scripts/docs-screens.py                 # writes docs/img/*.svg
    python3 scripts/docs-screens.py --out /tmp/img --keep

It builds a throwaway consumer repo (a small "notes" app spec tree, a bare origin), starts a real coordinator
(`wavecoord serve`) on a free loopback port, and drives a wave of work with the fakes of tests/ on PATH
(tests/fake-claude/claude, tests/fake-gh/gh): no model, no network. At each moment it runs the real CLI and
renders its stdout as a terminal window. Temp paths show as ~/demo, the port as 18741, and times and ids that
change from run to run are pinned, so a re-run gives near-identical files. Stdlib only.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
FAKE_CLAUDE_DIR = os.path.join(ROOT, "tests", "fake-claude")
FAKE_GH_DIR = os.path.join(ROOT, "tests", "fake-gh")
SHOWN_PORT = 18741
DEMO = "~/demo"
GIT_DATE = "2026-10-19T09:30:00+00:00"
TS_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z")
PARENT_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_MESSAGING_SOCKET",
              "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PLUGIN_OPTION_TOKEN", "CLAUDE_PLUGIN_OPTION_COORD_URL",
              "WAVECOORD_URL", "WAVECOORD_TOKEN", "WAVECOORD_TOKEN_FILE", "WAVECOORD_ROLE", "WAVECOORD_TASK",
              "WAVECOORD_AGENT_ID", "WAVECOORD_ATTEMPT_DIR", "WAVECOORD_STATE_DIR", "WAVECOORD_HOME",
              "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
              "CLAUDE_CONFIG_DIR")


# ---------------------------------------------------------------- the SVG renderer

ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")
OTHER_ESC_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-ln-z]|\x1b\][^\x07]*\x07|\r")
PALETTE = {30: "#45475a", 31: "#f38ba8", 32: "#a6e3a1", 33: "#f9e2af", 34: "#89b4fa", 35: "#f5c2e7", 36: "#94e2d5",
           37: "#bac2de", 90: "#6c7086", 91: "#f38ba8", 92: "#a6e3a1", 93: "#f9e2af", 94: "#89b4fa", 95: "#f5c2e7",
           96: "#94e2d5", 97: "#ffffff"}
FG, BG, BAR, DIM, PROMPT = "#cdd6f4", "#1e1e2e", "#313244", "#9399b2", "#a6e3a1"
FONT = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'DejaVu Sans Mono', 'Liberation Mono', monospace"
FONT_PX, CHAR_W, LINE_H, PAD = 13, 8.0, 18, 16  # CHAR_W: a little over 0.6 em, so no font overflows


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def ansi_spans(text):
    """[(text, style)] for one line; style is (color or None, bold). Unknown escapes are dropped."""
    text = OTHER_ESC_RE.sub("", text)
    out, color, bold, pos = [], None, False, 0
    for m in ANSI_RE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], (color, bold)))
        for code in [int(c) if c else 0 for c in m.group(1).split(";")]:
            if code == 0:
                color, bold = None, False
            elif code == 1:
                bold = True
            elif code == 22:
                bold = False
            elif code == 39:
                color = None
            elif code in PALETTE:
                color = PALETTE[code]
            elif code == 2:
                color = DIM
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], (color, bold)))
    return out


def wrap_spans(spans, cols):
    """Split one line's spans into rows of at most `cols` characters (a hard wrap; break at a space when one
    is near the end of the row)."""
    flat = [(ch, st) for t, st in spans for ch in t.expandtabs(8)]
    rows = []
    while len(flat) > cols:
        cut = cols
        for i in range(cols, max(cols - 24, 1), -1):
            if flat[i - 1][0] == " ":
                cut = i
                break
        rows.append(flat[:cut])
        flat = flat[cut:]
    rows.append(flat)
    out = []
    for row in rows:
        merged = []
        for ch, st in row:
            if merged and merged[-1][1] == st:
                merged[-1][0] += ch
            else:
                merged.append([ch, st])
        out.append([(t, st) for t, st in merged])
    return out


def render_svg(title, blocks, cols=100, max_rows=60):
    """A terminal window as SVG. blocks: [(command, output)]; each is shown as `$ command` then its output.
    Lines longer than `cols` wrap; more than `max_rows` rows are clipped with a marker line."""
    rows = []
    for i, (cmd, output) in enumerate(blocks):
        if i:
            rows.append([])
        for r in wrap_spans([("$ ", (PROMPT, True)), (cmd, (FG, True))], cols):
            rows.append(r)
        for line in output.rstrip("\n").split("\n") if output.strip() else []:
            rows.extend(wrap_spans(ansi_spans(line), cols))
    if len(rows) > max_rows:
        rows = rows[:max_rows - 1] + [[("... (%d more lines)" % (len(rows) - max_rows + 1), (DIM, False))]]
    width_cols = max([60] + [sum(len(t) for t, _ in r) for r in rows])
    width = int(PAD * 2.5 + width_cols * CHAR_W)
    top = 36
    height = int(top + PAD + len(rows) * LINE_H + PAD - 4)
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img" '
           'aria-label="%s">' % (width, height, width, height, esc(title)),
           '<title>%s</title>' % esc(title),
           '<rect width="%d" height="%d" rx="10" ry="10" fill="%s"/>' % (width, height, BG),
           '<path d="M0 10 a10 10 0 0 1 10 -10 h%d a10 10 0 0 1 10 10 v18 h-%d z" fill="%s"/>' % (
               width - 20, width, BAR)]
    for k, c in enumerate(("#f38ba8", "#f9e2af", "#a6e3a1")):
        out.append('<circle cx="%d" cy="14" r="6" fill="%s"/>' % (18 + k * 20, c))
    out.append('<text x="%d" y="18.5" fill="%s" font-family="%s" font-size="12" text-anchor="middle">%s</text>' % (
        width // 2, DIM, esc(FONT), esc(title if len(title) <= width_cols - 16 else title[:width_cols - 19] + "...")))
    # spaces matter: xml:space for SVG 1.1 renderers, white-space for browsers
    out.append('<g font-family="%s" font-size="%d" fill="%s" xml:space="preserve" style="white-space:pre">' % (
        esc(FONT), FONT_PX, FG))
    for n, row in enumerate(rows):
        if not row:
            continue
        y = top + PAD + n * LINE_H
        parts = []
        for t, (color, bold) in row:
            attrs = ""
            if color and color != FG:
                attrs += ' fill="%s"' % color
            if bold:
                attrs += ' font-weight="bold"'
            parts.append('<tspan%s>%s</tspan>' % (attrs, esc(t)) if attrs else esc(t))
        out.append('<text x="%d" y="%d" xml:space="preserve">%s</text>' % (PAD, y, "".join(parts)))
    out.append("</g></svg>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- the demo

def sh(args, cwd=None, env=None, check=True, timeout=120, input=None):
    p = subprocess.run(args, cwd=cwd, env=env, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError("%s: exit %s\n%s%s" % (" ".join(args), p.returncode, p.stdout[-2000:], p.stderr[-2000:]))
    return p


EXEC = {"model": "sonnet", "effort": "medium", "max_turns": 40, "budget_usd": 2, "timeout_min": 20}


def spec_ops():
    """The notes app: two features, three requirements, five tasks in three waves."""
    def req(rid, title, statement, scenario, verify, outputs=(), needs=(), inputs=()):
        fields = {"title": title, "status": "approved"}
        if outputs:
            fields["outputs"] = list(outputs)
        if needs:
            fields["needs"] = list(needs)
        if inputs:
            fields["inputs"] = list(inputs)
        return {"op": "create", "id": rid, "fields": fields, "sections": {
            "Statement": statement, "Scenarios": [scenario], "Verify": verify}}

    def task(tid, title, paths, outputs, context, prompt, needs=(), inputs=(), size="S", cls="B"):
        fields = {"title": title, "status": "planned", "paths": list(paths), "class": cls, "size": size,
                  "exec": EXEC, "outputs": list(outputs)}
        if needs:
            fields["needs"] = list(needs)
        if inputs:
            fields["inputs"] = list(inputs)
        return {"op": "create", "id": tid, "fields": fields, "sections": {
            "Context": context, "Acceptance": ["test -f %s" % paths[0]], "Prompt": prompt}}

    queue_api = {"id": "notes.sync.queue", "kind": "interface",
                 "contract": "push(edit); drain() -> list[Edit] in the order they were made"}
    index_api = {"id": "notes.search.index", "kind": "interface",
                 "contract": "add(note); remove(id); lookup(term) -> set[NoteId]"}
    return [
        {"op": "create", "id": "notes", "fields": {"title": "Notes", "status": "active"},
         "sections": {"Intent": "People write notes on any device and find them again in seconds."}},
        {"op": "create", "id": "notes/sync", "fields": {"title": "Sync", "status": "active"},
         "sections": {"Intent": "Edits made offline reach every device without loss."}},
        {"op": "create", "id": "notes/search", "fields": {"title": "Search", "status": "active"},
         "sections": {"Intent": "A query finds the notes that contain its words."}},
        req("notes/sync/queue", "Offline edit queue",
            "While the device is offline, the notes app shall queue each edit and replay the queue in order when"
            " the connection returns.",
            {"name": "Edits made offline are replayed", "steps": [
                ["Given", "a device with no connection"], ["When", "the user edits two notes"],
                ["And", "the connection returns"], ["Then", "both edits reach the server in the order made"]]},
            ["test -f src/sync/queue.py && test -f src/sync/replay.py"], outputs=[queue_api]),
        req("notes/sync/merge", "Conflicting edits",
            "When two devices change the same note, the notes app shall keep both versions and mark the note as"
            " conflicted.",
            {"name": "Two devices edit one note", "steps": [
                ["Given", "a note open on two devices"], ["When", "both change its first line offline"],
                ["Then", "the note keeps both versions"], ["And", "it is marked conflicted"]]},
            ["test -f src/sync/merge.py"], needs=["notes/sync/queue"],
            inputs=[{"id": "notes.sync.queue", "kind": "interface", "from": "notes/sync/queue"}]),
        req("notes/search/words", "Full-text search",
            "When the user types a query, the notes app shall list the notes that contain every word of it.",
            {"name": "A two-word query", "steps": [
                ["Given", "notes 'buy milk' and 'milk prices'"], ["When", "the user searches for 'milk prices'"],
                ["Then", "only 'milk prices' is listed"]]},
            ["test -f src/search/index.py && test -f src/search/query.py"], outputs=[index_api]),
        # written first, tasks planned later: check warns that nothing produces its output yet
        dict(req("notes/search/ranking", "Rank by recency",
                 "When several notes match a query, the notes app shall list the most recently edited first.",
                 {"name": "Two matching notes", "steps": [
                     ["Given", "two notes that match 'milk'"], ["When", "the user searches for 'milk'"],
                     ["Then", "the note edited last is listed first"]]},
                 ["test -f src/search/rank.py"],
                 outputs=[{"id": "notes.search.rank", "kind": "interface",
                           "contract": "rank(notes) -> list[Note], newest edit first"}]),
             fields={"title": "Rank by recency", "status": "draft",
                     "outputs": [{"id": "notes.search.rank", "kind": "interface",
                                  "contract": "rank(notes) -> list[Note], newest edit first"}]}),
        task("notes/sync/queue/store", "Persist the edit queue", ["src/sync/queue.py"],
             [queue_api, {"id": "src/sync/queue.py", "kind": "file"}],
             "Edits made offline are lost when the app restarts.",
             "Add a durable edit queue in src/sync/queue.py with push and drain.", size="S"),
        task("notes/sync/queue/replay", "Replay the queue on reconnect", ["src/sync/replay.py"],
             [{"id": "src/sync/replay.py", "kind": "file"}], "Nothing drains the queue yet.",
             "Drain the queue in order when the connection returns.",
             needs=["notes/sync/queue/store"],
             inputs=[{"id": "notes.sync.queue", "kind": "interface",
                      "from": "notes/sync/queue/store"}]),
        task("notes/sync/merge/keep-both", "Keep both versions on conflict", ["src/sync/merge.py"],
             [{"id": "src/sync/merge.py", "kind": "file"}], "The last write wins today.",
             "Keep both versions of a note changed on two devices and mark it conflicted.", size="M", cls="A"),
        task("notes/search/words/index", "Inverted index", ["src/search/index.py"],
             [index_api, {"id": "src/search/index.py", "kind": "file"}], "There is no search index.",
             "Build an inverted index of note words in src/search/index.py.", size="M"),
        task("notes/search/words/tokens", "Split notes into words", ["src/search/tokenize.py"],
             [{"id": "src/search/tokenize.py", "kind": "file"}], "Words with accents and hyphens are not found.",
             "Split note text into lowercase words, keeping hyphenated words whole.", size="XS", cls="C"),
        task("notes/search/words/query", "Query the index", ["src/search/query.py"],
             [{"id": "src/search/query.py", "kind": "file"}], "The search box does nothing.",
             "Answer a query with the notes that hold every word, through the index interface.",
             needs=["notes/search/words/index"],
             inputs=[{"id": "notes.search.index", "kind": "interface", "from": "notes/search/words/index"}]),
    ]


QUEUE = "notes.sync.queue.store"
INDEX = "notes.search.words.index"
REPLAY = "notes.sync.queue.replay"
TOKENIZE = "notes.search.words.tokens"


class Demo(object):
    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wc-docs-"))
        self.repo = os.path.join(self.tmp, "repo")
        self.home = os.path.join(self.tmp, "home")
        self.wchome = os.path.join(self.home, ".wavecoord")
        self.server = None
        self.url = None
        self.written = []
        self.modes_path = os.path.join(self.tmp, "modes.json")
        self.gate = os.path.join(self.tmp, "release-replay")
        env = {k: v for k, v in os.environ.items() if k not in PARENT_ENV and not k.startswith(("FAKE_", "GIT_"))}
        gitcfg = os.path.join(self.tmp, "gitconfig")
        with open(gitcfg, "w") as f:
            f.write("[user]\n\tname = demo\n\temail = demo@example.invalid\n[init]\n\tdefaultBranch = main\n"
                    "[commit]\n\tgpgsign = false\n[advice]\n\tdetachedHead = false\n")
        env.update({
            "HOME": self.home, "WAVECOORD_HOME": self.wchome, "PYTHONPATH": SRC,
            "PATH": os.pathsep.join([FAKE_CLAUDE_DIR, FAKE_GH_DIR, os.environ.get("PATH", "")]),
            "GIT_CONFIG_GLOBAL": gitcfg, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_DATE": GIT_DATE, "GIT_COMMITTER_DATE": GIT_DATE,
            "WAVECOORD_TOKENS_FILE": os.path.join(self.wchome, "tokens.json"),
            "WAVECOORD_TOKEN_FILE": os.path.join(self.wchome, "token"),
            "WAVECOORD_PLAN_FETCH_S": "0", "WAVECOORD_HB_INTERVAL_S": "0.5",
            "WAVECOORD_POLL_S": "0.2", "WAVECOORD_NOTIFY_MIN_INTERVAL_S": "0", "WAVECOORD_PROGRESS_S": "3600",
            "FAKE_GH_STATE": os.path.join(self.tmp, "gh.json"), "FAKE_CLAUDE_MODES": self.modes_path,
            "FAKE_CLAUDE_VERSION": "2.1.0 (Claude Code)", "LC_ALL": "C", "COLUMNS": "100",
        })
        self.env = env

    # -- helpers
    def wc(self, *args, check=True, timeout=180, input=None):
        return sh([sys.executable, "-m", "wavecoord"] + list(args), cwd=self.repo, env=self.env, check=check,
                  timeout=timeout, input=input)

    def git(self, *args, cwd=None):
        return sh(["git"] + list(args), cwd=cwd or self.repo, env=self.env).stdout

    def modes(self, m):
        with open(self.modes_path, "w") as f:
            json.dump(m, f)

    def mask(self, text):
        """Temp paths as ~/demo, the port as 18741, and wall-clock times pinned (in their order of appearance)."""
        text = text.replace(self.repo, DEMO).replace(self.home, "~").replace(self.tmp, DEMO)
        text = re.sub(r"127\.0\.0\.1:%d\b" % self.port, "127.0.0.1:%d" % SHOWN_PORT, text)
        for i, ts in enumerate(sorted(set(TS_RE.findall(text)))):
            text = text.replace(ts, "2026-10-19T09:%02d:%02dZ" % (30 + (i * 4) // 60, (i * 4) % 60))
        return text

    def capture(self, name, title, cmds, check=True, cols=100):
        """Run each wavecoord command (a list of args), mask, render, write <name>.svg. Returns the outputs."""
        blocks, outs = [], []
        for args in cmds:
            # as a terminal shows it: stdout and stderr interleaved
            p = subprocess.run([sys.executable, "-m", "wavecoord"] + list(args), cwd=self.repo, env=self.env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                               timeout=180)
            if check and p.returncode not in (0, 1):
                raise RuntimeError("wavecoord %s: exit %s\n%s" % (" ".join(args), p.returncode, p.stdout))
            text = p.stdout
            if args[0] == "hosts":  # this machine's memory and disk
                text = re.sub(r"^(\S+\s+\d+/\d+\s+)\S+(\s+)\S+", lambda m: "%s%-4s%s%s" % (
                    m.group(1), "32.0", m.group(2), "512"), text, flags=re.M)
            outs.append(p)
            blocks.append(("wavecoord " + " ".join(_quote(a) for a in args), self.mask(text)))
        svg = render_svg(title or blocks[0][0], blocks, cols=cols)
        path = os.path.join(self.out_dir, name + ".svg")
        with open(path, "w") as f:
            f.write(svg)
        self.written.append(path)
        sys.stdout.write("== %s\n" % name)
        for c, t in blocks:
            sys.stdout.write("$ %s\n%s\n" % (c, t))
        return outs

    def wait(self, pred, what, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(0.3)
        raise RuntimeError("timed out waiting for " + what)

    def attempt_state(self, aid):
        p = self.wc("attempts", "result", aid, "--json", check=False)
        try:
            return json.loads(p.stdout).get("state")
        except ValueError:
            return None

    def ended(self, aid):
        return self.attempt_state(aid) in ("reported", "failed", "timed_out", "stopped", "lost")

    # -- setup
    def setup(self):
        os.makedirs(os.path.join(self.home, ".claude"))
        with open(os.path.join(self.home, ".claude", ".credentials.json"), "w") as f:
            f.write("{}\n")  # the host probe checks only that claude has a login
        os.makedirs(self.wchome)
        origin = os.path.join(self.tmp, "origin.git")
        self.git("init", "-q", "--bare", "-b", "main", origin, cwd=self.tmp)
        self.git("init", "-q", "-b", "main", self.repo, cwd=self.tmp)
        cfg = {"project": "notes", "wip_cap": 4, "auto_resolve": False,
               "hosts": [{"name": "local", "slots": 3, "repo_path": "."}],
               "spec": {"status_flush_s": 0}, "fence": {"merge": "enforce"}}
        os.makedirs(os.path.join(self.repo, ".claude"))
        with open(os.path.join(self.repo, ".claude", "wavecoord.json"), "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        with open(os.path.join(self.repo, "README.md"), "w") as f:
            f.write("# notes\n\nA small notes app.\n")
        self.wc("spec", "apply", "-", input=json.dumps({"ops": spec_ops()}))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "notes: spec tree")
        self.git("remote", "add", "origin", origin)
        self.git("push", "-q", "-u", "origin", "main")
        self.wc("tokens", "provision", "--role", "lead", "--out", os.path.join(self.wchome, "token"))
        self.wc("tokens", "provision", "--role", "agent", "--host", "local", "--out",
                os.path.join(self.wchome, "hosts", "local.token"))
        # the server logs to a file: a pipe nobody reads would fill up and stall it
        log = os.path.join(self.tmp, "serve.log")
        with open(log, "w") as f:
            self.server = subprocess.Popen([sys.executable, "-m", "wavecoord", "serve", "--repo", self.repo,
                                            "--home", os.path.join(self.tmp, "server"), "--port", "0"],
                                           cwd=self.repo, env=self.env, stdout=f, stderr=subprocess.STDOUT)

        def started():
            with open(log) as f:
                first = f.readline()
            if self.server.poll() is not None:
                raise RuntimeError("the server did not start:\n" + open(log).read()[-1000:])
            return first if first.startswith("wavecoord serve: http") and first.endswith("\n") else None
        line = self.wait(started, "the server", timeout=30)
        self.url = line.split()[2]
        self.port = int(self.url.rsplit(":", 1)[1].split("/")[0])
        self.env["WAVECOORD_URL"] = self.url
        self.env["WAVECOORD_TOKEN"] = open(os.path.join(self.wchome, "token")).read().strip()

    def stop(self):
        """Stop the detached wrappers of attempts that still run, then the server."""
        if os.path.isdir(self.repo):
            try:
                rows = json.loads(self.wc("attempts", "ls", "--active", "--json", check=False).stdout or "[]")
                for r in rows:
                    self.wc("attempts", "stop", r["id"], check=False, timeout=30)
            except (ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
                pass
        if self.server and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(20)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait()

    # -- the story
    def pr_number(self, task):
        with open(self.env["FAKE_GH_STATE"]) as f:
            prs = json.load(f)["prs"]
        return str([p["number"] for p in prs if p["head"] == "wave/%s-1" % task][-1])

    def main_status(self, rel):
        self.git("fetch", "-q", "origin")
        p = sh(["git", "show", "origin/main:specs/" + rel], cwd=self.repo, env=self.env, check=False)
        m = re.search(r"^status: (\S+)", p.stdout, re.M)
        return m.group(1) if m else None

    def run(self):
        self.setup()
        self.capture("status", "wavecoord status && wavecoord hosts", [["status"], ["hosts"]], cols=120)
        self.capture("spec-check", None, [["spec", "check"]])
        self.capture("spec-waves", None, [["spec", "waves"]])
        self.capture("plan-ready", None, [["spec", "ready"]])
        # wave 1: store commits inside its paths; index waits for the lead's hint and also changes README.md,
        # outside its lease; tokens reports blocked.
        self.modes({
            QUEUE: {"commit": {"src/sync/queue.py": "class Queue: pass\n"}, "cost": 0.38,
                    "report": {"summary": "Durable edit queue with push and drain"}},
            INDEX: {"wait_steer": 1, "commit": {"src/search/index.py": "INDEX = {}\n",
                                                "README.md": "# notes\n\nNow with search.\n"}, "cost": 0.61,
                    "report": {"summary": "Inverted index of note words"}},
            TOKENIZE: {"commit": {"src/search/tokenize.py": "def words(text): return text.lower().split()\n"},
                       "cost": 0.12, "report": {"status": "blocked",
                                                "summary": "Hyphenated words need a decision: one word or two?"}},
            REPLAY: {"wait_for": self.gate, "commit": {"src/sync/replay.py": "def replay(q): pass\n"}},
        })
        self.capture("dispatch", None, [["dispatch"]], cols=110)
        self.wait(lambda: self.ended(QUEUE + "-1") and self.ended(TOKENIZE + "-1"), "wave 1 reports")
        self.capture("attempts", None, [["attempts", "ls"]])
        self.capture("steer-hint", "wavecoord steer && wavecoord hint",
                     [["steer", INDEX, "model", "opus"],
                      ["hint", INDEX, "Export the index through notes.search.index only."]])
        self.wait(lambda: self.ended(INDEX + "-1"), "the index attempt")
        self.capture("refinery", "wavecoord collect && wavecoord refinery merge", [["collect"], ["refinery", "merge"]])
        self.git("fetch", "-q", "origin")
        self.capture("gate-check", None, [["gate-check", self.pr_number(INDEX)]])
        # wave 2 starts: replay runs (it waits) while the tree shows the progress so far
        self.wc("dispatch")
        self.wait(lambda: self.attempt_state(REPLAY + "-1") == "running", "the replay attempt")
        self.wait(lambda: self.main_status("notes/sync/queue/tasks/replay.md") == "in_progress" and
                  self.main_status("notes/sync/queue/tasks/store.md") == "done", "the status commits")
        self.git("pull", "-q", "--ff-only", "origin", "main")
        self.capture("spec-tree", None, [["spec", "tree"]])
        self.capture("events-lead", None, [["events", "--for", "lead", "--no-cursor", "--since", "0"]])
        with open(self.gate, "w") as f:
            f.write("go\n")
        self.wait(lambda: self.ended(REPLAY + "-1"), "the replay attempt")


def _quote(a):
    return a if re.match(r"^[A-Za-z0-9_@%+=:,./-]+$", a) else "'" + a.replace("'", "'\\''") + "'"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "img"), help="output directory (default docs/img)")
    ap.add_argument("--keep", action="store_true", help="keep the demo directory")
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    d = Demo(os.path.abspath(args.out))
    try:
        d.run()
    finally:
        d.stop()
        if args.keep:
            sys.stdout.write("demo kept: %s\n" % d.tmp)
        else:
            shutil.rmtree(d.tmp, ignore_errors=True)
    sys.stdout.write("wrote %d SVGs to %s\n" % (len(d.written), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
