#!/usr/bin/env python3
"""Build plugin/bin/wavecoord, an executable Python zipapp, from src/wavecoord.

The build logic is in src/wavecoord/zipapp.py (the launcher uses it too). It
also embeds every schemas/*.schema.json.
The build is deterministic: sorted entries, a fixed timestamp, fixed file modes
and no compression (zlib output can differ between platforms). So the committed
file can be checked in CI:

    scripts/build-zipapp.py          # write plugin/bin/wavecoord
    scripts/build-zipapp.py --check  # exit 1 if the committed file is stale
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
OUT = os.path.join(ROOT, "plugin", "bin", "wavecoord")

sys.path.insert(0, SRC)
from wavecoord import zipapp  # noqa: E402  (the build logic is shared with the launcher)


def build_bytes():
    return zipapp.build_bytes(SRC, ROOT)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="fail if the committed zipapp is out of date")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)
    data = build_bytes()
    if args.check:
        try:
            with open(args.out, "rb") as f:
                current = f.read()
        except OSError:
            current = None
        if current != data:
            sys.stderr.write("%s is out of date: run scripts/build-zipapp.py and commit the result\n" % args.out)
            return 1
        if not os.access(args.out, os.X_OK):
            sys.stderr.write("%s is not executable\n" % args.out)
            return 1
        print("ok: %s is up to date" % os.path.relpath(args.out, ROOT))
        return 0
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "wb") as f:
        f.write(data)
    os.chmod(args.out, 0o755)
    print("wrote %s (%d bytes)" % (os.path.relpath(args.out, ROOT), len(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
