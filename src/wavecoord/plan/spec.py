"""Parse a task spec file (docs/task-spec.md) into the shape of task-spec.schema.json.

The file is YAML-subset frontmatter between `---` lines, then Markdown body
sections. The frontmatter subset is:

    key: scalar            # int, float, true/false, 'single' or "double" quoted, or bare text
    key: [a, b, 'c d']     # a flow list of scalars ([] for empty)
    key:                   # a block list of scalars, one item per line
      - a
      - b
    key:                   # a block list of flat maps (the spec tree's artifacts)
      - id: x
        kind: file
    exec:                  # exactly one level of nesting, indented by spaces
      key: scalar-or-list
    # a full-line comment

Anything else (nested collections, multi-line values, anchors, tabs) is an error.
The body sections are `## Context`, `## Risks`, `## Acceptance`, `## Prompt` and
`## Notes`; another `## ` heading is an error. Stdlib only.
"""

import json
import re

from wavecoord import schemaload

SECTIONS = {"context": "context", "risks": "risks", "acceptance": "acceptance", "prompt": "prompt",
            "notes": "notes"}
NESTED = ("exec",)
KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?:\s+(.*?))?\s*$")
INT_RE = re.compile(r"^-?[0-9]+$")
FLOAT_RE = re.compile(r"^-?[0-9]+\.[0-9]+$")


class SpecError(ValueError):
    """The file is outside the format; `errors` lists every problem found."""

    def __init__(self, errors):
        self.errors = list(errors)
        ValueError.__init__(self, "; ".join(self.errors))


def _scalar(text, where):
    t = text.strip()
    if t == "":
        raise SpecError(["%s: empty value" % where])
    if t[0] in "'\"":
        q = t[0]
        if len(t) < 2 or t[-1] != q:
            raise SpecError(["%s: unterminated quote" % where])
        inner = t[1:-1]
        if q == "'":
            if "'" in inner.replace("''", ""):
                raise SpecError(["%s: a ' inside a single-quoted value must be doubled" % where])
            return inner.replace("''", "'")
        try:
            return json.loads(t)
        except ValueError:
            raise SpecError(["%s: bad double-quoted value" % where])
    if t[0] in "[{&*!|>%@`" or t.startswith("- "):
        raise SpecError(["%s: value %r is outside the YAML subset" % (where, t)])
    if INT_RE.match(t):
        return int(t)
    if FLOAT_RE.match(t):
        return float(t)
    if t in ("true", "false"):
        return t == "true"
    if t in ("null", "~"):
        raise SpecError(["%s: null is not allowed; leave the key out" % where])
    if " #" in t:
        t = t.split(" #", 1)[0].rstrip()
    return t


def _split_flow(inner, where):
    items, cur, q = [], "", None
    for ch in inner:
        if q:
            cur += ch
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
            cur += ch
        elif ch == ",":
            items.append(cur)
            cur = ""
        elif ch in "[]{}":
            raise SpecError(["%s: nested collections are outside the YAML subset" % where])
        else:
            cur += ch
    if q:
        raise SpecError(["%s: unterminated quote in list" % where])
    items.append(cur)
    if len(items) == 1 and not items[0].strip():
        return []
    if any(not i.strip() for i in items):
        raise SpecError(["%s: empty list item" % where])
    return items


def _value(text, where):
    t = (text or "").strip()
    if t.startswith("["):
        if not t.endswith("]"):
            raise SpecError(["%s: a flow list must end with ]" % where])
        return [_scalar(i, where) for i in _split_flow(t[1:-1], where)]
    return _scalar(t, where)


