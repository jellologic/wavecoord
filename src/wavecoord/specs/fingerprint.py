"""The fingerprint of a requirement (docs/design/spec-layer.md, section 3.8).

It covers what a task builds against: the Statement, the Scenarios, the Verify
commands, and the inputs and outputs. It ignores the title, status, tags,
aliases, Rationale, Notes and the path, and an input's `from` (a path, which a
move rewrites), so a reflow, a reorder, a status change or a move leaves it
unchanged. A task stores it as `basis`.
"""

import hashlib
import json
import unicodedata

PREFIX = b"wavecoord-req-fp/1\n"


def norm(s):
    s = unicodedata.normalize("NFC", str(s)).strip()
    if len(s) >= 2 and s[0] == "`" and s[-1] == "`":
        s = s[1:-1]
    return " ".join(s.split())


def _artifact(a):
    return {k: (norm(v) if isinstance(v, str) else v) for k, v in sorted(a.items()) if k != "from"}


def canonical(rec):
    sec = rec.get("sections") or {}
    data = {
        "statement": norm(sec.get("statement") or ""),
        "scenarios": sorted([norm(sc["name"]), [[w.lower(), norm(t)] for w, t in sc["steps"]]]
                            for sc in sec.get("scenarios") or []),
        "verify": sorted(norm(c) for c in sec.get("verify") or []),
        "inputs": sorted((_artifact(a) for a in rec.get("inputs") or []), key=lambda a: json.dumps(a, sort_keys=True)),
        "outputs": sorted((_artifact(a) for a in rec.get("outputs") or []), key=lambda a: json.dumps(a, sort_keys=True)),
    }
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def fingerprint(rec):
    """8 hex digits."""
    return hashlib.sha256(PREFIX + canonical(rec)).hexdigest()[:8]
