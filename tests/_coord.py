"""Test helpers: a temporary consumer repo with a local bare remote, a fake clock."""

import os
import shutil
import subprocess
import tempfile

import _paths  # noqa: F401

T0 = 1792324800.0  # 2026-10-18T12:00:00Z


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    return subprocess.run(["git", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"] + list(args),
                          cwd=cwd, env=env, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE).stdout.decode()


class Clock(object):
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class TempRepo(object):
    """A consumer repo with an `origin` bare remote, and a wavecoord home."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="wavecoord-test-")
        self.remote = os.path.join(self.root, "remote.git")
        self.repo = os.path.join(self.root, "repo")
        self.home = os.path.join(self.root, "home")
        git(self.root, "init", "-q", "--bare", self.remote)
        git(self.root, "init", "-q", self.repo)
        git(self.repo, "remote", "add", "origin", self.remote)
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def make_coord(tr, clock=None, **kw):
    from wavecoord.core import Coord
    kw.setdefault("commit_window_ms", 0)
    return Coord(tr.repo, home=tr.home, clock=clock, **kw)
