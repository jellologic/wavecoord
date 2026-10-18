"""The HTTP front of the coordinator: REST, SSE and MCP on one port.

Checks, in order, for every request (docs/security.md):

1. the peer address is loopback or inside `allow_cidrs`, else 403;
2. the `Host` header is in the Host allowlist, else 421 (blocks DNS rebinding);
3. the body is at most REQ_MAX_BYTES, else 413;
4. a bearer token whose sha256 is in the tokens file, else 401
   (`GET /api/health` needs no token);
5. a `ui` write also needs an allowlisted `Origin` and `X-Wavecoord: 1`, else 403.

Write requests may carry `Idempotency-Key`; a repeat returns the first result
and logs nothing.
"""

import hashlib
import ipaddress
import json
import os
import re
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from wavecoord import const, mcp
from wavecoord.core import ApiError, Ctx

LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")


class Tokens(object):
    """The tokens file: sha256 digests only. Reloaded when the file changes."""

    def __init__(self, path=None, data=None):
        self.path = path
        self._data = data
        self._mtime = None

    def _load(self):
        if self.path is None:
            return self._data or {"tokens": {}}
        try:
            m = os.stat(self.path).st_mtime_ns
        except OSError:
            return {"tokens": {}}
        if m != self._mtime:
            with open(self.path, encoding="utf-8") as f:
                self._data = json.load(f)
            self._mtime = m
        return self._data

    def lookup(self, token):
        """(role, principal) for a token, or None."""
        if not token:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        ent = self._load().get("tokens", {}).get(digest)
        if not ent:
            return None
        return ent["role"], ent["principal"]


def _is_loopback_bind(bind):
    try:
        return ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return bind == "localhost"


class Server(object):
    def __init__(self, coord, bind="127.0.0.1", port=const.DEFAULT_PORT, tokens=None, allow_cidrs=None,
                 allow_hosts=None, allow_origins=None, verbose=False):
        if not _is_loopback_bind(bind) and not (allow_cidrs and allow_hosts):
            raise ValueError("binding %s needs allow_cidrs and allow_hosts in the server config" % bind)
        self.coord = coord
        self.bind = bind
        self.tokens = tokens if isinstance(tokens, Tokens) else Tokens(tokens)
        self.cidrs = [ipaddress.ip_network(c, strict=False) for c in (allow_cidrs or [])]
        self.allow_hosts_cfg = list(allow_hosts or [])
        self.allow_origins = set(allow_origins or [])
        self.verbose = verbose
        self.stopping = threading.Event()
        handler = type("Handler", (Handler,), {"app": self})
        cls = ThreadingHTTPServer
        if ":" in bind:
            cls = type("Server6", (ThreadingHTTPServer,), {"address_family": socket.AF_INET6})
        self.httpd = cls((bind, port), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        hosts = set(self.allow_hosts_cfg)
        for n in LOOPBACK_NAMES:
            hosts.update((n, "%s:%d" % (n, self.port)))
        self.allow_hosts = hosts
        self._thread = None

    @property
    def url(self):
        host = self.bind if ":" not in self.bind else "[%s]" % self.bind
        return "http://%s:%d" % (host, self.port)

    def start(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.2},
                                        daemon=True)
        self._thread.start()
        return self

    def serve_forever(self):
        self.httpd.serve_forever(poll_interval=0.5)

    def stop(self):
        self.stopping.set()
        with self.coord.ev_cv:
            self.coord.ev_cv.notify_all()
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(5)

    def peer_ok(self, addr):
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            return False
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        return ip.is_loopback or any(ip in n for n in self.cidrs if n.version == ip.version)


def _q1(q, name, default=None):
    v = q.get(name)
    return v[0] if v else default


