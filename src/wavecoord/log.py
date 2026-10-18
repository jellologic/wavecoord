"""The coordination event log: a single-writer hash chain on an orphan git branch.

Layout of the branch (docs/contract.md section 2):

    events/<yyyy>/<mm>/<dd>/<seq:012d>-<hash12>.json   one event per file
    snapshots/<seq:012d>.json                          folded state at <seq>
    meta.json                                          {"schema": 1, "project": "<project>"}

The branch is checked out in a dedicated worktree of the consumer repo
(`$WAVECOORD_HOME/log`). Only the server writes to it.

- `append()` validates an event, assigns `seq` and `prev`, writes the file and
  queues it for the next group commit. A second append with the same `idem`
  returns the first event and writes nothing.
- A committer thread commits all queued files at most `COMMIT_WINDOW_MS` after
  the first one. `wait(seq)` returns only after the commit that holds `seq`, so
  the caller can acknowledge a request only after the event is durable.
- A pusher thread pushes the branch to `origin` (fast-forward only) after each
  commit and retries failures every `PUSH_RETRY_S`. A push never blocks an ack.
- `open()` verifies the chain: contiguous seqs, file name hashes and `prev`.
"""

import json
import os
import re
import subprocess
import threading
import time

from wavecoord import const, schemaload

