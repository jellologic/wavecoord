"""A minimal REST client for the coordinator (docs/contract.md section 6), with a spool.

A write that cannot reach the coordinator is spooled to
`<wavecoord home>/spool/` and sent again later (`flush_spool`). A write's
idempotency key goes in the `Idempotency-Key` header (as in the hooks
client); a spooled write keeps it, so a repeat never logs twice.
"""

import json
import os
import urllib.error
import urllib.request

from .common import Unreachable, ensure_dir, now, rand4, read_json, wc_home, write_json

CLIENT_TIMEOUT_S = 3.0
SPOOL_MAX_AGE_S = 7 * 86400


def token_path():
    """Where this machine keeps its coordinator token (security.md, "Tokens")."""
    p = os.environ.get("WAVECOORD_TOKEN_FILE")
    if p:
        return os.path.expanduser(p)
    return os.path.join(wc_home(), "token")


def read_token():
    """The bearer token, or None. The plugin option wins, then the token file."""
    t = os.environ.get("CLAUDE_PLUGIN_OPTION_TOKEN", "").strip()
    if t:
        return t
    try:
        with open(token_path(), encoding="utf-8") as f:
            t = f.read().strip()
            return t or None
    except OSError:
        return None


class Coord(object):
    """One coordinator endpoint. `url` None means not configured: every call is a no-op."""

    def __init__(self, url, agent=None, token=None, timeout=None):
        self.url = url.rstrip("/") if url else None
        self.agent = agent
        self.token = token if token is not None else read_token()
        self.timeout = timeout or float(os.environ.get("WAVECOORD_CLIENT_TIMEOUT_S", CLIENT_TIMEOUT_S))

    @property
    def configured(self):
        return bool(self.url)

    def request(self, method, path, body=None, idem=None):
        """-> (http status, parsed JSON body or {}). Raises Unreachable on a network error."""
        if not self.url:
            raise Unreachable("coordinator not configured")
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        if self.agent:
            req.add_header("X-Wavecoord-Agent", self.agent)
        if idem:
            req.add_header("Idempotency-Key", idem)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                status = r.status
        except urllib.error.HTTPError as e:
            raw = e.read() if hasattr(e, "read") else b""
            status = e.code
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise Unreachable("coordinator at %s unreachable: %s" % (self.url, getattr(e, "reason", e)))
        try:
            out = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            out = {"raw": raw[:500].decode("utf-8", "replace")}
        return status, out if isinstance(out, dict) else {"body": out}

    def post_or_spool(self, path, body, idem=None):
        """POST; spool on a network error or a 5xx. -> 'sent', 'spooled', 'rejected:<code>' or 'off'."""
        if not self.url:
            return "off"
        try:
            status, out = self.request("POST", path, body, idem=idem)
        except Unreachable:
            spool(self, path, body, idem)
            return "spooled"
        if status >= 500 or status in (408, 429):
            spool(self, path, body, idem)
            return "spooled"
        if status >= 400:
            return "rejected:%s" % ((out.get("error") or {}).get("code") or status)
        return "sent"


def spool_dir():
    return ensure_dir(os.path.join(wc_home(), "spool"))


def spool(coord, path, body, idem=None):
    d = spool_dir()
    rec = {"url": coord.url, "agent": coord.agent, "path": path, "body": body, "idem": idem, "spooled": now()}
    name = "%d-%s.json" % (int(now() * 1000), rand4())
    write_json(os.path.join(d, name), rec)
    return name


def flush_spool(limit=200):
    """Send spooled writes again. -> {sent, kept, dropped}."""
    d = spool_dir()
    res = {"sent": 0, "kept": 0, "dropped": 0}
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
    except OSError:
        return res
    for n in names[:limit]:
        p = os.path.join(d, n)
        rec = read_json(p)
        if not isinstance(rec, dict) or not rec.get("url"):
            _drop(d, n)
            res["dropped"] += 1
            continue
        c = Coord(rec["url"], agent=rec.get("agent"))
        try:
            status, _ = c.request("POST", rec["path"], rec.get("body"), idem=rec.get("idem"))
        except Unreachable:
            if now() - float(rec.get("spooled") or 0) > SPOOL_MAX_AGE_S:
                _drop(d, n)
                res["dropped"] += 1
            else:
                res["kept"] += 1
            continue
        if status >= 500 or status in (408, 429):
            res["kept"] += 1
            continue
        if status >= 400:
            _drop(d, n)  # kept in spool/dead for a human to read
            res["dropped"] += 1
            continue
        try:
            os.unlink(p)
        except OSError:
            pass
        res["sent"] += 1
    return res


def _drop(d, n):
    dead = ensure_dir(os.path.join(d, "dead"))
    try:
        os.replace(os.path.join(d, n), os.path.join(dead, n))
    except OSError:
        pass


def spool_count():
    try:
        return len([n for n in os.listdir(spool_dir()) if n.endswith(".json")])
    except OSError:
        return 0
