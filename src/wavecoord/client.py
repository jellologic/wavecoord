"""A small REST client for the coordinator. Stdlib only, Python >= 3.9.

Used by the hooks and the git guards. It never raises on a network problem
to its caller's caller: `request` raises `Unreachable`, which the hooks
catch. Writes that cannot reach the server can go to a jsonl spool, and the
next call that reaches the server replays the spool.

    c = Client.from_env(agent="claude:abc", deadline=Deadline(5))
    status, body = c.request("POST", "/api/hb", {...})
"""

import contextlib
import hashlib
import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import fcntl
except ImportError:  # pragma: no cover - not POSIX: the spool works without locks
    fcntl = None

CONFIG_REL_PATH = os.path.join(".claude", "wavecoord.json")
TOKEN_DEFAULT = os.path.join("~", ".wavecoord", "token")
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


def _in_repo(d):
    """Is there a `.git` at or above folder `d`?"""
    while True:
        if os.path.lexists(os.path.join(d, ".git")):
            return True
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent


def find_repo_config(start):
    """Return the parsed .claude/wavecoord.json of the repo that holds `start`, or None. Only the file at the
    repo's top level counts (the innermost folder with a `.git`): a nested `pkg/.claude/wavecoord.json` is
    not the repo's config. Outside a repo, the nearest file above `start`."""
    d = os.path.abspath(start)
    while True:
        top = os.path.lexists(os.path.join(d, ".git"))
        p = os.path.join(d, CONFIG_REL_PATH)
        if os.path.isfile(p) and (top or not _in_repo(d)):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else None
            except (OSError, ValueError):
                return None
        if top:
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


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def url_trusted(url, env=None):
    """May a token from a user-level place (the plugin option, ~/.wavecoord/token) go to `url`?

    Yes for the URL the user set (WAVECOORD_URL, the plugin option coord_url) and for a loopback
    URL. A coord_url that only a repo's .claude/wavecoord.json names is not trusted: any checked-out
    repo or PR branch can set it, and the lead token must not go to its server (security.md).
    """
    env = os.environ if env is None else env
    if not url:
        return False
    u = url.strip().rstrip("/")
    for key in ("WAVECOORD_URL", "CLAUDE_PLUGIN_OPTION_COORD_URL"):
        if env.get(key, "").strip().rstrip("/") == u:
            return True
    try:
        host = urllib.parse.urlsplit(u).hostname
    except ValueError:
        return False
    return host in LOOPBACK_HOSTS


def spool_file(spool_dir, url):
    """The spool of one coordinator: writes spooled for one are never replayed into another."""
    return os.path.join(spool_dir, "spool-%s.jsonl" % hashlib.sha256(url.rstrip("/").encode("utf-8")).hexdigest()[:16])


@contextlib.contextmanager
def _locked(path, blocking=True):
    """An exclusive flock on `path` (created). Yields False when `blocking` is off and it is taken."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except OSError:
                yield False
                return
        yield True
    finally:
        os.close(fd)


def resolve_token(env=None):
    """The bearer token: plugin option, then WAVECOORD_TOKEN_FILE, then ~/.wavecoord/token.

    In a worker session (WAVECOORD_TASK or WAVECOORD_AGENT_ID set) only WAVECOORD_TOKEN_FILE
    counts: the launcher points it at the host's agent token. The plugin option and
    ~/.wavecoord/token can hold the lead's token, and a worker never uses it.
    """
    env = os.environ if env is None else env
    if env.get("WAVECOORD_TASK", "").strip() or env.get("WAVECOORD_AGENT_ID", "").strip():
        path = env.get("WAVECOORD_TOKEN_FILE", "").strip()
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as f:
                return f.read().strip() or None
        except (OSError, ValueError):
            return None
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
        token = resolve_token(env) if url_trusted(url, env) else None
        return cls(url, token, agent, deadline, state_dir(env), const("CLIENT_TIMEOUT_S", env))

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
            try:
                status, raw = e.code, e.read()
            except (http.client.HTTPException, OSError, ValueError) as e2:
                raise Unreachable("truncated error answer: %s" % e2)
        except (urllib.error.URLError, socket.timeout, OSError, ValueError, http.client.HTTPException) as e:
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
        return spool_file(self.spool_dir, self.url) if self.spool_dir else None

    def spool_append(self, method, path, body, idem, agent):
        p = self._spool_path()
        if not p:
            return
        rec = {"ts": time.time(), "url": self.url, "method": method, "path": path, "body": body, "idem": idem,
               "agent": agent}
        try:
            os.makedirs(self.spool_dir, exist_ok=True)
            line = json.dumps(rec, separators=(",", ":")) + "\n"
            with _locked(p + ".lock"):  # a replay rewrites the file under the same lock
                fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, line.encode("utf-8"))
                finally:
                    os.close(fd)
        except OSError:
            pass

    @staticmethod
    def _lines(p):
        try:
            with open(p, encoding="utf-8") as f:
                return [ln for ln in f.read().splitlines() if ln.strip()]
        except OSError:
            return []

    def spool_replay(self, limit=SPOOL_REPLAY_MAX):
        """Send spooled writes of this coordinator, oldest first. Returns the number answered.

        An entry leaves the spool when the server answers it with a final status (2xx, or a 4xx
        other than 408 and 429, which a retry will not change). An unreachable send, a 5xx, 408
        or 429 keeps it and stops the replay, so the order of writes is kept. One replay runs at
        a time per spool; writes spooled meanwhile stay behind the kept ones.
        """
        p = self._spool_path()
        if not p or not os.path.exists(p):
            return 0
        try:
            with _locked(p + ".replay", blocking=False) as mine:
                if not mine:
                    return 0  # another hook replays this spool now
                with _locked(p + ".lock"):
                    lines = self._lines(p)
                sent, keep = 0, []
                for i, ln in enumerate(lines):
                    try:
                        rec = json.loads(ln)
                    except ValueError:
                        continue
                    if sent >= limit:
                        keep.extend(lines[i:])
                        break
                    try:
                        status, _ = self._send(rec.get("method", "POST"), rec["path"], rec.get("body"),
                                               self._headers(rec.get("agent"), rec.get("idem")))
                    except (Unreachable, KeyError):
                        keep.extend(lines[i:])
                        break
                    if status >= 500 or status in (408, 429):
                        keep.extend(lines[i:])  # the server could not take it now: retry later, in order
                        break
                    sent += 1
                with _locked(p + ".lock"):
                    cur = self._lines(p)
                    rest = keep + cur[len(lines):]  # the kept entries first, then the ones spooled meanwhile
                    rest = rest[-SPOOL_MAX_LINES:]
                    if rest:
                        tmp = p + ".%d.tmp" % os.getpid()
                        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                        with os.fdopen(fd, "w", encoding="utf-8") as f:
                            f.write("\n".join(rest) + "\n")
                        os.replace(tmp, p)
                    else:
                        os.remove(p)
                return sent
        except OSError:
            return 0
