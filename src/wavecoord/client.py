"""A small REST client for the coordinator. Stdlib only, Python >= 3.9.

Used by the hooks and the git guards. It never raises on a network problem
to its caller's caller: `request` raises `Unreachable`, which the hooks
catch. Writes that cannot reach the server can go to a jsonl spool, and the
next call that reaches the server replays the spool.

    c = Client.from_env(agent="claude:abc", deadline=Deadline(5))
    status, body = c.request("POST", "/api/hb", {...})
"""

import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.request

CONFIG_REL_PATH = os.path.join(".claude", "wavecoord.json")
TOKEN_DEFAULT = os.path.join("~", ".wavecoord", "token")
SPOOL_NAME = "spool.jsonl"
SPOOL_MAX_LINES = 1000
SPOOL_REPLAY_MAX = 20

DEFAULTS = {
    "CLIENT_TIMEOUT_S": 3.0,
    "HOOK_MIN_INTERVAL_S": 10.0,
}


class Unreachable(Exception):
    """The coordinator did not answer (refused, timeout, DNS, no time left)."""


def const(name, env=None):
    """A contract constant, overridable by WAVECOORD_<NAME> (contract section 5)."""
    env = os.environ if env is None else env
    v = env.get("WAVECOORD_" + name)
    if v:
        try:
            return float(v)
        except ValueError:
            pass
    return DEFAULTS[name]


def find_repo_config(start):
    """Return the parsed .claude/wavecoord.json above `start`, or None."""
    d = os.path.abspath(start)
    while True:
        p = os.path.join(d, CONFIG_REL_PATH)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else None
            except (OSError, ValueError):
                return None
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def resolve_coord_url(env=None, cwd=None):
    """The coordinator URL, or None. WAVECOORD_URL, then the plugin option, then the repo config."""
    env = os.environ if env is None else env
    for key in ("WAVECOORD_URL", "CLAUDE_PLUGIN_OPTION_COORD_URL"):
        v = env.get(key, "").strip()
        if v:
            return v.rstrip("/")
    try:
        cfg = find_repo_config(cwd or os.getcwd())
    except OSError:
        cfg = None
    if cfg and isinstance(cfg.get("coord_url"), str) and cfg["coord_url"].strip():
        return cfg["coord_url"].strip().rstrip("/")
    return None


def resolve_token(env=None):
    """The bearer token: plugin option, then WAVECOORD_TOKEN_FILE, then ~/.wavecoord/token."""
    env = os.environ if env is None else env
    v = env.get("CLAUDE_PLUGIN_OPTION_TOKEN", "").strip()
    if v:
        return v
    for path in (env.get("WAVECOORD_TOKEN_FILE", "").strip(), os.path.expanduser(TOKEN_DEFAULT)):
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                t = f.read().strip()
            if t:
                return t
        except OSError:
            continue
    return None


def state_dir(env=None):
    """Directory for the hook state files and the spool."""
    env = os.environ if env is None else env
    d = env.get("WAVECOORD_STATE_DIR", "").strip()
    if not d:
        data = env.get("CLAUDE_PLUGIN_DATA", "").strip()
        d = os.path.join(data, "state") if data else os.path.join(os.path.expanduser("~"), ".wavecoord", "state")
    return d


class Deadline(object):
    """A wall-clock budget shared by all calls of one hook run."""

    def __init__(self, seconds):
        self.end = time.monotonic() + seconds

    def remaining(self):
        return self.end - time.monotonic()


def idem_key(*parts):
    """A stable idempotency key from its parts (contract section 2)."""
    raw = ":".join(str(p) for p in parts)
    if len(raw) <= 120:
        return raw
    return raw[:80] + ":" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class Client(object):
    def __init__(self, url, token=None, agent=None, deadline=None, spool_dir=None, timeout=None):
        self.url = url.rstrip("/")
        self.token = token
        self.agent = agent
        self.deadline = deadline or Deadline(3600)
        self.spool_dir = spool_dir
        self.timeout = timeout if timeout is not None else const("CLIENT_TIMEOUT_S")
        self.reached = False

    @classmethod
    def from_env(cls, agent=None, deadline=None, env=None, cwd=None):
        """A client, or None when no coordinator URL is configured."""
        env = os.environ if env is None else env
        url = resolve_coord_url(env, cwd)
        if not url:
            return None
        return cls(url, resolve_token(env), agent, deadline, state_dir(env), const("CLIENT_TIMEOUT_S", env))

    # -- transport --

    def _send(self, method, path, body, headers):
        t = min(self.timeout, self.deadline.remaining())
        if t < 0.05:
            raise Unreachable("no time left")
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in headers.items():
            if v:
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=t) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, socket.timeout, OSError, ValueError) as e:
            raise Unreachable(str(getattr(e, "reason", e)))
        self.reached = True
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            parsed = None
        return status, parsed

    def _headers(self, agent, idem):
        h = {"X-Wavecoord-Agent": agent or self.agent, "Idempotency-Key": idem}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        return h

    def request(self, method, path, body=None, idem=None, spool=False, agent=None):
        """Send one request. Returns (status, parsed JSON or None).

        Raises Unreachable when the server does not answer. With spool=True a
        write that does not reach the server is appended to the spool first.
        """
        try:
            return self._send(method, path, body, self._headers(agent, idem))
        except Unreachable:
            if spool and method != "GET":
                self.spool_append(method, path, body, idem, agent or self.agent)
            raise

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None, idem=None, spool=False):
        return self.request("POST", path, body, idem=idem, spool=spool)

    # -- spool --

    def _spool_path(self):
        return os.path.join(self.spool_dir, SPOOL_NAME) if self.spool_dir else None

    def spool_append(self, method, path, body, idem, agent):
        p = self._spool_path()
        if not p:
            return
        rec = {"ts": time.time(), "method": method, "path": path, "body": body, "idem": idem, "agent": agent}
        try:
            os.makedirs(self.spool_dir, exist_ok=True)
            line = json.dumps(rec, separators=(",", ":")) + "\n"
            # One small O_APPEND write: concurrent hooks do not interleave lines.
            fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
        except OSError:
            pass

    def spool_replay(self, limit=SPOOL_REPLAY_MAX):
        """Send spooled writes, oldest first. Returns the number sent.

        An entry leaves the spool when the server answers it (any status: a
        4xx will not get better with a retry). Replay stops at the first
        unreachable send, so the order of writes is kept.
        """
        p = self._spool_path()
        if not p or not os.path.exists(p):
            return 0
        work = p + ".%d.replay" % os.getpid()
        try:
            os.rename(p, work)  # claim the current spool; new writes go to a fresh file
        except OSError:
            return 0
        try:
            with open(work, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        except OSError:
            lines = []
        sent, keep = 0, []
        for i, ln in enumerate(lines):
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            if sent >= limit:
                keep.append(ln)
                continue
            try:
                self._send(rec.get("method", "POST"), rec["path"], rec.get("body"),
                           self._headers(rec.get("agent"), rec.get("idem")))
                sent += 1
            except (Unreachable, KeyError):
                keep.extend(lines[i:])
                break
        keep = keep[-SPOOL_MAX_LINES:]
        try:
            if keep:
                fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, ("\n".join(keep) + "\n").encode("utf-8"))
                finally:
                    os.close(fd)
            os.remove(work)
        except OSError:
            pass
        return sent