# (method, path regex, handler(app, ctx, match, body, query, idem) -> response dict)
def _routes():
    c = lambda app: app.coord  # noqa: E731
    i = int
    return [
        ("GET", r"/api/live", lambda a, x, m, b, q, k: c(a).live(x)),
        ("POST", r"/api/hb", lambda a, x, m, b, q, k: c(a).hb(x, b)),
        ("POST", r"/api/agents/([^/]+)/finish", lambda a, x, m, b, q, k: c(a).finish(
            x, urllib.parse.unquote(m.group(1)), b, k)),
        ("POST", r"/api/leases", lambda a, x, m, b, q, k: c(a).claim(x, b, k)),
        ("GET", r"/api/leases", lambda a, x, m, b, q, k: c(a).leases(x, _q1(q, "all") == "1")),
        ("GET", r"/api/tasks/([^/]+)/lease", lambda a, x, m, b, q, k: c(a).task_lease(
            x, urllib.parse.unquote(m.group(1)))),
        ("POST", r"/api/leases/(\d+)/release", lambda a, x, m, b, q, k: c(a).release(x, i(m.group(1)), b, k)),
        ("POST", r"/api/reservations", lambda a, x, m, b, q, k: c(a).reserve(x, b, k)),
        ("DELETE", r"/api/reservations/(\d+)", lambda a, x, m, b, q, k: c(a).unreserve(x, i(m.group(1)), k)),
        ("GET", r"/api/conflicts", lambda a, x, m, b, q, k: c(a).conflicts(
            x, [g for g in (_q1(q, "globs") or "").split(",") if g] or None,
            i(_q1(q, "lease_id")) if (_q1(q, "lease_id") or "").isdigit() else None,
            _q1(q, "mode", "exclusive"))),
        ("POST", r"/api/check", lambda a, x, m, b, q, k: c(a).check(x, b)),
        ("POST", r"/api/messages", lambda a, x, m, b, q, k: c(a).send(x, b, k)),
        ("GET", r"/api/inbox", lambda a, x, m, b, q, k: c(a).inbox(x, _q1(q, "agent"))),
        ("POST", r"/api/messages/(\d+)/ack", lambda a, x, m, b, q, k: c(a).ack(x, i(m.group(1)), b, k)),
        ("POST", r"/api/notify", lambda a, x, m, b, q, k: c(a).notify(x, b, k)),
        ("GET", r"/api/proposals", lambda a, x, m, b, q, k: c(a).proposals(x)),
        ("POST", r"/api/proposals", lambda a, x, m, b, q, k: c(a).propose(x, b, k)),
        ("PATCH", r"/api/proposals/(\d+)", lambda a, x, m, b, q, k: c(a).decide(x, i(m.group(1)), b, k)),
        ("GET", r"/api/requests", lambda a, x, m, b, q, k: c(a).requests(x)),
        ("POST", r"/api/requests", lambda a, x, m, b, q, k: c(a).request(x, b, k)),
        ("GET", r"/api/attempts", lambda a, x, m, b, q, k: c(a).attempts(x, _q1(q, "task"), _q1(q, "state"))),
        ("GET", r"/api/attempts/([^/]+)/(\d+)", lambda a, x, m, b, q, k: c(a).attempt(
            x, urllib.parse.unquote(m.group(1)), i(m.group(2)))),
        ("POST", r"/api/attempts/([^/]+)/(\d+)/report", lambda a, x, m, b, q, k: c(a).report(
            x, urllib.parse.unquote(m.group(1)), i(m.group(2)), b, k)),
        ("GET", r"/api/config", lambda a, x, m, b, q, k: c(a).config(x)),
        ("POST", r"/api/revert", lambda a, x, m, b, q, k: c(a).revert(x, b, k)),
        ("POST", r"/api/rebuild", lambda a, x, m, b, q, k: c(a).rebuild(x)),
    ]


ROUTES = [(meth, re.compile("^" + rx + "$"), fn) for meth, rx, fn in _routes()]
SPECIAL = {"/api/health", "/api/events", "/mcp"}


