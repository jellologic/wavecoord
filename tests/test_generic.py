"""The repo is public and generic: no secrets, no names of a private consumer."""

import os
import re
import subprocess
import unittest

import _paths

# Built from parts so this file does not match itself.
FORBIDDEN = [
    re.compile(r"\b10\.1\.1\.\d+"),
    re.compile(r"\bdev[1-3]\b"),
    re.compile("ez" + "ra", re.I),
    re.compile(r"\be" + r"zh\b"),
    re.compile(r"\bW" + r"Ps?\b"),
    re.compile("STE" + "100"),
]
SECRETS = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]


def tracked_files():
    try:
        out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             cwd=_paths.ROOT, stdout=subprocess.PIPE, check=True).stdout
        return [f for f in out.decode().split("\0") if f]
    except (OSError, subprocess.CalledProcessError):
        files = []
        for d, dirs, fs in os.walk(_paths.ROOT):
            dirs[:] = [x for x in dirs if x not in (".git", "__pycache__")]
            files += [os.path.relpath(os.path.join(d, f), _paths.ROOT) for f in fs]
        return files


class GenericTest(unittest.TestCase):
    def test_no_private_names_or_secrets(self):
        bad = []
        for rel in tracked_files():
            path = os.path.join(_paths.ROOT, rel)
            try:
                with open(path, "rb") as f:
                    text = f.read().decode("utf-8", "replace")
            except OSError:
                continue
            for rx in FORBIDDEN + SECRETS:
                for m in rx.finditer(text):
                    bad.append("%s: %r" % (rel, m.group(0)))
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