def parse_frontmatter(lines, first_line=2):
    """Frontmatter lines (between the --- markers) -> dict."""
    out, errs = {}, []
    rows = []  # (line number, indent, stripped text) of the lines that carry content
    for i, raw in enumerate(lines):
        where = "line %d" % (first_line + i)
        if "\t" in raw:
            errs.append("%s: tabs are not allowed" % where)
            continue
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        rows.append((first_line + i, len(raw) - len(raw.lstrip(" ")), raw.strip()))
    i = 0
    while i < len(rows):
        ln, indent, text = rows[i]
        where = "line %d" % ln
        i += 1
        try:
            m = KEY_RE.match(text)
            if indent != 0 or not m:
                raise SpecError(["%s: %s" % (where, "unexpected indentation" if indent else
                                                     "expected `key: value`, got %r" % text[:80])])
            key, val = m.group(1), m.group(2)
            if key in out:
                raise SpecError(["%s: duplicate key %s" % (where, key)])
            if val not in (None, ""):
                out[key] = _value(val, where)
                continue
            body = []
            while i < len(rows) and rows[i][1] > 0:
                body.append(rows[i])
                i += 1
            if body and body[0][2].startswith("- "):
                out[key] = _block_list(key, body)
            elif key in NESTED and body:
                out[key] = _nested_map(key, body)
            elif key in NESTED:
                out[key] = {}
            else:
                raise SpecError(["%s: %s needs a value (an empty list is `%s: []`)" % (where, key, key)])
        except SpecError as e:
            errs.extend(e.errors)
    if errs:
        raise SpecError(errs)
    return out


def _nested_map(key, body):
    out = {}
    for ln, _indent, text in body:
        where = "line %d" % ln
        m = KEY_RE.match(text)
        if not m:
            raise SpecError(["%s: expected `key: value` under %s" % (where, key)])
        if m.group(2) in (None, ""):
            raise SpecError(["%s: %s.%s needs a value (only one level of nesting)" % (where, key, m.group(1))])
        if m.group(1) in out:
            raise SpecError(["%s: duplicate key %s.%s" % (where, key, m.group(1))])
        out[m.group(1)] = _value(m.group(2), where)
    return out


def _block_list(key, body):
    """`- item` lines: all scalars, or all flat maps (`- k: v` then indented `k: v` lines)."""
    items, maps, errs = [], None, []
    dash = body[0][1]
    cur = None
    for ln, indent, text in body:
        where = "line %d" % ln
        if indent == dash and text.startswith("- "):
            item = text[2:].strip()
            m = KEY_RE.match(item)
            is_map = bool(m) and m.group(2) not in (None, "")
            if maps is None:
                maps = is_map
            if is_map != maps:
                errs.append("%s: %s mixes scalars and maps" % (where, key))
                cur = None
                continue
            if not item:
                errs.append("%s: empty list item" % where)
                continue
            if is_map:
                cur = {m.group(1): _value(m.group(2), where)}
                if isinstance(cur[m.group(1)], list):
                    errs.append("%s: a list inside a list item is outside the YAML subset" % where)
                items.append(cur)
            else:
                try:
                    items.append(_scalar(item, where))
                except SpecError as e:
                    errs.extend(e.errors)
            continue
        m = KEY_RE.match(text)
        if not maps or cur is None or indent <= dash or not m:
            errs.append("%s: unexpected line in the list %s: %r" % (where, key, text[:80]))
            continue
        if m.group(2) in (None, ""):
            errs.append("%s: %s needs a value (no nesting inside a list item)" % (where, m.group(1)))
            continue
        if m.group(1) in cur:
            errs.append("%s: duplicate key %s in a list item" % (where, m.group(1)))
            continue
        v = _value(m.group(2), where)
        if isinstance(v, list):
            errs.append("%s: a list inside a list item is outside the YAML subset" % where)
            continue
        cur[m.group(1)] = v
    if errs:
        raise SpecError(errs)
    return items


