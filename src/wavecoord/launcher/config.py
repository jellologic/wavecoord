"""The consumer config (`.claude/wavecoord.json`, docs/adapters.md) as the launcher sees it."""

import json
import os

from wavecoord import jsonschema_lite, zipapp

from .common import LauncherError, git_run

CONFIG_REL_PATH = os.path.join(".claude", "wavecoord.json")
DEFAULT_HOSTS = [{"name": "local", "slots": 2, "repo_path": "."}]
DEFAULT_BRANCH_PREFIX = "wave"
# `{name}` placeholders are filled per attempt; the same values are also in the
# environment as WAVECOORD_BRANCH, WAVECOORD_BASE, WAVECOORD_PR_TITLE and
# WAVECOORD_PR_BODY_FILE, for a shell-string pr_cmd.
DEFAULT_PR_CMD = ["gh", "pr", "create", "--head", "{branch}", "--base", "{base}",
                  "--title", "{title}", "--body-file", "{body_file}"]


def find_repo_root(start):
    """The directory above `start` that holds .claude/wavecoord.json, else the git top level."""
    d = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(d, CONFIG_REL_PATH)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    rc, out, _ = git_run(["rev-parse", "--show-toplevel"], start)
    return out if rc == 0 and out else os.path.abspath(start)


def config_schema():
    return json.loads(zipapp.load_schema_bytes("config.schema.json").decode("utf-8"))


class Config(object):
    def __init__(self, raw, root, path=None):
        self.raw = raw
        self.root = root
        self.path = path
        self.project = raw.get("project")
        self.branch_prefix = raw.get("branch_prefix") or DEFAULT_BRANCH_PREFIX
        self.pr_cmd = raw.get("pr_cmd") or DEFAULT_PR_CMD
        self.log_branch = raw.get("log_branch") or "coord/log"
        hosts = raw.get("hosts") or DEFAULT_HOSTS
        self.hosts = {}
        for h in hosts:
            h = dict(h)
            if not h.get("ssh"):
                h["ssh"] = None
                rp = os.path.expanduser(h["repo_path"])
                h["repo_path"] = os.path.normpath(rp if os.path.isabs(rp) else os.path.join(root, rp))
            if h["name"] in self.hosts:
                raise LauncherError("duplicate host name %r in %s" % (h["name"], path or "config"))
            self.hosts[h["name"]] = h
        self.wip_cap = raw.get("wip_cap") or sum(h["slots"] for h in self.hosts.values())

    def coord_url(self):
        from wavecoord.cli import resolve_coord_url
        return resolve_coord_url(cwd=self.root)


def load_config(path=None, cwd=None):
    """Load and validate the consumer config. A repo without one gets the defaults."""
    cwd = cwd or os.getcwd()
    if path:
        path = os.path.abspath(path)
        root = find_repo_root(os.path.dirname(os.path.dirname(path)))
    else:
        root = find_repo_root(cwd)
        cand = os.path.join(root, CONFIG_REL_PATH)
        path = cand if os.path.isfile(cand) else None
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            raise LauncherError("cannot read %s: %s" % (path, e))
        errs = jsonschema_lite.validate(raw, config_schema())
        if errs:
            raise LauncherError("invalid %s: %s" % (path, "; ".join(errs[:5])))
    else:
        raw = {"project": os.path.basename(root).lower() or "default"}
    return Config(raw, root, path)
