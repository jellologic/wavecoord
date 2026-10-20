"""`wavecoord spec import`: move another spec-driven setup into the spec tree (docs/import.md).

    ops, report = build(fmt, src, root, cap, granularity="criterion")

It reads the files of GitHub Spec Kit (`specs/<NNN>-<feature>/`), Amazon Kiro
(`.kiro/specs/<feature>/`, `.kiro/steering/`) or OpenSpec (`openspec/specs/`,
`openspec/changes/`) and returns spec-tree ops (`create` only, all under the one
capability `cap`) for apply_ops, and a report of what a person has to look at:
statements that are not EARS, tasks without file paths, sections it did not
know. It never writes or deletes a source file. Every record starts as draft
(a decision as proposed); every record names its source file. The result is a
pure function of the source files: the same files give the same ops.
"""

import os
import re

from wavecoord.globs import globs_overlap

from . import doc
from .check import canonical_glob, ears_class
from .model import RESERVED, STEP_WORDS

FORMATS = ("speckit", "kiro", "openspec")
MAX_COORD_ID = 64  # a task id with "." for "/" (check.py)
CAP_MAX = 20
REQ_MAX = 16
TASK_MAX = 8
FEAT_MAX = 30
# The "S" starting point of plugin/skills/wave-plan/exec-plan.md: /wavecoord:wave-plan sizes each task.
EXEC = {"model": "sonnet", "effort": "medium", "max_turns": 50, "budget_usd": 2, "timeout_min": 40}
EXTS = {"c", "cc", "cfg", "cjs", "conf", "cpp", "cs", "css", "dart", "env", "ex", "exs", "go", "gradle", "graphql",
        "h", "hpp", "html", "ini", "java", "js", "json", "jsx", "kt", "lock", "md", "mjs", "php", "proto", "py",
        "rb", "rs", "scss", "sh", "sql", "svelte", "swift", "toml", "ts", "tsx", "txt", "vue", "xml", "yaml", "yml"}

COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
CHECKBOX_RE = re.compile(r"^(\s*)[-*]\s+\[([ xX-])\](\*)?\s*(.*)$")
DETAIL_RE = re.compile(r"^(\s+)[-*]\s+(.*)$")
SHALL_RE = re.compile(r"\b(SHALL|MUST)(\s+NOT)?\b", re.I)


class ImportFailed(ValueError):
    pass


# ------------------------------------------------------------------ text helpers

def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read().replace("\r\n", "\n").replace("\r", "\n")


def strip_frontmatter(text):
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            return text[end + 5:]
    return text


def clean(text):
    return COMMENT_RE.sub("", strip_frontmatter(text))


def blocks(lines, level):
    """Split lines at headings of exactly `level` (outside code fences) -> (lines before, [(title, lines)])."""
    pre, out, cur, fstate = [], [], None, None
    for line in lines:
        fstate, in_fence = doc.fence(fstate, line)
        m = None if in_fence else HEADING_RE.match(line)
        if m and len(m.group(1)) == level:
            cur = (m.group(2).strip(), [])
            out.append(cur)
            continue
        (cur[1] if cur else pre).append(line)
    return pre, out


def heading_key(title):
    """`User Scenarios & Testing *(mandatory)*` -> `user scenarios & testing`."""
    t = re.sub(r"\*\(.*?\)\*|\(.*?\)", "", title)
    return re.sub(r"[*_`]", "", t).strip().lower()


def section(title, body):
    """A source section to carry into a prose section: its heading (without `*(mandatory)*` marks) and lines."""
    return ["## " + re.sub(r"\s*\*\(.*?\)\*", "", title).strip()] + list(body)


def demote(lines, n):
    out, fstate = [], None
    for line in lines:
        fstate, in_fence = doc.fence(fstate, line)
        m = None if in_fence else HEADING_RE.match(line)
        out.append("#" * min(6, len(m.group(1)) + n) + " " + m.group(2) if m else line)
    return out


def included(b, path):
    """A whole source file under a `From <file>` heading, its own headings one level below that one."""
    return ["", "## From %s" % b.rel(path)] + demote(clean(read(path)).split("\n"), 2)


def prose(lines, shift=2):
    """Source Markdown as a prose section: headings two levels down (a `## ` line would start a section),
    trailing spaces and blank runs removed."""
    if isinstance(lines, str):
        lines = lines.split("\n")
    out, fstate = [], None
    for line in lines:
        line = line.rstrip().replace("\t", "    ")
        fstate, in_fence = doc.fence(fstate, line)
        m = None if in_fence else HEADING_RE.match(line)
        if m:
            line = "#" * min(6, len(m.group(1)) + shift) + " " + m.group(2)
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def paragraphs(lines):
    """Text paragraphs (no headings, lists or fences) of some lines, each joined to one line."""
    out, cur, fstate = [], [], None
    for line in lines:
        fstate, in_fence = doc.fence(fstate, line)
        s = line.strip()
        if in_fence or not s or HEADING_RE.match(s) or s.startswith(("- ", "* ", "|", ">")):
            if cur:
                out.append(" ".join(cur))
                cur = []
            continue
        cur.append(s)
    if cur:
        out.append(" ".join(cur))
    return out


def one_line(text, limit=100):
    t = re.sub(r"\s+", " ", re.sub(r"\*\*", "", text or "")).strip()
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(" ", 1)[0]
    return (cut or t[:limit]).rstrip(" ,;:.") + "..."


def items(lines):
    """`- item` lines -> one-line items (nested lines join their item)."""
    out = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^(?:[-*]|\d+[.)])\s+(.*)$", s)
        if m and not line[:1].isspace():
            out.append(m.group(1).strip())
        elif out:
            out[-1] += " " + s
        else:
            out.append(s)
    return [one_line(x, 300) for x in out if x]


def slug(text, limit, fallback):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:limit].strip("-")
    if not s:
        s = fallback
    if s in RESERVED:
        s = (s + "-x")[:limit]
    return s


def find_paths(text):
    """Repo paths named in a task's text: `a/b.py`, src/x/, docs/y.md; a backquoted token counts with a /
    alone. Never a URL, an absolute path or one with `..`."""
    out = []
    for quoted, bare in re.findall(r"`([^`\s]+)`|(\S+)", text or ""):
        tok = (quoted or bare).strip("()[]{}<>,;:'\"!?")
        if bare:
            tok = tok.rstrip(".")
        if not tok or "://" in tok or tok.startswith(("/", "~", "-", "$")) or ".." in tok.split("/"):
            continue
        if not re.match(r"^[A-Za-z0-9_.@*/-]+$", tok) or not re.search(r"[A-Za-z]", tok):
            continue
        last = tok.rstrip("/").rsplit("/", 1)[-1]
        ext = last.rsplit(".", 1)[1].lower() if "." in last[1:] else ""
        if tok.endswith("/"):
            tok += "**"
        elif ext in EXTS and len(last) > len(ext) + 1:
            pass
        elif not (quoted and "/" in tok):
            continue
        g = canonical_glob(tok)
        if g and g not in out:
            out.append(g)
    return out


