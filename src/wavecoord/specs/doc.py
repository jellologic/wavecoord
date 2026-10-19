"""Parse and render the records of the spec tree (docs/design/spec-layer.md, section 3).

    rec = parse(text)          # -> dict: frontmatter keys + "sections"; raises SpecError
    text = render(rec)         # the canonical text (rule M8)

`render` writes one canonical form: the frame keys first (status, kind, schema),
the kind's other keys in order, block lists, sorted unordered lists, the kind's
sections in order, one blank line between sections, LF, a final newline. So
`parse(render(r)) == normalize(r)` and `render(parse(render(r))) == render(r)`.
`parse` accepts any valid input (flow lists, other key orders, extra blank
lines); `fmt` is `render(parse(text))`.
"""

import decimal
import json
import math
import re

from wavecoord.plan.spec import SpecError, _risks, _scalar, parse_frontmatter

from .model import ARTIFACT_KEYS, ARTIFACT_LISTS, EXEC_KEYS, FRAME, KINDS, STEP_WORDS, normalize, shape_errors

_PLAIN_RE = re.compile(r"^[A-Za-z0-9_./(<][^\x00-\x1f\x7f]*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_FLOW_SPECIAL = ",[]{}"


def fence(state, line):
    """Markdown code fences, shared by the parser, lint and the sentence splitter.
    `state` is None outside a fence, else (character, length) of the opening run.
    -> (new state, whether the line is a fence line or inside a fence).

    A fence opens with ``` or ~~~ (3 or more; a ``` fence's info string has no backquote, so ```x``` is
    inline code) and closes with a run of the same character at least as long and nothing after it."""
    m = _FENCE_RE.match(line)
    if state is None:
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            return (m.group(1)[0], len(m.group(1))), True
        return None, False
    if m and m.group(1)[0] == state[0] and len(m.group(1)) >= state[1] and not m.group(2).strip():
        return None, True
    return state, True


# ------------------------------------------------------------------ parse

def split(text):
    """-> (frontmatter lines, first line number of the frontmatter, body lines, first body line number)."""
    text = text.replace("\r\n", "\n")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise SpecError(["line 1: a record starts with a `---` frontmatter line"])
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise SpecError(["the frontmatter has no closing `---` line"])
    return lines[1:end], 2, lines[end + 1:], end + 2


def parse(text, kind=None):
    """The text of one record -> the record. `kind` (from the path) decides the sections when given;
    otherwise the file's own `kind` does. A mismatch is for `check` to report."""
    fm_lines, fm_first, body, body_first = split(text)
    errs = []
    try:
        rec = parse_frontmatter(fm_lines, fm_first)
    except SpecError as e:
        errs.extend(e.errors)
        rec = {}
    k = kind or rec.get("kind")
    if k not in KINDS:
        raise SpecError(errs + ["kind %r is not one of %s" % (k, ", ".join(sorted(KINDS)))])
    sections, serrs = _sections(KINDS[k], body, body_first)
    errs.extend(serrs)
    if errs:
        raise SpecError(errs)
    rec["sections"] = sections
    return rec


def _sections(kind, body, first):
    titles = {s.title.lower(): s for s in kind.sections}
    raw, errs, cur, fstate = {}, [], None, None
    for i, line in enumerate(body):
        n = first + i
        fstate, in_fence = fence(fstate, line)
        if not in_fence and line.startswith("## "):
            sec = titles.get(line[3:].strip().lower())
            if sec is None:
                errs.append("line %d: unknown section %r for a %s (allowed: %s)" % (
                    n, line[3:].strip(), kind.name, ", ".join(s.title for s in kind.sections)))
                cur = None
                continue
            if sec.key in raw:
                errs.append("line %d: duplicate section %s" % (n, sec.title))
            cur = sec
            raw[sec.key] = ([], n + 1)
            continue
        if cur is None:
            if line.strip():
                errs.append("line %d: text outside a section" % n)
            continue
        raw[cur.key][0].append(line)
    out = {}
    for s in kind.sections:
        if s.key not in raw:
            continue
        lines, start = raw[s.key]
        try:
            out[s.key] = _section_value(s, lines, start)
        except SpecError as e:
            errs.extend(e.errors)
    return out, errs


def _section_value(sec, lines, start):
    if sec.type == "prose":
        return _prose(lines)
    if sec.type in ("list", "commands"):
        return _items(lines, start, sec.type == "commands")
    if sec.type == "risks":
        return _risks("\n".join(lines), start)
    if sec.type == "scenarios":
        return _scenarios(lines, start)
    raise SpecError(["section type %s" % sec.type])


def _prose(lines):
    out, blank = [], False
    for l in lines:
        l = l.rstrip()
        if not l:
            blank = bool(out)
            continue
        if blank:
            out.append("")
            blank = False
        out.append(l)
    return "\n".join(out)


def _items(lines, start, commands):
    items, errs = [], []
    for i, l in enumerate(lines):
        s = l.strip()
        if not s:
            continue
        if not s.startswith("- ") or not s[2:].strip():
            errs.append("line %d: expected a `- item` line" % (start + i))
            continue
        item = s[2:].strip()
        if commands and len(item) >= 2 and item[0] == "`" and item[-1] == "`" and "`" not in item[1:-1]:
            item = item[1:-1]
        items.append(item)
    if errs:
        raise SpecError(errs)
    return items


def _scenarios(lines, start):
    out, errs, cur = [], [], None
    for i, l in enumerate(lines):
        s = l.strip()
        n = start + i
        if not s:
            continue
        if s.startswith("### "):
            cur = {"name": s[4:].strip(), "steps": []}
            if not cur["name"]:
                errs.append("line %d: a scenario needs a name" % n)
            out.append(cur)
            continue
        if not s.startswith("- ") or cur is None:
            errs.append("line %d: expected `### <scenario>` or a `- Given/When/Then/And/But` step" % n)
            continue
        word, _, rest = s[2:].strip().partition(" ")
        canon = word.capitalize()
        if canon not in STEP_WORDS or not rest.strip():
            errs.append("line %d: a step starts with Given, When, Then, And or But, then text" % n)
            continue
        cur["steps"].append([canon, rest.strip()])
    for sc in out:
        if not sc["steps"]:
            errs.append("scenario %r has no steps" % sc["name"])
    if errs:
        raise SpecError(errs)
    return out


# ------------------------------------------------------------------ render

def _float(v):
    if math.isnan(v) or math.isinf(v):
        raise SpecError(["%r is not a number the YAML subset can hold" % v])
    s = format(decimal.Decimal(repr(v)), "f")  # 1e-05 -> 0.00001: the subset reads only digits.digits
    return s if "." in s else s + ".0"


def scalar(v, flow=False):
    """A scalar in the YAML subset; quoted (JSON) only when a plain value would read differently.
    In a flow list (`flow`), a `,`, `[`, `]`, `{` or `}` is quoted too."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return repr(v)
    if isinstance(v, float):
        return _float(v)
    s = str(v)
    plain = (_PLAIN_RE.match(s) and s == s.strip() and ": " not in s and " #" not in s and not s.endswith(":")
             and s not in ("true", "false", "null", "~") and not (flow and any(c in s for c in _FLOW_SPECIAL)))
    if plain:  # and it reads back as the same string (not a number or a boolean)
        try:
            if _scalar(s, "render") == s:
                return s
        except SpecError:
            pass
    return json.dumps(s, ensure_ascii=False)


def _flow(items):
    return "[" + ", ".join(scalar(i, flow=True) for i in items) + "]"


def render(rec):
    rec = normalize(rec)
    kind = KINDS[rec["kind"]]
    out = ["---"]
    for k in FRAME + kind.keys:
        if k not in rec:
            continue
        v = rec[k]
        if k in ARTIFACT_LISTS:
            out.append("%s:" % k)
            for a in v:
                keys = [x for x in ARTIFACT_KEYS if x in a] + sorted(x for x in a if x not in ARTIFACT_KEYS)
                for j, ak in enumerate(keys):
                    out.append("%s%s: %s" % ("  - " if j == 0 else "    ", ak, scalar(a[ak])))
        elif k == "exec":
            out.append("exec:")
            keys = [x for x in EXEC_KEYS if x in v] + sorted(x for x in v if x not in EXEC_KEYS)
            for ek in keys:
                ev = v[ek]
                out.append("  %s: %s" % (ek, _flow(ev) if isinstance(ev, list) else scalar(ev)))
        elif isinstance(v, list):
            out.append("%s:" % k)
            out.extend("  - %s" % scalar(i) for i in v)
        else:
            out.append("%s: %s" % (k, scalar(v)))
    out.append("---")
    for s in kind.sections:
        if s.key not in rec["sections"]:
            continue
        v = rec["sections"][s.key]
        out += ["", "## " + s.title]
        if s.type == "prose":
            out.append(v)
        elif s.type == "list":
            out += ["- %s" % i for i in v]
        elif s.type == "commands":
            out += ["- `%s`" % c if "`" not in c else "- %s" % c for c in v]
        elif s.type == "risks":
            for r in v:
                out.append("- risk: %s" % scalar(r["risk"]))
                for rk in ("mitigation", "kind"):
                    if rk in r:
                        out.append("  %s: %s" % (rk, scalar(r[rk])))
        elif s.type == "scenarios":
            for j, sc in enumerate(v):
                if j:
                    out.append("")
                out.append("### " + sc["name"])
                out += ["- %s %s" % (w, t) for w, t in sc["steps"]]
    return "\n".join(out) + "\n"


def fmt(text, kind=None):
    """The canonical text of a record (rule M8). Raises SpecError when the text does not parse, or when the
    record cannot be written back whole (a kind that is not the path's, unknown keys, a list key that holds
    something else): fmt never drops data."""
    rec = parse(text, kind)
    if kind and "kind" not in rec:
        rec["kind"] = kind
    errs = shape_errors(rec, kind)
    if errs:
        raise SpecError(errs)
    return render(rec)
