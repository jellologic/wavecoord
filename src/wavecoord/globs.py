"""Conservative overlap test for repo-relative path globs.

Two globs overlap when one path could match both. The test looks only at the
literal prefix before the first wildcard, so it can report a false overlap
(a false alarm is fine) but never misses a real one. A glob with no wildcard
is a literal path; a literal path also covers everything below it, because it
can name a directory.
"""

WILD = "*?["


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