def to_ears(text):
    """A source statement -> (statement, ok). `ok` when it was rewritten (or already is) one EARS sentence
    with `shall` once (check.ears_class); else the statement is the source text, for a person to rewrite."""
    src = one_line(text, 2000).rstrip()
    t = src.rstrip(".")
    if not t or "NEEDS CLARIFICATION" in t.upper():
        return src, False
    m = re.match(r"^(?:the\s+)?users?\s+(?:MUST|SHALL)(\s+NOT)?\s+be\s+able\s+to\s+(.+)$", t, re.I)
    if m:
        out = "The system shall %slet users %s." % ("not " if m.group(1) else "", m.group(2))
        return (out, True) if ears_class(out) else (src, False)
    hits = list(SHALL_RE.finditer(t))
    if len(hits) != 1:
        return src, False
    h = hits[0]
    pre, post = t[:h.start()].strip(" ,"), t[h.end():].strip()
    verb = "shall not" if h.group(2) else "shall"
    if not post:
        return src, False
    kw = re.match(r"^(when|while|if|where)\s+(.+)$", pre, re.I)
    if kw:
        k, rest = kw.group(1).lower(), kw.group(2)
        parts = re.split(r",?\s+then\s+", rest, flags=re.I)
        if len(parts) >= 2:
            cond, subj = " then ".join(parts[:-1]), parts[-1]
        elif "," in rest:
            cond, subj = rest.rsplit(",", 1)
        else:
            mm = list(re.finditer(r"\bthe\s", rest, re.I))
            if not mm or mm[-1].start() == 0:
                return src, False
            cond, subj = rest[:mm[-1].start()], rest[mm[-1].start():]
        cond, subj = _soft(cond.strip(" ,")), _subject(subj.strip(" ,"))
        if not cond or not subj:
            return src, False
        if k == "if":
            out = "If %s, then %s %s %s." % (cond, subj, verb, post)
        elif k == "while" and re.search(r"\swhen\s", cond, re.I):
            a, b = re.split(r",?\s+when\s+", cond, maxsplit=1, flags=re.I)
            out = "While %s, when %s, %s %s %s." % (a, b, subj, verb, post)
        else:
            out = "%s %s, %s %s %s." % (k.capitalize(), cond, subj, verb, post)
    else:
        subj = _subject(pre)
        if not subj:
            return src, False
        out = "%s %s %s." % (subj[0].upper() + subj[1:], verb, post)
    return (out, True) if ears_class(out) else (src, False)


def _soft(s):
    """Upper-case EARS keywords inside a clause -> lower case (AND, OR, NOT)."""
    return re.sub(r"\b(AND|OR|NOT|THE|A|AN)\b", lambda m: m.group(1).lower(), s)


def _subject(s):
    s = s.strip()
    if s.upper() == s:
        s = s.lower()
    if s.lower() in ("system", "the system"):
        return "the system"
    if re.match(r"^(The|A|An)\s", s):
        s = s[0].lower() + s[1:]
    return s


def steps_of(text):
    """`**Given** x, **When** y, **Then** z` (Spec Kit) -> [[Given, x], [When, y], [Then, z]], or None."""
    t = text.strip()
    words = "|".join(STEP_WORDS)
    marks = list(re.finditer(r"\*\*(%s)\*\*" % words, t)) or list(re.finditer(r"(?:^|,\s*|\s)(%s)\b" % words, t))
    if not marks or t[:marks[0].start()].strip(" ,"):
        return None
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(t)
        body = re.sub(r"\*\*", "", t[m.end():end]).strip(" ,;.")
        if not body:
            return None
        out.append([m.group(1), one_line(body, 400)])
    return out


def bullet_step(line):
    """`- **WHEN** x` or `- GIVEN x` (OpenSpec) -> [When, x], or None."""
    m = re.match(r"^\s*[-*]\s+(?:\*\*)?([A-Za-z]+)(?:\*\*)?:?\s+(.+)$", line)
    if not m or m.group(1).capitalize() not in STEP_WORDS:
        return None
    return [m.group(1).capitalize(), one_line(m.group(2), 400)]


# ------------------------------------------------------------------ the build

class Build(object):
    def __init__(self, fmt, cap, root):
        self.fmt, self.cap, self.root = fmt, cap, root
        self.nodes = {}  # id -> {"kind", "fields", "sections"}
        self.order = []
        self.names = {}  # parent id -> names taken
        self.flags = []
        self.info = []
        self.items = []  # task items, all files, in order
        self.feat_max = min(FEAT_MAX, MAX_COORD_ID - 3 - len(cap) - REQ_MAX - TASK_MAX)

    def rel(self, path):
        return os.path.relpath(path, self.root).replace(os.sep, "/")

    def name(self, parent, text, limit, fallback):
        taken = self.names.setdefault(parent, set())
        base = slug(text, limit, fallback)
        name, n = base, 2
        while name in taken:
            sfx = "-%d" % n
            name = base[:limit - len(sfx)].rstrip("-") + sfx
            n += 1
        taken.add(name)
        return name

    def add(self, nid, kind, fields, sections):
        self.nodes[nid] = {"kind": kind, "fields": dict(fields), "sections": dict(sections)}
        self.order.append(nid)
        return nid

    def rename(self, old, new):
        self.nodes[new] = self.nodes.pop(old)
        self.order[self.order.index(old)] = new

    def flag(self, what, nid, src, msg):
        self.flags.append({"kind": what, "id": nid, "source": src, "msg": msg})

    def note(self, nid, line):
        sec = self.nodes[nid]["sections"]
        sec["Notes"] = (sec.get("Notes", "") + "\n" + line).strip("\n")

    def requirement(self, feat, name_text, fallback, title, statement_src, src, sections=None, fields=None,
                    what="statement"):
        """A requirement with its statement in EARS when the rewrite is mechanical; else as written, flagged."""
        nid = "%s/%s" % (feat, self.name(feat, name_text, REQ_MAX, fallback))
        st, ok = to_ears(statement_src)
        secs = {"Statement": st}
        secs.update(sections or {})
        t = one_line(title, 100) or nid.rsplit("/", 1)[1]
        f = {"title": t[:1].upper() + t[1:]}
        f.update(fields or {})
        self.add(nid, "requirement", f, secs)
        self.note(nid, "Imported from %s." % src)
        if not ok:
            self.flag("non-ears", nid, src, "%s kept as written: rewrite it in EARS form (%s)" % (
                what, one_line(st, 80)))
        return nid

    def ops(self):
        out = []
        for nid in self.order:
            n = self.nodes[nid]
            fields = {k: v for k, v in n["fields"].items() if v not in (None, [], "")}
            secs = {k: v for k, v in n["sections"].items() if v not in (None, [], "")}
            op = {"op": "create", "id": nid, "fields": fields, "sections": secs}
            out.append(op)
        return out

    def report(self):
        kinds = {}
        for nid in self.order:
            kinds[self.nodes[nid]["kind"]] = kinds.get(self.nodes[nid]["kind"], 0) + 1
        verify = [{"kind": "no-verify", "id": nid, "source": None, "msg": "no Verify in the source: add a Verify"
                   " command"} for nid in self.order if self.nodes[nid]["kind"] == "requirement"
                  and not self.nodes[nid]["sections"].get("Verify")
                  and self.nodes[nid]["fields"].get("status") != "deprecated"]
        return {"from": self.fmt, "into": self.cap, "created": kinds, "flags": self.flags, "verify": verify,
                "info": self.info}


