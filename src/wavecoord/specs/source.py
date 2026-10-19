"""Read every file under the spec directory, from a working tree or from a git ref.

    files = read_tree(root, spec_dir)            # working tree: {relative path: text}
    files = read_ref(repo, "origin/main", spec_dir)   # a git ref, in two processes for any number of files

Paths are relative to the spec directory, with "/" separators.
"""

import os
import subprocess


class SourceError(RuntimeError):
    pass


def read_tree(root, spec_dir):
    base = os.path.join(root, spec_dir)
    out = {}
    if not os.path.isdir(base):
        return out
    for d, dirs, files in os.walk(base):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        for f in sorted(files):
            if f.startswith("."):
                continue
            p = os.path.join(d, f)
            rel = os.path.relpath(p, base).replace(os.sep, "/")
            try:
                with open(p, "rb") as fh:
                    out[rel] = fh.read().decode("utf-8", "replace")
            except OSError as e:
                raise SourceError("cannot read %s: %s" % (p, e))
    return out


def _git(repo, args, data=None):
    p = subprocess.run(["git"] + args, cwd=repo, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=300)
    if p.returncode != 0:
        raise SourceError("git %s: %s" % (" ".join(args), p.stderr.decode("utf-8", "replace").strip()[-300:]))
    return p.stdout


def read_ref(repo, ref, spec_dir):
    prefix = spec_dir.strip("/") + "/"
    listing = _git(repo, ["ls-tree", "-r", "-z", "--full-tree", ref, "--", prefix])
    blobs = []
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        parts = meta.split()
        if len(parts) != 3 or parts[1] != b"blob":
            continue
        rel = path.decode("utf-8", "replace")[len(prefix):]
        if any(seg.startswith(".") for seg in rel.split("/")):
            continue
        blobs.append((rel, parts[2].decode()))
    if not blobs:
        return {}
    data = _git(repo, ["cat-file", "--batch"], ("\n".join(sha for _, sha in blobs) + "\n").encode())
    out, pos = {}, 0
    for rel, sha in blobs:
        nl = data.index(b"\n", pos)
        head = data[pos:nl].split()
        if len(head) != 3 or head[1] != b"blob":
            raise SourceError("git cat-file: unexpected answer for %s" % rel)
        size = int(head[2])
        out[rel] = data[nl + 1:nl + 1 + size].decode("utf-8", "replace")
        pos = nl + 1 + size + 1
    return out
