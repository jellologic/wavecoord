#!/usr/bin/env python3
"""One check of the demo's state for the rig's `wait_shell` steps: exit 0 when the condition holds, else 1.

    python3 wait.py idle --min 2        at least 2 attempts exist and none is in flight
    python3 wait.py running --min 1     at least 1 attempt is running
    python3 wait.py started --min 4     at least 4 attempts exist, in any state (a fast worker may be done)
    python3 wait.py done --min 2        at least 2 tasks are `done` on origin's default branch

Run it in the demo repo, with `wavecoord` on PATH (or WAVECOORD_BIN). Stdlib only.
"""

import argparse
import json
import os
import subprocess
import sys

FINAL = ("reported", "failed", "timed_out", "stopped", "lost", "merged", "abandoned")


def wavecoord(*args):
    exe = os.environ.get("WAVECOORD_BIN")
    argv = ([sys.executable, exe] if exe else ["wavecoord"]) + list(args)
    p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip()[-300:])
    return json.loads(p.stdout or "null")


def holds(cond, n, attempts=None, done=None):
    """Pure: does `cond` hold for these attempt rows (or this count of done tasks)?"""
    if cond == "idle":
        return len(attempts) >= n and all(a.get("state") in FINAL for a in attempts)
    if cond == "started":
        return len(attempts) >= n
    if cond == "running":
        return sum(1 for a in attempts if a.get("state") == "running") >= n
    if cond == "done":
        return done >= n
    raise ValueError(cond)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cond", choices=("idle", "running", "started", "done"))
    ap.add_argument("--min", type=int, default=1)
    a = ap.parse_args(argv)
    try:
        if a.cond == "done":
            subprocess.run(["git", "fetch", "-q", "origin"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            rows = wavecoord("spec", "query", "kind=task status=done", "--ref", "origin/HEAD", "--json")
            ok = holds("done", a.min, done=len(rows or []))
            print("%d tasks done" % len(rows or []))
        else:
            rows = wavecoord("attempts", "ls", "--json") or []
            ok = holds(a.cond, a.min, attempts=rows)
            print(", ".join("%s %s" % (r.get("id"), r.get("state")) for r in rows) or "no attempts")
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as e:
        print("not yet: %s" % e)
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