# ------------------------------------------------------------------ tasks

class Item(object):
    """One task line of a source tasks.md."""

    def __init__(self, key, text, src, done=False):
        self.key, self.text, self.src, self.done = key, text, src, done
        self.details = []
        self.parallel = False
        self.optional = False
        self.req = None
        self.stage = None
        self.deps = []  # keys of items it waits for
        self.tags = []
        self.context = ""
        self.notes = []
        self.id = None

    def paths(self):
        return find_paths(" ".join([self.text] + self.details))


def place_tasks(b, items):
    """Create a task per item that has paths and is not done; the others go to their requirement's Notes, and
    what waits for them waits for what they waited for. Then tasks that may run together and share a path get
    conflicts_with, so predicate P holds."""
    by_key = {(it.src, it.key): it for it in items}
    created = {}
    for it in items:
        paths = it.paths()
        if it.done or not paths or it.req is None:
            why = "checked done in the source" if it.done else "no file path in the source"
            if it.req is not None:
                b.note(it.req, "- %s %s (%s)" % (it.key, one_line(it.text, 120), why))
            if not it.done:
                b.flag("no-paths", it.req, it.src, "task %s has no file path: kept in the Notes of %s for"
                       " /wavecoord:wave-plan" % (it.key, it.req))
            else:
                b.flag("done", it.req, it.src, "task %s is checked done: kept in the Notes, not imported" % it.key)
            continue
        key = re.sub(r"[^a-z0-9]+", "-", it.key.lower())
        name = b.name(it.req, key if key[:1].isalpha() else "t" + key, TASK_MAX,
                      "t%d" % (len(created) + 1))
        it.id = "%s/%s" % (it.req, name)
        created[(it.src, it.key)] = it
        fields = {"title": one_line(it.text, 100) or it.key, "paths": sorted(paths), "class": "B", "size": "S",
                  "exec": dict(EXEC), "tags": sorted(set(it.tags))}
        context = "Imported from %s (task %s)." % (it.src, it.key)
        if it.context:
            context += "\n" + it.context
        prompt = one_line(it.text, 1000)
        if it.details:
            prompt += "\n\n" + "\n".join("- %s" % one_line(d, 300) for d in it.details)
        secs = {"Context": context, "Prompt": prompt}
        if it.notes:
            secs["Notes"] = "\n".join(it.notes)
        b.add(it.id, "task", fields, secs)
    memo = {}

    def resolve(k, seen=()):
        if k in created:
            return {created[k].id}
        if k in memo:
            return memo[k]
        if k in seen or k not in by_key:
            return set()
        out = set()
        for d in by_key[k].deps:
            out |= resolve(d, seen + (k,))
        memo[k] = out
        return out

    for k, it in created.items():
        needs = set()
        for d in it.deps:
            needs |= resolve(d)
        needs.discard(it.id)
        if needs:
            b.nodes[it.id]["fields"]["needs"] = sorted(needs)
    _conflicts(b, [it.id for it in created.values()])


def _conflicts(b, tasks):
    def needs(nid):
        return b.nodes[nid]["fields"].get("needs") or []

    def tasks_under(nid):
        return [t for t in tasks if t == nid or t.startswith(nid + "/")]

    direct = {}
    for t in tasks:
        deps = set(x for x in needs(t) if x in b.nodes and b.nodes[x]["kind"] == "task")
        parts = t.split("/")
        for i in range(1, len(parts)):
            for n in needs("/".join(parts[:i])):
                deps.update(tasks_under(n))
        for n in needs(t):
            if n in b.nodes and b.nodes[n]["kind"] != "task":
                deps.update(tasks_under(n))
        direct[t] = deps
    reach = {}
    for t in tasks:
        seen, stack = set(), list(direct[t])
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            stack.extend(direct.get(x, ()))
        reach[t] = seen
    for i, a in enumerate(tasks):
        for x in tasks[:i]:
            if x in reach[a] or a in reach[x]:
                continue
            pa, px = b.nodes[a]["fields"]["paths"], b.nodes[x]["fields"]["paths"]
            if any(globs_overlap(p, q) for p in pa for q in px):
                cw = b.nodes[a]["fields"].setdefault("conflicts_with", [])
                cw.append(x)
                b.flag("conflict", a, None, "%s and %s may run together and write the same files: they get"
                       " conflicts_with (add a needs if the order matters)" % (a, x))


def checkbox_items(text, src):
    """Checkbox lines of a tasks.md -> [(indent, Item, numbered key)], with their detail lines; and the
    `## ` / `### ` headings they sit under (item.stage = (h2 index, h3 index), item.context = the h2 title)."""
    out, cur, h2, h3, h2title, fstate = [], None, 0, 0, "", None
    n = 0
    for line in clean(text).split("\n"):
        fstate, in_fence = doc.fence(fstate, line)
        if in_fence:
            continue
        m = HEADING_RE.match(line)
        if m:
            if len(m.group(1)) <= 2:
                h2, h3, h2title = h2 + 1, 0, m.group(2).strip()
            else:
                h3 += 1
            cur = None
            continue
        m = CHECKBOX_RE.match(line)
        if m:
            n += 1
            body = m.group(4).strip()
            km = re.match(r"^(\d+(?:\.\d+)*)\.?\s+(.*)$", body) or re.match(r"^(T\d+)\s+(.*)$", body)
            key, body = (km.group(1), km.group(2)) if km else (str(n), body)
            it = Item(key, body, src, done=m.group(2) in "xX")
            it.optional = bool(m.group(3))
            it.stage = (h2, h3)
            it.context = h2title
            cur = (len(m.group(1).replace("\t", "    ")), it)
            out.append(cur)
            continue
        m = DETAIL_RE.match(line)
        if m and cur is not None and len(m.group(1)) > cur[0]:
            cur[1].details.append(m.group(2).strip())
            continue
        if line.strip() and not line[:1].isspace():
            cur = None
    return out


