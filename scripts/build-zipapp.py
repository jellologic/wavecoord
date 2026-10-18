#!/usr/bin/env python3
"""Build plugin/bin/wavecoord, an executable Python zipapp, from src/wavecoord.

The build is deterministic: sorted entries, a fixed timestamp, fixed file modes
and no compression (zlib output can differ between platforms). So the committed
file can be checked in CI:

    scripts/build-zipapp.py          # write plugin/bin/wavecoord
    scripts/build-zipapp.py --check  # exit 1 if the committed file is stale
"""

import argparse
import io
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
PKG = "wavecoord"
OUT = os.path.join(ROOT, "plugin", "bin", "wavecoord")
SHEBANG = b"#!/usr/bin/env python3\n"
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o100644 << 16
MAIN = b"import sys\nfrom wavecoord.cli import main\nsys.exit(main())\n"


def collect():
    """Sorted (archive name, bytes) pairs for every .py file of the package."""
    entries = [("__main__.py", MAIN)]
    base = os.path.join(SRC, PKG)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for fn in filenames:
            if not fn.endswith(".py") or fn.startswith("."):
                continue
            full = os.path.join(dirpath, fn)
            arc = os.path.relpath(full, SRC).replace(os.sep, "/")
            with open(full, "rb") as f:
                data = f.read().replace(b"\r\n", b"\n")
            entries.append((arc, data))
    return sorted(entries)


def build_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for arc, data in collect():
            info = zipfile.ZipInfo(arc, date_time=FIXED_TIME)
            info.external_attr = FILE_MODE
            info.create_system = 3  # unix, so the result is the same on every OS
            z.writestr(info, data, compress_type=zipfile.ZIP_STORED)
    return SHEBANG + buf.getvalue()


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