def _risks(text, first_line):
    """`- risk: ...` items, each with indented `mitigation:` and optional `kind:`."""
    risks, errs, cur = [], [], None
    for i, raw in enumerate(text.split("\n")):
        where = "line %d" % (first_line + i)
        s = raw.strip()
        if not s:
            continue
        if s.startswith("- "):
            m = KEY_RE.match(s[2:].strip())
            if not m or m.group(1) != "risk" or not m.group(2):
                errs.append("%s: a risk item starts with `- risk: <text>`" % where)
                cur = None
                continue
            cur = {"risk": _unquote(m.group(2))}
            risks.append(cur)
            continue
        m = KEY_RE.match(s)
        if cur is None or not m or m.group(1) not in ("mitigation", "kind") or not m.group(2):
            errs.append("%s: expected `mitigation: <text>` or `kind: <kind>` under a risk" % where)
            continue
        if m.group(1) in cur:
            errs.append("%s: duplicate %s" % (where, m.group(1)))
            continue
        cur[m.group(1)] = _unquote(m.group(2))
    if errs:
        raise SpecError(errs)
    return risks


def _unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        try:
            return _scalar(s, "value")
        except SpecError:
            return s
    return s


def _list(text, first_line):
    items, errs = [], []
    for i, raw in enumerate(text.split("\n")):
        s = raw.strip()
        if not s:
            continue
        if not s.startswith("- ") or not s[2:].strip():
            errs.append("line %d: expected a `- item` line" % (first_line + i))
            continue
        item = s[2:].strip()
        if len(item) >= 2 and item[0] == "`" and item[-1] == "`" and "`" not in item[1:-1]:
            item = item[1:-1]
        items.append(item)
    if errs:
        raise SpecError(errs)
    return items


def parse_text(text, name=None):
    """The text of a spec file -> the parsed spec (not yet schema-checked). Raises SpecError."""
    text = text.replace("\r\n", "\n")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise SpecError(["line 1: a spec starts with a `---` frontmatter line"])
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise SpecError(["the frontmatter has no closing `---` line"])
    errs = []
    try:
        fm = parse_frontmatter(lines[1:end], 2)
    except SpecError as e:
        errs.extend(e.errors)
        fm = {}
    sections, order = {}, []
    cur, buf, start = None, [], end + 2
    for i in range(end + 1, len(lines)):
        raw = lines[i]
        if raw.startswith("## "):
            if cur is not None:
                sections[cur] = ("\n".join(buf), start)
            head = raw[3:].strip().lower()
            key = SECTIONS.get(head)
            if key is None:
                errs.append("line %d: unknown section %r (allowed: %s)"
                            % (i + 1, raw[3:].strip(), ", ".join(s.title() for s in SECTIONS)))
                cur, buf = None, []
                continue
            if key in sections:
                errs.append("line %d: duplicate section %s" % (i + 1, raw[3:].strip()))
            cur, buf, start = key, [], i + 2
            order.append(key)
            continue
        if cur is None:
            if raw.strip() and not raw.startswith("# "):
                errs.append("line %d: text outside a section" % (i + 1))
            continue
        buf.append(raw)
    if cur is not None:
        sections[cur] = ("\n".join(buf), start)
    sec = {}
    for key, (body, first) in sections.items():
        try:
            if key == "risks":
                sec[key] = _risks(body, first)
            elif key == "acceptance":
                sec[key] = _list(body, first)
            else:
                sec[key] = body.strip()
        except SpecError as e:
            errs.extend(e.errors)
    if errs:
        raise SpecError(["%s: %s" % (name, e) if name else e for e in errs])
    spec = dict(fm)
    spec["sections"] = sec
    return spec


def schema_errors(spec):
    return schemaload.errors(spec, "task-spec")


def load_text(text, name=None, expect_id=None):
    """Parse and validate. -> spec. Raises SpecError with every problem."""
    spec = parse_text(text, name)
    errs = schema_errors(spec)
    if expect_id is not None and spec.get("id") != expect_id:
        errs.append("id %r is not the file name %r" % (spec.get("id"), expect_id))
    if errs:
        raise SpecError(["%s: %s" % (name, e) if name else e for e in errs])
    return spec