REQ_REF_RE = re.compile(r"^_?\s*Requirements?\s*:\s*(.*?)_?$", re.I)


# ------------------------------------------------------------------ GitHub Spec Kit

SPECKIT_KNOWN = {"user scenarios & testing", "requirements", "success criteria", "assumptions", "clarifications"}


def speckit_dirs(src):
    """The feature folders of a Spec Kit project: <src>/specs/*/spec.md, or <src>/*/spec.md."""
    for base in (os.path.join(src, "specs"), src):
        if os.path.isdir(base):
            dirs = sorted(d for d in os.listdir(base) if os.path.isfile(os.path.join(base, d, "spec.md")))
            if dirs:
                return base, [os.path.join(base, d) for d in dirs]
    return None, []


def import_speckit(b, src):
    base, dirs = speckit_dirs(src)
    if not dirs:
        raise ImportFailed("no Spec Kit features under %s (specs/<NNN>-<feature>/spec.md)" % b.rel(src) or ".")
    cap = b.cap
    const = None
    for p in (os.path.join(src, ".specify", "memory", "constitution.md"), os.path.join(src, "memory",
                                                                                       "constitution.md"),
              os.path.join(b.root, ".specify", "memory", "constitution.md")):
        if os.path.isfile(p):
            const = p
            break
    cap_secs = {"Intent": "Features imported from GitHub Spec Kit (%s)." % b.rel(base)}
    b.add(cap, "capability", {"title": "Imported from Spec Kit"}, cap_secs)
    if const:
        _constitution(b, const)
    for d in dirs:
        _speckit_feature(b, d)
    place_tasks(b, b.items)


def _constitution(b, path):
    text = clean(read(path))
    lines = text.split("\n")
    pre, h2 = blocks(lines, 2)
    title = next((m.group(2) for m in map(HEADING_RE.match, pre) if m), "Constitution")
    date = re.search(r"\*\*Ratified\*\*:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", text)
    version = re.search(r"\*\*Version\*\*:\s*([0-9][^\s|]*)", text)
    principles = [ln for t, body in h2 if heading_key(t) != "governance" for ln in section(t, body)]
    governance = [ln for t, body in h2 if heading_key(t) == "governance" for ln in body
                  if not re.search(r"\*\*(Version|Ratified)\*\*", ln)]
    src = b.rel(path)
    if not date:
        b.nodes[b.cap]["sections"]["Design"] = "### Constitution (%s)\n%s" % (src, prose(lines))
        b.info.append("%s has no Ratified date: it is in the Design of %s" % (src, b.cap))
        return
    nid = "%s/decisions/%s-constitution" % (b.cap, date.group(1))
    b.add(nid, "decision", {"title": one_line(title, 100), "affects": [b.cap]},
          {"Context": "Imported from %s%s." % (src, " (version %s)" % version.group(1) if version else ""),
           "Decision": prose(principles) or prose(lines),
           "Consequences": prose(governance) or "Every feature under %s follows these principles." % b.cap})


def _speckit_feature(b, d):
    spec = os.path.join(d, "spec.md")
    srel = b.rel(spec)
    text = clean(read(spec))
    lines = text.split("\n")
    pre, h2 = blocks(lines, 2)
    folder = os.path.basename(d)
    title = next((m.group(2) for m in map(HEADING_RE.match, pre) if m), folder)
    title = re.sub(r"^Feature Specification:\s*", "", title).strip()
    feat = "%s/%s" % (b.cap, b.name(b.cap, re.sub(r"^[0-9]+[-_]", "", folder), b.feat_max, "feature"))
    inp = re.search(r"\*\*Input\*\*:\s*(?:User description:\s*)?\"?(.*?)\"?\s*$", "\n".join(pre), re.M)
    design, questions, stories, frs = [], [], [], []
    for t, body in h2:
        key = heading_key(t)
        if key == "user scenarios & testing":
            _, h3 = blocks(body, 3)
            for st, sbody in h3:
                if heading_key(st) == "edge cases":
                    questions += items(sbody)
                elif re.match(r"^User Story\s+\d+", st, re.I):
                    stories.append((st, sbody))
                else:
                    design += section(st, sbody)
        elif key == "requirements":
            fpre, h3 = blocks(body, 3)
            for st, sbody in [("", fpre)] + h3:
                if heading_key(st) == "key entities":
                    design += ["## Key entities"] + sbody
                    continue
                for ln in sbody:
                    m = re.match(r"^\s*[-*]\s+\*{0,2}(FR-[0-9]+)\*{0,2}:?\*{0,2}\s*(.+)$", ln)
                    if m:
                        frs.append((m.group(1), m.group(2).strip()))
                    elif ln.strip() and st and heading_key(st) != "functional requirements":
                        design.append(ln)
        elif key in SPECKIT_KNOWN:
            design += section(t, body)
        else:
            design += section(t, body)
            b.flag("unknown-section", feat, srel, "section %r is not a Spec Kit section: kept in Design" % t)
    for extra in ("plan.md", "research.md"):
        p = os.path.join(d, extra)
        if os.path.isfile(p):
            design += included(b, p)
    others = sorted(os.path.relpath(os.path.join(dp, f), d).replace(os.sep, "/")
                    for dp, _, fs in os.walk(d) for f in fs
                    if f.endswith(".md") and os.path.relpath(os.path.join(dp, f), d) not in
                    ("spec.md", "plan.md", "research.md", "tasks.md"))
    if others:
        design += ["", "Also see: %s." % ", ".join("`%s/%s`" % (b.rel(d), o) for o in others)]
    for fid, ftext in frs:
        if "NEEDS CLARIFICATION" in ftext.upper():
            q = re.search(r"\[NEEDS CLARIFICATION:\s*(.*?)\]", ftext, re.I)
            questions.append("%s: %s" % (fid, q.group(1) if q else ftext))
    intent = (inp.group(1).strip() if inp and inp.group(1).strip() else title)
    b.add(feat, "feature", {"title": one_line(title, 100) or folder},
          {"Intent": "%s\n\nImported from %s." % (intent, srel), "Design": prose(design),
           "Open questions": [one_line(q, 300) for q in questions]})
    story_ids = {}
    for st, sbody in stories:
        m = re.match(r"^User Story\s+(\d+)\s*[-:–—]?\s*(.*?)\s*(?:\(Priority:\s*(P\d+)\))?\s*$", st, re.I)
        num, stitle, prio = m.group(1), m.group(2) or st, m.group(3)
        desc, why, test, scen = [], "", "", []
        for ln in sbody:
            s = ln.strip()
            if s.startswith("**Why this priority**"):
                why = s.split(":", 1)[1].strip() if ":" in s else ""
            elif s.startswith("**Independent Test**"):
                test = s.split(":", 1)[1].strip() if ":" in s else ""
            elif s.startswith("**Acceptance Scenarios**"):
                continue
            elif re.match(r"^\d+\.\s+", s):
                scen.append(re.sub(r"^\d+\.\s+", "", s))
            elif s and s != "---":
                desc.append(s)
        stmt = " ".join(desc) or stitle
        scenarios, bad = [], []
        for i, sc in enumerate(scen):
            steps = steps_of(sc)
            if steps:
                scenarios.append({"name": "Acceptance %d" % (i + 1), "steps": steps})
            else:
                bad.append(sc)
        rid = b.requirement(feat, "us-%s" % num, "us-%s" % num, stitle, stmt, srel,
                            sections={"Scenarios": scenarios, "Rationale": why},
                            fields={"tags": [prio.lower()] if prio else []}, what="user story")
        if test:
            b.note(rid, "Independent test: %s" % test)
        for sc in bad:
            b.note(rid, "- Acceptance scenario (not Given/When/Then): %s" % sc)
            b.flag("scenario", rid, srel, "an acceptance scenario is not Given/When/Then: kept in Notes")
        story_ids[num] = rid
    for fid, ftext in frs:
        m = SHALL_RE.search(ftext)
        title = ftext[m.end():].strip() if m and not m.group(2) else ftext
        title = re.sub(r"^be able to\s+", "", title)
        title = title[:1].upper() + title[1:]
        b.requirement(feat, fid.lower(), fid.lower(), title.rstrip("."), ftext, "%s (%s)" % (srel, fid))
    tasks = os.path.join(d, "tasks.md")
    if os.path.isfile(tasks):
        _speckit_tasks(b, feat, tasks, story_ids)