EVENT_RE = re.compile(r"^(\d{12})-([0-9a-f]{12})\.json$")
SNAP_RE = re.compile(r"^(\d{12})\.json$")
GIT_IDENTITY = ["-c", "user.name=wavecoord", "-c", "user.email=wavecoord@localhost",
                "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]


class LogError(Exception):
    """The log is unusable: a broken chain, a failed commit or a git error."""


def git(cwd, *args, check=True, input=None, timeout=60):
    """Run git in `cwd`; return stdout as text. Raise LogError when `check` fails."""
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(k, None)
    try:
        p = subprocess.run(["git"] + GIT_IDENTITY + list(args), cwd=cwd, input=input,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        if check:
            raise LogError("git %s: %s" % (" ".join(args), e))
        return None
    if check and p.returncode != 0:
        raise LogError("git %s: %s" % (" ".join(args), p.stderr.decode("utf-8", "replace").strip()))
    if not check and p.returncode != 0:
        return None
    return p.stdout.decode("utf-8", "replace")


def event_path(ev, digest):
    y, m, d = ev["ts"][0:4], ev["ts"][5:7], ev["ts"][8:10]
    return "events/%s/%s/%s/%012d-%s.json" % (y, m, d, ev["seq"], digest[:12])


class EventLog(object):
    def __init__(self, repo, log_dir, branch=const.DEFAULT_LOG_BRANCH, remote="origin",
                 project="project", commit_window_ms=None, snapshot_every=None,
                 push=True, push_retry_s=None):
        self.repo = os.path.abspath(repo)
        self.log_dir = os.path.abspath(log_dir)
        self.branch = branch
        self.remote = remote
        self.project = project
        self.window_s = (const.get("COMMIT_WINDOW_MS") if commit_window_ms is None else commit_window_ms) / 1000.0
        self.snapshot_every = const.get("SNAPSHOT_EVERY") if snapshot_every is None else snapshot_every
        self.push_enabled = push
        self.push_retry_s = const.get("PUSH_RETRY_S") if push_retry_s is None else push_retry_s

        self.events = []      # events[i] has seq i + 1
        self.hashes = []      # sha256 hex of the event file bytes
        self.idem = {}        # idem -> seq
        self.snapshots = []   # sorted seqs with a snapshot
        self.committed_seq = 0
        self.pushed_seq = 0
        self.push_error = None
        self.failed = None

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._pending = []    # [(seq or None, relpath)]
        self._pending_since = None
        self._stop = False
        self._push_wake = threading.Event()
        self._threads = []

    # ----- setup -----

    def open(self, start_threads=True):
        git(self.repo, "rev-parse", "--git-dir")
        self._ensure_worktree()
        git(self.log_dir, "reset", "-q", "--hard")
        git(self.log_dir, "clean", "-fdq")
        self._load()
        self.committed_seq = len(self.events)
        if start_threads:
            for target in (self._commit_loop, self._push_loop):
                t = threading.Thread(target=target, daemon=True)
                t.start()
                self._threads.append(t)
            self._push_wake.set()
        return self

    def has_remote(self):
        return git(self.repo, "remote", "get-url", self.remote, check=False) is not None

    def _branch_exists(self):
        return git(self.repo, "rev-parse", "--verify", "-q", "refs/heads/" + self.branch, check=False) is not None

    def _ensure_worktree(self):
        ok = False
        if os.path.isdir(self.log_dir):
            top = git(self.log_dir, "rev-parse", "--show-toplevel", check=False)
            head = git(self.log_dir, "symbolic-ref", "-q", "HEAD", check=False)
            ok = (top is not None and os.path.realpath(top.strip()) == os.path.realpath(self.log_dir)
                  and head is not None and head.strip() == "refs/heads/" + self.branch)
            if not ok and os.listdir(self.log_dir):
                raise LogError("%s exists and is not a worktree of %s" % (self.log_dir, self.branch))
        if not ok:
            git(self.repo, "worktree", "prune")
            if not self._branch_exists() and self.has_remote():
                git(self.repo, "fetch", "-q", self.remote,
                    "+refs/heads/%s:refs/heads/%s" % (self.branch, self.branch), check=False, timeout=30)
            if not self._branch_exists():
                self._create_orphan()
            os.makedirs(os.path.dirname(self.log_dir), exist_ok=True)
            if os.path.isdir(self.log_dir):
                os.rmdir(self.log_dir)
            git(self.repo, "worktree", "add", "-q", self.log_dir, self.branch)
        elif self.has_remote():
            # Catch up with a remote that is ahead (the server moved machines).
            if git(self.log_dir, "fetch", "-q", self.remote, self.branch, check=False, timeout=30) is not None:
                git(self.log_dir, "merge", "-q", "--ff-only", "FETCH_HEAD", check=False)

    def _create_orphan(self):
        meta = json.dumps({"schema": const.SCHEMA_VERSION, "project": self.project}, sort_keys=True) + "\n"
        blob = git(self.repo, "hash-object", "-w", "--stdin", input=meta.encode()).strip()
        tree = git(self.repo, "mktree", input=("100644 blob %s\tmeta.json\n" % blob).encode()).strip()
        commit = git(self.repo, "commit-tree", tree, "-m", "wavecoord: start %s" % self.branch).strip()
        git(self.repo, "update-ref", "refs/heads/" + self.branch, commit, "")

    def _load(self):
        found = {}
        root = os.path.join(self.log_dir, "events")
        for d, _dirs, files in os.walk(root):
            for fn in files:
                m = EVENT_RE.match(fn)
                if not m:
                    raise LogError("unexpected file in the log: %s" % os.path.join(d, fn))
                seq = int(m.group(1))
                if seq in found:
                    raise LogError("two files for seq %d" % seq)
                found[seq] = (os.path.join(d, fn), m.group(2))
        prev = None
        for seq in range(1, len(found) + 1):
            if seq not in found:
                raise LogError("gap in the log: seq %d is missing" % seq)
            path, h12 = found[seq]
            with open(path, "rb") as f:
                raw = f.read()
            digest = const.sha256_hex(raw)
            try:
                ev = json.loads(raw.decode("utf-8"))
            except ValueError:
                raise LogError("seq %d: not JSON" % seq)
            if digest[:12] != h12 or const.canonical(ev) != raw:
                raise LogError("seq %d: the file was edited (hash mismatch)" % seq)
            if ev.get("seq") != seq or ev.get("prev") != prev:
                raise LogError("seq %d: broken chain (prev does not match)" % seq)
            self.events.append(ev)
            self.hashes.append(digest)
            if ev.get("idem"):
                self.idem.setdefault(ev["idem"], seq)
            prev = digest
        sroot = os.path.join(self.log_dir, "snapshots")
        if os.path.isdir(sroot):
            self.snapshots = sorted(int(m.group(1)) for m in (SNAP_RE.match(f) for f in os.listdir(sroot)) if m)

    # ----- writing -----

    @property
    def last_seq(self):
        return len(self.events)

    @property
    def last_hash(self):
        return self.hashes[-1] if self.hashes else None

    def find_idem(self, idem):
        """The event logged with `idem`, or None."""
        with self._lock:
            seq = self.idem.get(idem) if idem else None
            return self.events[seq - 1] if seq else None

    def append(self, ev):
        """Validate, number and write one event. Return (event, created).

        `ev` needs ts, kind, actor, task, attempt, payload and idem; this sets
        schema, seq and prev. The event is durable only after wait(seq).
        """
        with self._lock:
            if self.failed:
                raise LogError("the log is not writable: %s" % self.failed)
            idem = ev.get("idem")
            if idem and idem in self.idem:
                return self.events[self.idem[idem] - 1], False
            full = dict(ev)
            full["schema"] = const.SCHEMA_VERSION
            full["seq"] = len(self.events) + 1
            full["prev"] = self.last_hash
            errs = schemaload.errors(full, "event")
            if errs:
                raise ValueError("invalid event: %s" % "; ".join(errs[:5]))
            raw = const.canonical(full)
            digest = const.sha256_hex(raw)
            rel = event_path(full, digest)
            self._write(rel, raw)
            self.events.append(full)
            self.hashes.append(digest)
            if idem:
                self.idem[idem] = full["seq"]
            self._queue(full["seq"], rel)
            return full, True

    def add_snapshot(self, seq, state):
        """Write snapshots/<seq>.json in the same group commit as event <seq>."""
        rel = "snapshots/%012d.json" % seq
        with self._lock:
            self._write(rel, (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
            self.snapshots.append(seq)
            self._queue(None, rel)

    def _write(self, rel, data):
        path = os.path.join(self.log_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _queue(self, seq, rel):
        if not self._pending:
            self._pending_since = time.time()
        self._pending.append((seq, rel))
        self._cv.notify_all()

    def wait(self, seq, timeout=60.0):
        """Block until event `seq` is committed. Raise LogError on a failed commit."""
        deadline = time.time() + timeout
        with self._lock:
            if not self._threads and self.committed_seq < seq:
                self._commit_batch_locked()
            while self.committed_seq < seq:
                if self.failed:
                    raise LogError("commit failed: %s" % self.failed)
                left = deadline - time.time()
                if left <= 0:
                    raise LogError("timed out waiting for the commit of seq %d" % seq)
                self._cv.wait(left)

    def flush(self):
        """Commit everything that is queued now."""
        with self._lock:
            if not self._pending:
                return
            target = max([s for s, _ in self._pending if s] or [self.committed_seq])
            self._pending_since = 0  # commit without waiting for the window
            self._cv.notify_all()
        if self._threads:
            self.wait(target)
        else:
            with self._lock:
                self._commit_batch_locked()

    def _commit_loop(self):
        with self._lock:
            while not self._stop:
                if not self._pending:
                    self._cv.wait(1.0)
                    continue
                left = self._pending_since + self.window_s - time.time()
                if left > 0:
                    self._cv.wait(left)
                    continue
                self._commit_batch_locked()

    def _commit_batch_locked(self):
        """Commit the queued files. Called with the lock held; git runs without it."""
        batch, self._pending = self._pending, []
        if not batch:
            return
        seqs = [s for s, _ in batch if s]
        top = max(seqs) if seqs else self.committed_seq
        self._lock.release()
        err = None
        try:
            paths = "".join(rel + "\0" for _, rel in batch).encode("utf-8")
            git(self.log_dir, "add", "--pathspec-from-file=-", "--pathspec-file-nul", input=paths)
            msg = "wavecoord: events %d..%d" % (min(seqs), top) if seqs else "wavecoord: snapshot"
            git(self.log_dir, "commit", "-q", "--no-verify", "-m", msg)
        except LogError as e:
            err = str(e)
        finally:
            self._lock.acquire()
        if err:
            self.failed = err
        else:
            self.committed_seq = max(self.committed_seq, top)
            self._push_wake.set()
        self._cv.notify_all()

    def _push_loop(self):
        while not self._stop:
            self._push_wake.wait(self.push_retry_s)
            self._push_wake.clear()
            if self._stop:
                return
            self.push_once()

    def push_once(self):
        """Push the branch to the remote, fast-forward only. Return True on success."""
        if not self.push_enabled or not self.has_remote():
            return False
        with self._lock:
            target = self.committed_seq
        if target <= self.pushed_seq:
            return True
        out = git(self.log_dir, "push", "-q", "--no-verify", self.remote,
                  "refs/heads/%s:refs/heads/%s" % (self.branch, self.branch), check=False, timeout=120)
        if out is None:
            self.push_error = "push to %s failed; retry in %ss" % (self.remote, self.push_retry_s)
            return False
        self.pushed_seq = max(self.pushed_seq, target)
        self.push_error = None
        return True

    def close(self):
        """Commit what is queued and stop the threads."""
        try:
            self.flush()
        except LogError:
            pass
        with self._lock:
            self._stop = True
            self._cv.notify_all()
        self._push_wake.set()
        for t in self._threads:
            t.join(5)
        self._threads = []

    # ----- reading -----

    def head(self):
        return git(self.log_dir, "rev-parse", "HEAD").strip()

    def resolve(self, rev):
        """A full commit sha on the branch for `rev`, or LogError."""
        sha = git(self.log_dir, "rev-parse", "--verify", "-q", rev + "^{commit}", check=False)
        if not sha:
            raise LogError("unknown commit %s" % rev)
        sha = sha.strip()
        if git(self.log_dir, "merge-base", "--is-ancestor", sha, "HEAD", check=False) is None:
            raise LogError("%s is not on %s" % (rev, self.branch))
        return sha

    def last_seq_at(self, rev):
        """The last event seq in commit `rev` of the branch."""
        sha = self.resolve(rev)
        names = git(self.log_dir, "ls-tree", "-r", "--name-only", sha, "--", "events").split()
        top = 0
        for n in names:
            m = EVENT_RE.match(os.path.basename(n))
            if not m:
                continue
            seq = int(m.group(1))
            if seq > len(self.events) or self.hashes[seq - 1][:12] != m.group(2):
                raise LogError("%s: seq %d does not match the current log" % (rev, seq))
            top = max(top, seq)
        return top

    def replay(self, upto_sha=None):
        """Iterate the events in seq order, up to the last event of commit `upto_sha`."""
        top = self.last_seq if upto_sha is None else self.last_seq_at(upto_sha)
        for ev in self.events[:top]:
            yield ev

    def commits(self):
        """[(sha, last_seq)] of the branch, oldest first (one git call)."""
        out = git(self.log_dir, "log", "--reverse", "--format=commit %H", "--name-only", "HEAD")
        res, top = [], 0
        for line in out.splitlines():
            if line.startswith("commit "):
                res.append([line[7:].strip(), top])
                continue
            m = EVENT_RE.match(os.path.basename(line.strip()))
            if m and line.startswith("events/"):
                top = max(top, int(m.group(1)))
                res[-1][1] = top
        return [tuple(x) for x in res]

    def snapshot_before(self, seq):
        """(snap_seq, state) of the newest snapshot at or before `seq`, or (0, None)."""
        cands = [s for s in self.snapshots if s <= seq]
        if not cands:
            return 0, None
        s = max(cands)
        with open(os.path.join(self.log_dir, "snapshots", "%012d.json" % s), encoding="utf-8") as f:
            return s, json.load(f)


def read_branch(repo, branch=const.DEFAULT_LOG_BRANCH, rev=None):
    """Read and verify the events of `branch` (or commit `rev`) without a worktree.

    Safe while the server runs: it reads committed git objects only.
    """
    ref = rev or "refs/heads/" + branch
    if git(repo, "rev-parse", "--verify", "-q", ref + "^{commit}", check=False) is None:
        raise LogError("no log branch %s in %s" % (ref, repo))
    names = [n for n in git(repo, "ls-tree", "-r", "--name-only", ref, "--", "events").split()
             if EVENT_RE.match(os.path.basename(n))]
    names.sort(key=lambda n: int(os.path.basename(n)[:12]))
    if not names:
        return []
    spec = "".join("%s:%s\n" % (ref, n) for n in names).encode("utf-8")
    env = dict(os.environ)
    p = subprocess.run(["git", "cat-file", "--batch"], cwd=repo, input=spec, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, env=env)
    if p.returncode != 0:
        raise LogError("git cat-file failed")
    out, pos, events, prev = p.stdout, 0, [], None
    for i, n in enumerate(names):
        nl = out.index(b"\n", pos)
        header = out[pos:nl].split()
        size = int(header[2])
        raw = out[nl + 1:nl + 1 + size]
        pos = nl + 1 + size + 1
        seq = i + 1
        digest = const.sha256_hex(raw)
        ev = json.loads(raw.decode("utf-8"))
        m = EVENT_RE.match(os.path.basename(n))
        if int(m.group(1)) != seq or digest[:12] != m.group(2) or ev.get("seq") != seq or ev.get("prev") != prev:
            raise LogError("broken chain at seq %d" % seq)
        events.append(ev)
        prev = digest
    return events