class Handler(BaseHTTPRequestHandler):
    app = None
    server_version = "wavecoord"
    sys_version = ""

    def log_message(self, fmt, *args):
        if self.app.verbose:
            BaseHTTPRequestHandler.log_message(self, fmt, *args)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_PUT(self):
        self._handle("PUT")

    def do_OPTIONS(self):
        # CORS preflights are never approved, so no web page can write.
        self._handle("OPTIONS")

    # ----- responses -----

    def _send_json(self, status, obj, extra_headers=None):
        data = json.dumps(obj, sort_keys=True).encode("utf-8") if obj is not None else b""
        self.send_response(status)
        if obj is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def _error(self, e):
        body = {"now": self.app.coord.ts(), "error": {"code": e.code, "message": e.message}}
        body.update(e.extra)
        hdrs = {"WWW-Authenticate": "Bearer"} if e.status == 401 else None
        self._send_json(e.status, body, hdrs)

    # ----- the pipeline -----

    def _handle(self, method):
        try:
            self._pipeline(method)
        except ApiError as e:
            self._error(e)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # never leak a traceback to a client
            try:
                self._error(ApiError("unavailable", "internal error: %s" % type(e).__name__))
            except OSError:
                pass

    def _pipeline(self, method):
        app = self.app
        if not app.peer_ok(self.client_address[0]):
            raise ApiError("forbidden", "peer %s is not allowed" % self.client_address[0])
        host = self.headers.get("Host", "")
        if host not in app.allow_hosts:
            raise ApiError("misdirected", "Host %s is not allowed" % host)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError("bad_request", "bad Content-Length")
        if length > const.get("REQ_MAX_BYTES") or length < 0:
            raise ApiError("too_large", "the request body is larger than %d bytes" % const.get("REQ_MAX_BYTES"))
        raw = self.rfile.read(length) if length else b""
        url = urllib.parse.urlsplit(self.path)
        path, query = url.path.rstrip("/") or "/", urllib.parse.parse_qs(url.query)

        if path == "/api/health":
            if method != "GET":
                raise ApiError("method_not_allowed", "use GET")
            return self._send_json(200, app.coord.health())

        route, match, allowed = None, None, False
        if path not in SPECIAL:
            for meth, rx, fn in ROUTES:
                m = rx.match(path)
                if m:
                    allowed = True
                    if meth == method:
                        route, match = fn, m
                        break
            if not allowed:
                raise ApiError("not_found", "no such path %s" % path)

        ctx = self._auth()
        if path == "/mcp":
            if method != "POST":
                raise ApiError("method_not_allowed", "MCP uses POST; GET streams are not offered")
            self._ui_write_check(ctx)
            status, resp = mcp.handle(app.coord, ctx, self._json(raw))
            return self._send_json(status, resp)
        if path == "/api/events":
            if method != "GET":
                raise ApiError("method_not_allowed", "use GET")
            return self._sse(ctx, query)
        if route is None:
            raise ApiError("method_not_allowed", "%s is not allowed on %s" % (method, path))
        if method != "GET":
            self._ui_write_check(ctx)
        if ctx.role == "gate" and not (method == "GET" and (path == "/api/leases" or path.startswith("/api/tasks/"))):
            raise ApiError("forbidden", "the gate role may read leases and health only")
        body = self._json(raw) if method in ("POST", "PATCH", "PUT") else None
        idem = self.headers.get("Idempotency-Key")
        self._send_json(200, route(app, ctx, match, body, query, idem))

    def _json(self, raw):
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError("bad_request", "the body is not valid JSON")

    def _auth(self):
        h = self.headers.get("Authorization", "")
        token = h[7:].strip() if h.lower().startswith("bearer ") else ""
        found = self.app.tokens.lookup(token)
        if not found:
            raise ApiError("unauthorized", "a valid bearer token is required")
        role, principal = found
        agent = self.headers.get("X-Wavecoord-Agent") or None
        if agent is not None and (len(agent) > 128 or not agent.strip()):
            raise ApiError("bad_request", "bad X-Wavecoord-Agent")
        if role == "agent" and agent in ("lead", "ui"):
            raise ApiError("forbidden", "%s is a reserved id" % agent)
        if role in ("lead", "ui"):
            agent = role if agent is None or role == "ui" else agent
        return Ctx(role, principal, agent, self.headers.get("X-Wavecoord-Session") or None)

    def _ui_write_check(self, ctx):
        if ctx.role != "ui":
            return
        if self.headers.get("X-Wavecoord") != "1" or self.headers.get("Origin") not in self.app.allow_origins:
            raise ApiError("forbidden", "a ui write needs an allowed Origin and X-Wavecoord: 1")

    # ----- server-sent events -----

    def _sse(self, ctx, query):
        coord = self.app.coord
        if ctx.role not in ("agent", "lead", "ui"):
            raise ApiError("forbidden", "role %s may not read events" % ctx.role)
        last = self.headers.get("Last-Event-ID") or _q1(query, "since")
        try:
            pos = int(last) if last not in (None, "") else coord.log.committed_seq
        except ValueError:
            raise ApiError("bad_request", "bad Last-Event-ID or since")
        once = _q1(query, "once") == "1"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        ping = const.get("SSE_PING_S")
        quiet = 0.0
        while not self.app.stopping.is_set():
            evs = coord.events_after(pos)
            for ev in evs:
                data = json.dumps(ev, sort_keys=True, separators=(",", ":"))
                self.wfile.write(("id: %d\nevent: %s\ndata: %s\n\n" % (ev["seq"], ev["kind"], data)).encode("utf-8"))
                pos = ev["seq"]
            if evs:
                self.wfile.flush()
                quiet = 0.0
                continue
            if once:
                return
            step = min(1.0, ping)
            coord.wait_events(pos, step)
            quiet += step
            if quiet >= ping:
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                quiet = 0.0