def _speckit_tasks(b, feat, path, story_ids):
    src = b.rel(path)
    found = checkbox_items(read(path), src)
    phase_story, seen_story = {}, False
    groups = {}  # requirement -> [items], in order
    for _, it in found:
        m = re.match(r"^((?:\[[^\]]*\]\s*)*)(.*)$", it.text)
        marks = re.findall(r"\[([^\]]*)\]", m.group(1))
        it.text = m.group(2).strip()
        it.parallel = "P" in marks
        story = next((x[2:] for x in marks if re.match(r"^US\d+$", x)), None)
        pm = re.search(r"User Story\s+(\d+)", it.context, re.I)
        if pm:
            seen_story = True
            story = story or pm.group(1)
        phase_story[it.stage[0]] = seen_story
        if story and story in story_ids:
            it.req = story_ids[story]
            it.tags = ["us%s" % story]
        else:
            if story:
                b.flag("unlinked", feat, src, "task %s names user story %s, which spec.md does not have" % (
                    it.key, story))
            group = "polish" if seen_story else "setup"
            it.req = _group_req(b, feat, group, src)
        if it.context:
            it.context = "Phase: %s." % one_line(it.context, 120)
        it.deps = ["T%s" % x if not x.startswith("T") else x
                   for x in re.findall(r"depends on ((?:T\d+(?:,\s*|\s+and\s+)?)+)", it.text)
                   for x in re.findall(r"T\d+", x)]
        it.deps = [(src, x) for x in it.deps]
        groups.setdefault(it.req, []).append(it)
    for req, its in groups.items():  # [P] tasks need the stage's entry; others all open work of the stage
        entry, frontier, stage = set(), set(), None
        for it in its:
            if it.stage != stage:
                if stage is not None:
                    entry = frontier or entry
                stage, frontier = it.stage, set()
            k = (src, it.key)
            if it.parallel:
                it.deps += sorted(entry)
                frontier.add(k)
            else:
                it.deps += sorted(frontier or entry)
                frontier = {k}
        b.items.extend(its)
    setup, polish = "%s/setup" % feat, "%s/polish" % feat
    with_tasks = {it.req for _, it in found if not it.done and it.paths()}
    for rid in story_ids.values():
        if setup in with_tasks:
            b.nodes[rid]["fields"]["needs"] = [setup]
    if polish in b.nodes:
        need = [r for r in sorted(story_ids.values()) if r in with_tasks] or ([setup] if setup in with_tasks else [])
        if need:
            b.nodes[polish]["fields"]["needs"] = need


def _group_req(b, feat, group, src):
    nid = "%s/%s" % (feat, group)
    if nid not in b.nodes:
        b.names.setdefault(feat, set()).add(group)
        what = "Setup and foundation" if group == "setup" else "Polish and cross-cutting work"
        b.add(nid, "requirement", {"title": what},
              {"Statement": "%s tasks of %s that serve no single user story." % (what, src),
               "Notes": "Made by the import to hold tasks of %s." % src})
        b.flag("grouping", nid, src, "a requirement the import made to hold tasks: write its statement, or move"
               " its tasks under the requirements they serve (wavecoord spec mv)")
    return nid


# ------------------------------------------------------------------ Amazon Kiro

def import_kiro(b, src, granularity):
    base = os.path.join(src, ".kiro", "specs")
    if not os.path.isdir(base):
        base = src if os.path.basename(src.rstrip(os.sep)) == "specs" else base
    dirs = sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))) if os.path.isdir(base) else []
    dirs = [d for d in dirs if any(os.path.isfile(os.path.join(base, d, f)) for f in
                                   ("requirements.md", "bugfix.md", "design.md", "tasks.md"))]
    if not dirs:
        raise ImportFailed("no Kiro specs under %s (.kiro/specs/<feature>/requirements.md)" % b.rel(base))
    steer_dir = os.path.join(src, ".kiro", "steering")
    intent, design = "Features imported from Amazon Kiro (%s)." % b.rel(base), []
    if os.path.isdir(steer_dir):
        for f in sorted(os.listdir(steer_dir)):
            if not f.endswith(".md"):
                continue
            text = clean(read(os.path.join(steer_dir, f)))
            design += ["", "## Steering: %s" % b.rel(os.path.join(steer_dir, f))] + demote(text.split("\n"), 2)
            if f == "product.md":
                paras = paragraphs(text.split("\n"))
                if paras:
                    intent = "%s\n\n%s" % (paras[0], intent)
    b.add(b.cap, "capability", {"title": "Imported from Kiro"}, {"Intent": intent, "Design": prose(design)})
    for d in dirs:
        _kiro_feature(b, os.path.join(base, d), granularity)
    place_tasks(b, b.items)


