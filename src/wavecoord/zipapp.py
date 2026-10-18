"""Build the wavecoord zipapp and the pinned plugin archive. Stdlib only.

The zipapp build is deterministic: sorted entries, a fixed timestamp, fixed
file modes and no compression (zlib output can differ between platforms).
So the same source always gives the same bytes and the same content hash.

`scripts/build-zipapp.py` writes `plugin/bin/wavecoord` with `build_bytes()`.
The launcher ships `self_bytes()` to hosts as `~/.wavecoord/bin/wavecoord-<hash>`
and the plugin directory as `~/.wavecoord/plugin-<hash>/` (`plugin_tar()`).
"""

import hashlib
import io
import os
import pkgutil
import tarfile
import zipfile

PKG = "wavecoord"
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_ARCHIVE = getattr(globals().get("__loader__"), "archive", None)  # set by zipimport only
SRC_ROOT = os.path.dirname(PKG_DIR)  # src/ in a source tree
REPO_ROOT = os.path.dirname(SRC_ROOT)  # the repo root in a source tree
SHEBANG = b"#!/usr/bin/env python3\n"
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o100644 << 16
MAIN = b"import sys\nfrom wavecoord.cli import main\nsys.exit(main())\n"
# Every schemas/*.schema.json is embedded as package data (the server and the launcher use them).
EMBEDDED_SCHEMAS = ("config.schema.json", "report.schema.json", "task-spec.schema.json")  # used by the launcher


def running_from_archive():
    return bool(_ARCHIVE)


def self_path():
    """The zipapp file this code runs from, or None in a source tree."""
    return os.path.abspath(_ARCHIVE) if _ARCHIVE else None


def collect(src_root=None, repo_root=None):
    """Sorted (archive name, bytes) pairs: every .py file of the package plus embedded schemas."""
    src_root = src_root or SRC_ROOT
    repo_root = repo_root or os.path.dirname(src_root)
    entries = [("__main__.py", MAIN)]
    base = os.path.join(src_root, PKG)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__" and not d.startswith("."))
        for fn in filenames:
            if not fn.endswith(".py") or fn.startswith("."):
                continue
            full = os.path.join(dirpath, fn)
            arc = os.path.relpath(full, src_root).replace(os.sep, "/")
            with open(full, "rb") as f:
                data = f.read().replace(b"\r\n", b"\n")
            entries.append((arc, data))
    sdir = os.path.join(repo_root, "schemas")
    for name in sorted(os.listdir(sdir)):
        if name.endswith(".schema.json"):
            with open(os.path.join(sdir, name), "rb") as f:
                entries.append(("%s/_schemas/%s" % (PKG, name), f.read().replace(b"\r\n", b"\n")))
    return sorted(entries)


def build_bytes(src_root=None, repo_root=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for arc, data in collect(src_root, repo_root):
            info = zipfile.ZipInfo(arc, date_time=FIXED_TIME)
            info.external_attr = FILE_MODE
            info.create_system = 3  # unix, so the result is the same on every OS
            z.writestr(info, data, compress_type=zipfile.ZIP_STORED)
    return SHEBANG + buf.getvalue()


_SELF = {}


def self_bytes():
    """The bytes the launcher ships: the running zipapp, or one built from the source tree."""
    if "bytes" not in _SELF:
        if _ARCHIVE:
            with open(_ARCHIVE, "rb") as f:
                _SELF["bytes"] = f.read()
        else:
            _SELF["bytes"] = build_bytes()
    return _SELF["bytes"]


def content_hash(data):
    return hashlib.sha256(data).hexdigest()[:16]


def self_hash():
    if "hash" not in _SELF:
        _SELF["hash"] = content_hash(self_bytes())
    return _SELF["hash"]


def load_schema_bytes(name):
    """An embedded schema (zipapp) or the file in schemas/ (source tree)."""
    try:
        data = pkgutil.get_data(PKG, "_schemas/" + name)
        if data:
            return data
    except (OSError, ValueError):
        pass
    with open(os.path.join(REPO_ROOT, "schemas", name), "rb") as f:
        return f.read()


# ---- the pinned plugin copy -------------------------------------------------------------------

def default_plugin_dir():
    """The plugin dir this wavecoord belongs to, or None.

    WAVECOORD_PLUGIN_DIR wins. A zipapp at <plugin>/bin/wavecoord belongs to <plugin>.
    In a source tree it is <repo>/plugin."""
    env = os.environ.get("WAVECOORD_PLUGIN_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    cands = []
    if _ARCHIVE:
        cands.append(os.path.dirname(os.path.dirname(os.path.abspath(_ARCHIVE))))
    else:
        cands.append(os.path.join(REPO_ROOT, "plugin"))
    for c in cands:
        if os.path.isfile(os.path.join(c, ".claude-plugin", "plugin.json")):
            return c
    return None


def plugin_tar(plugin_dir):
    """A deterministic tar of `plugin_dir`, with bin/wavecoord replaced by self_bytes().

    So the hooks of a worker run the same wavecoord build as its launcher."""
    files = []
    for dirpath, dirnames, filenames in os.walk(plugin_dir):
        dirnames[:] = sorted(d for d in dirnames if d not in ("__pycache__", ".git"))
        for fn in sorted(filenames):
            if fn == ".DS_Store":
                continue
            full = os.path.join(dirpath, fn)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            files.append(os.path.relpath(full, plugin_dir).replace(os.sep, "/"))
    me = self_bytes()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as t:
        names = sorted(set(files) | {"bin/wavecoord"})
        for rel in names:
            if rel == "bin/wavecoord":
                data, mode = me, 0o755
            else:
                full = os.path.join(plugin_dir, rel)
                with open(full, "rb") as f:
                    data = f.read()
                mode = 0o755 if os.access(full, os.X_OK) else 0o644
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = mode
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def safe_extract(data, dest):
    """Extract a plugin tar into dest. Only regular files with relative, non-escaping names."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as t:
        for m in t.getmembers():
            name = m.name
            if not m.isfile() or name.startswith("/") or ".." in name.split("/") or "\\" in name:
                raise ValueError("unsafe entry in plugin archive: %r" % name)
            target = os.path.join(dest, *name.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            src = t.extractfile(m)
            with open(target, "wb") as f:
                f.write(src.read())
            os.chmod(target, 0o755 if m.mode & 0o111 else 0o644)
