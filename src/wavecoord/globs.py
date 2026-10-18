"""Conservative overlap test for repo-relative path globs.

Two globs overlap when one path could match both. The test looks only at the
literal prefix before the first wildcard, so it can report a false overlap
(a false alarm is fine) but never misses a real one. A glob with no wildcard
is a literal path; a literal path also covers everything below it, because it
can name a directory.
"""

import re

WILD = "*?["
_COVERS_RX = {}


def covers_rx(glob):
    """The compiled regex of `covers` for one glob (cached).

    `*` is any run of characters other than `/`; `?` is one character other
    than `/`; `**/` is zero or more whole directories; `**` elsewhere is any
    run of characters; every other character, `[` included, is literal. A
    match also covers every path below it, so a glob can name a directory.
    """
    rx = _COVERS_RX.get(glob)
    if rx is None:
        g = glob.rstrip("/") or glob
        out, i = [], 0
        while i < len(g):
            c = g[i]
            if g.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if g.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c))
            i += 1
        rx = re.compile("^" + "".join(out) + "(?:/.*)?$", re.S)
        if len(_COVERS_RX) > 4096:
            _COVERS_RX.clear()
        _COVERS_RX[glob] = rx
    return rx


def covers(glob, path, fold_case=False):
    """Does glob `glob` match the literal repo-relative path `path` (or a directory above it)?

    With fold_case, both sides are compared case-folded (for the foreign
    check on a case-insensitive file system: it denies more).
    """
    if not isinstance(glob, str) or not isinstance(path, str):
        return False
    if fold_case:
        glob, path = glob.casefold(), path.casefold()
    return bool(covers_rx(glob).match(path))


def literal_prefix(g):
    """(text before the first wildcard, True if the glob has a wildcard)."""
    i = min([g.index(c) for c in WILD if c in g] or [len(g)])
    return g[:i], i < len(g)


def globs_overlap(a, b):
    """Could one path match both globs? Conservative: a false alarm is fine."""
    (pa, wa), (pb, wb) = literal_prefix(a), literal_prefix(b)
    if not wa and not wb:  # two literal paths: equal, or one is a directory of the other
        a, b = a.rstrip("/"), b.rstrip("/")
        return a == b or a.startswith(b + "/") or b.startswith(a + "/")
    if wa and wb:
        return pa.startswith(pb) or pb.startswith(pa)
    lit, pre = (a, pb) if not wa else (b, pa)
    return lit.startswith(pre) or pre.startswith(lit.rstrip("/") + "/")


def valid_glob(g):
    """The glob rules of contract section 3.2: repo-relative, no space, no `..`, no braces."""
    if not isinstance(g, str) or not g or g.startswith("/"):
        return False
    if any(c.isspace() for c in g) or "{" in g or "}" in g:
        return False
    return ".." not in g.split("/")