def _kiro_feature(b, d, granularity):
    folder = os.path.basename(d)
    feat = "%s/%s" % (b.cap, b.name(b.cap, folder, b.feat_max, "feature"))
    req_path = os.path.join(d, "requirements.md")
    intro, design, stories = [], [], []
    if os.path.isfile(req_path):
        srel = b.rel(req_path)
        _, h2 = blocks(clean(read(req_path)).split("\n"), 2)
        for t, body in h2:
            key = heading_key(t)
            if key == "introduction":
                intro = body
            elif key == "requirements":
                _, h3 = blocks(body, 3)
                for st, sbody in h3:
                    stories.append((st, sbody))
            else:
                design += section(t, body)
                b.flag("unknown-section", feat, srel, "section %r is not a Kiro requirements section: kept in"
                       " Design" % t)
    else:
        srel = None
    bug = os.path.join(d, "bugfix.md")
    if os.path.isfile(bug):
        design += included(b, bug)
        b.flag("bugfix", feat, b.rel(bug), "a bugfix spec: kept in Design; write its requirements by hand")
    dpath = os.path.join(d, "design.md")
    if os.path.isfile(dpath):
        design += included(b, dpath)
    paras = paragraphs(intro)
    b.add(feat, "feature", {"title": folder.replace("-", " ").replace("_", " ").capitalize()},
          {"Intent": "%s\n\nImported from %s." % (" ".join(paras) or folder, b.rel(d)), "Design": prose(design)})
    refs = {}  # "1" or "1.2" -> requirement id
    for i, (st, sbody) in enumerate(stories):
        m = re.match(r"^Requirements?\s+(\d+)\s*[:.-]?\s*(.*)$", st, re.I)
        num = m.group(1) if m else str(i + 1)
        stitle = (m.group(2) if m else st).strip()
        story, crits, rest = "", [], []
        pre, h4 = blocks(sbody, 4)
        for ln in pre:
            s = ln.strip()
            mm = re.match(r"^\*\*User Story:?\*\*:?\s*(.+)$", s)
            if mm:
                story = mm.group(1).strip()
            elif s:
                rest.append(s)
        for t, body in h4:
            if heading_key(t) == "acceptance criteria":
                crits += [re.sub(r"^\d+[.)]\s+", "", x) for x in items(body)]
            else:
                rest += ["#### " + t] + body
        if not stitle:
            sm = re.search(r"I want (?:to )?(.+?)(?:,? so that|$)", story)
            stitle = sm.group(1) if sm else "Requirement %s" % num
        where = "%s (requirement %s)" % (srel, num)
        if granularity == "story" or not crits:
            if len(crits) == 1:
                stmt, what = crits[0], "acceptance criterion"
            else:
                stmt, what = story or stitle, "user story"
            rid = b.requirement(feat, "r%s" % num, "r%s" % num, stitle, stmt, where,
                                sections={"Rationale": story if stmt != story else ""}, what=what)
            if len(crits) > 1:
                b.note(rid, "Acceptance criteria:")
                for j, c in enumerate(crits):
                    st2, _ = to_ears(c)
                    b.note(rid, "- %s.%d %s" % (num, j + 1, st2))
            refs[num] = rid
            for j in range(len(crits)):
                refs["%s.%d" % (num, j + 1)] = rid
        else:
            for j, c in enumerate(crits):
                rid = b.requirement(feat, "r%s-%d" % (num, j + 1), "r%s-%d" % (num, j + 1),
                                    "%s (%s.%d)" % (stitle[:1].upper() + stitle[1:80], num, j + 1), c,
                                    "%s (requirement %s, criterion %d)" % (srel, num, j + 1),
                                    sections={"Rationale": story}, what="acceptance criterion")
                refs["%s.%d" % (num, j + 1)] = rid
                refs.setdefault(num, rid)
        if rest:
            for rid in set(v for k, v in refs.items() if k == num or k.startswith(num + ".")):
                b.note(rid, prose(rest))
    tpath = os.path.join(d, "tasks.md")
    if os.path.isfile(tpath):
        _kiro_tasks(b, feat, tpath, refs)


def _kiro_tasks(b, feat, path, refs):
    src = b.rel(path)
    found = checkbox_items(read(path), src)
    leaves, parents = [], {}
    for i, (indent, it) in enumerate(found):
        it.refs = []
        keep = []
        for dline in it.details:
            m = REQ_REF_RE.match(dline.strip().strip("*"))
            if m:
                it.refs += [x.strip() for x in re.split(r"[,\s]+", m.group(1).strip("_ ")) if x.strip()]
            else:
                keep.append(dline)
        it.details = keep
    for i, (indent, it) in enumerate(found):  # a parent is a numbered item whose sub-numbers follow
        kids = [x for _, x in found if x.key.startswith(it.key + ".") and x.key.count(".") == it.key.count(".") + 1]
        nxt = found[i + 1] if i + 1 < len(found) else None
        if not kids and nxt and nxt[0] > indent:
            kids = [x for ind, x in found[i + 1:] if ind > indent]
        if kids:
            for k in kids:
                parents[id(k)] = it
        else:
            leaves.append(it)
    prev = None
    for it in leaves:
        par = parents.get(id(it))
        if par is not None:
            it.context = "Part of %s %s." % (par.key, one_line(par.text, 120))
            it.details += par.details
            it.refs = it.refs or par.refs
            it.done = it.done or par.done
        else:
            it.context = ""
        it.req = next((refs[r] for r in it.refs if r in refs), None)
        if it.req is None:
            if it.refs:
                b.flag("unlinked", feat, src, "task %s names requirements %s, which requirements.md does not have"
                       % (it.key, ", ".join(it.refs)))
            it.req = _unlinked_req(b, feat, src)
        also = sorted(set(refs[r] for r in it.refs if r in refs) - {it.req})
        if also:
            it.notes.append("Also covers %s." % ", ".join(also))
        if it.optional:
            it.tags = ["optional"]
        it.deps = [(src, prev.key)] if prev is not None else []
        prev = it
        b.items.append(it)


def _unlinked_req(b, feat, src):
    nid = "%s/unlinked" % feat
    if nid not in b.nodes:
        b.names.setdefault(feat, set()).add("unlinked")
        b.add(nid, "requirement", {"title": "Tasks without a requirement"},
              {"Statement": "Tasks of %s that name no requirement." % src,
               "Notes": "Made by the import to hold tasks of %s." % src})
        b.flag("grouping", nid, src, "a requirement the import made to hold tasks that name no requirement: move"
               " them under the requirements they serve (wavecoord spec mv)")
    return nid


# ------------------------------------------------------------------ OpenSpec

def import_openspec(b, src):
    base = os.path.join(src, "openspec")
    if not os.path.isdir(base) and os.path.isdir(os.path.join(src, "specs")):
        base = src
    specs_dir, changes_dir = os.path.join(base, "specs"), os.path.join(base, "changes")
    caps = sorted(d for d in os.listdir(specs_dir) if os.path.isfile(os.path.join(specs_dir, d, "spec.md"))) \
        if os.path.isdir(specs_dir) else []
    changes = sorted(d for d in os.listdir(changes_dir) if d != "archive" and os.path.isdir(
        os.path.join(changes_dir, d))) if os.path.isdir(changes_dir) else []
    if not caps and not changes:
        raise ImportFailed("no OpenSpec specs or changes under %s (openspec/specs/<capability>/spec.md)" % (
            b.rel(base)))
    intent, design = "Capabilities imported from OpenSpec (%s)." % b.rel(base), []
    project = os.path.join(base, "project.md")
    if os.path.isfile(project):
        _, h2 = blocks(clean(read(project)).split("\n"), 2)
        for t, body in h2:
            if heading_key(t) == "purpose" and paragraphs(body):
                intent = "%s\n\n%s" % (" ".join(paragraphs(body)), intent)
            else:
                design += section(t, body)
        design = ["Imported from %s." % b.rel(project), ""] + design
    b.add(b.cap, "capability", {"title": "Imported from OpenSpec"}, {"Intent": intent, "Design": prose(design)})
    feats = {}  # OpenSpec capability -> feature id
    reqs = {}  # (feature id, requirement name lower) -> requirement id
    for c in caps:
        _openspec_spec(b, os.path.join(specs_dir, c, "spec.md"), c, feats, reqs)
    for ch in changes:
        _openspec_change(b, os.path.join(changes_dir, ch), ch, feats, reqs)
    arch = os.path.join(changes_dir, "archive")
    if os.path.isdir(arch):
        n = len([d for d in os.listdir(arch) if os.path.isdir(os.path.join(arch, d))])
        if n:
            b.info.append("skipped %d archived change(s) in %s: their deltas are already in %s" % (
                n, b.rel(arch), b.rel(specs_dir)))
    place_tasks(b, b.items)


def _os_feature(b, cap, feats, title=None, intent=None, src=None):
    if cap not in feats:
        fid = "%s/%s" % (b.cap, b.name(b.cap, cap, b.feat_max, "feature"))
        feats[cap] = fid
        b.add(fid, "feature", {"title": one_line(title or cap.replace("-", " ").capitalize(), 100)},
              {"Intent": "%s\n\nImported from %s." % (intent or "The %s capability." % cap, src)})
        if not intent:
            b.flag("no-purpose", fid, src, "no Purpose in the source: write the feature's Intent")
    return feats[cap]


def _os_requirement(name, body):
    """### Requirement block lines -> (statement source, other lines, scenarios, bad step lines)."""
    pre, h4 = blocks(body, 4)
    paras = paragraphs(pre)
    text = paras[0] if paras else ""
    from .ops import split_sentences
    sents = split_sentences(text).split("\n") if text else []
    pick = next((s for s in sents if SHALL_RE.search(s)), sents[0] if sents else name)
    rest = [s for s in sents if s != pick] + paras[1:]
    scenarios, bad, other = [], [], []
    for t, sbody in h4:
        m = re.match(r"^Scenario:\s*(.+)$", t, re.I)
        if not m:
            other += ["#### " + t] + sbody
            continue
        steps = []
        for ln in sbody:
            if not ln.strip():
                continue
            st = bullet_step(ln)
            if st:
                steps.append(st)
            else:
                bad.append(ln.strip())
        if steps:
            scenarios.append({"name": one_line(m.group(1), 120), "steps": steps})
    return pick, rest + other, scenarios, bad


def _os_blocks(lines):
    """`### Requirement: Name` blocks of some lines -> [(name, body)], and the other h3 blocks."""
    _, h3 = blocks(lines, 3)
    out, other = [], []
    for t, body in h3:
        m = re.match(r"^Requirement:\s*(.+)$", t, re.I)
        (out if m else other).append((m.group(1).strip() if m else t, body))
    return out, other


def _openspec_spec(b, path, cap, feats, reqs):
    srel = b.rel(path)
    pre, h2 = blocks(clean(read(path)).split("\n"), 2)
    title = next((m.group(2) for m in map(HEADING_RE.match, pre) if m), cap)
    title = re.sub(r"\s*Specification$", "", title).strip() or cap
    title = title[:1].upper() + title[1:]
    purpose, design, blocks_ = None, [], []
    for t, body in h2:
        key = heading_key(t)
        if key == "purpose":
            purpose = " ".join(paragraphs(body)) or None
        elif key == "requirements":
            found, other = _os_blocks(body)
            blocks_ += found
            for ot, ob in other:
                design += ["### " + ot] + ob
        else:
            design += section(t, body)
            b.flag("unknown-section", "%s/%s" % (b.cap, cap), srel, "section %r is not an OpenSpec section: kept"
                   " in Design" % t)
    fid = _os_feature(b, cap, feats, title, purpose, srel)
    if design:
        b.nodes[fid]["sections"]["Design"] = prose(design)
    for name, body in blocks_:
        _os_add(b, fid, name, body, srel, reqs)


def _os_add(b, fid, name, body, src, reqs):
    stmt, rest, scenarios, bad = _os_requirement(name, body)
    rid = b.requirement(fid, name, "req", name, stmt, "%s (Requirement: %s)" % (src, name),
                        sections={"Scenarios": scenarios})
    reqs[(fid, name.lower())] = rid
    if rest:
        b.note(rid, prose(rest))
    for ln in bad:
        b.note(rid, "- Scenario line (not WHEN/THEN): %s" % one_line(ln, 200))
        b.flag("scenario", rid, src, "a scenario line is not GIVEN/WHEN/THEN/AND: kept in Notes")
    return rid


def _openspec_change(b, d, change, feats, reqs):
    crel = b.rel(d)
    touched = []
    sdir = os.path.join(d, "specs")
    caps = sorted(c for c in os.listdir(sdir) if os.path.isfile(os.path.join(sdir, c, "spec.md"))) \
        if os.path.isdir(sdir) else []
    for c in caps:
        path = os.path.join(sdir, c, "spec.md")
        srel = b.rel(path)
        pre, h2 = blocks(clean(read(path)).split("\n"), 2)
        delta = {"added": [], "modified": [], "removed": [], "renamed": [], "purpose": None}
        for t, body in h2:
            m = re.match(r"^(ADDED|MODIFIED|REMOVED|RENAMED)\s+Requirements$", t.strip(), re.I)
            if m:
                found, _ = _os_blocks(body)
                if m.group(1).upper() == "RENAMED":
                    pairs = re.findall(r"FROM:\s*`?(?:###\s*)?Requirement:\s*(.+?)`?\s*$\s*[-*]?\s*TO:\s*`?(?:###\s*)?"
                                       r"Requirement:\s*(.+?)`?\s*$", "\n".join(body), re.M | re.I)
                    delta["renamed"] += [(a.strip(), z.strip()) for a, z in pairs]
                else:
                    delta[m.group(1).lower()] += found
            elif heading_key(t) == "purpose":
                delta["purpose"] = " ".join(paragraphs(body)) or None
            else:
                b.flag("unknown-section", None, srel, "section %r is not a delta section: skipped" % t)
        fid = _os_feature(b, c, feats, None, delta["purpose"], srel)
        for old, new in delta["renamed"]:
            rid = reqs.pop((fid, old.lower()), None)
            if rid is None:
                b.flag("delta", None, srel, "RENAMED %r: no such requirement in %s" % (old, fid))
                continue
            name = b.name(fid, new, REQ_MAX, "req")
            b.names[fid].discard(rid.rsplit("/", 1)[1])
            nid = "%s/%s" % (fid, name)
            b.rename(rid, nid)
            b.nodes[nid]["fields"]["title"] = one_line(new, 100)
            b.note(nid, "Renamed from %r by %s." % (old, srel))
            reqs[(fid, new.lower())] = nid
            touched.append(nid)
        for name, body in delta["removed"]:
            rid = reqs.get((fid, name.lower()))
            if rid is None:
                b.flag("delta", None, srel, "REMOVED %r: no such requirement in %s" % (name, fid))
                continue
            b.nodes[rid]["fields"]["status"] = "deprecated"
            b.note(rid, "Removed by %s.%s" % (srel, ("\n" + prose(body)) if prose(body) else ""))
            touched.append(rid)
        for name, body in delta["modified"]:
            rid = reqs.get((fid, name.lower()))
            if rid is None:
                b.flag("delta", None, srel, "MODIFIED %r: no such requirement in %s; added instead" % (name, fid))
                touched.append(_os_add(b, fid, name, body, srel, reqs))
                continue
            stmt, rest, scenarios, bad = _os_requirement(name, body)
            st, ok = to_ears(stmt)
            secs = b.nodes[rid]["sections"]
            secs["Statement"], secs["Scenarios"] = st, scenarios
            b.flags = [f for f in b.flags if not (f["id"] == rid and f["kind"] == "non-ears")]
            if not ok:
                b.flag("non-ears", rid, srel, "statement kept as written: rewrite it in EARS form (%s)" % one_line(
                    st, 80))
            b.note(rid, "Modified by %s." % srel)
            if rest:
                b.note(rid, prose(rest))
            touched.append(rid)
        for name, body in delta["added"]:
            if (fid, name.lower()) in reqs:
                b.flag("delta", reqs[(fid, name.lower())], srel, "ADDED %r exists already: kept the existing one" %
                       name)
                continue
            touched.append(_os_add(b, fid, name, body, srel, reqs))
    prop = os.path.join(d, "proposal.md")
    why, notes = "", []
    if os.path.isfile(prop):
        _, h2 = blocks(clean(read(prop)).split("\n"), 2)
        for t, body in h2:
            if heading_key(t) == "why":
                why = " ".join(paragraphs(body))
            else:
                notes += section(t, body)
    dpath = os.path.join(d, "design.md")
    if os.path.isfile(dpath):
        notes += included(b, dpath)
    tpath = os.path.join(d, "tasks.md")
    if not os.path.isfile(tpath):
        if touched:
            b.info.append("change %s has no tasks.md: its deltas are applied, nothing else" % change)
        return
    if "changes" not in feats:
        feats["changes"] = "%s/%s" % (b.cap, b.name(b.cap, "changes", b.feat_max, "changes"))
        b.add(feats["changes"], "feature", {"title": "Open changes"},
              {"Intent": "Made by the import to hold the tasks of OpenSpec changes that were not archived yet."})
    from .ops import split_sentences
    first = split_sentences(why).split("\n")[0] if why else "The change %s." % change
    rid = b.requirement(feats["changes"], change, "change", change.replace("-", " ").capitalize(), first,
                        "%s/proposal.md" % crel, sections={"Rationale": why}, what="proposal")
    if touched:
        b.note(rid, "Changes: %s." % ", ".join(sorted(set(touched))))
    if notes:
        b.note(rid, prose(notes))
    found = checkbox_items(read(tpath), b.rel(tpath))
    prev = None
    for _, it in found:
        it.req = rid
        group = re.sub(r"^[0-9]+[.)]?\s*", "", it.context or "")
        it.context = "Group: %s." % one_line(group, 120) if group else ""
        it.deps = [(it.src, prev.key)] if prev is not None else []
        prev = it
        b.items.append(it)


# ------------------------------------------------------------------ entry

def build(fmt, src, root, cap, granularity="criterion"):
    """-> (ops, report). Raises ImportFailed."""
    if fmt not in FORMATS:
        raise ImportFailed("--from is one of %s" % ", ".join(FORMATS))
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", cap or "") or cap in RESERVED or len(cap) > CAP_MAX:
        raise ImportFailed("--into %r: a capability name, lowercase letters, digits and '-', at most %d characters"
                           % (cap, CAP_MAX))
    if not os.path.isdir(src):
        raise ImportFailed("--src %s is not a folder" % src)
    b = Build(fmt, cap, root)
    if fmt == "speckit":
        import_speckit(b, src)
    elif fmt == "kiro":
        import_kiro(b, src, granularity)
    else:
        import_openspec(b, src)
    return b.ops(), b.report()


def source_dirs(fmt, src):
    """The folders the importer reads, to refuse an import whose source lies inside the spec dir."""
    if fmt == "speckit":
        base, dirs = speckit_dirs(src)
        return dirs
    if fmt == "kiro":
        return [os.path.join(src, ".kiro")]
    return [os.path.join(src, "openspec")]
