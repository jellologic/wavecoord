"""A fake coordinator for the hook and guard tests. Stdlib only.

It answers the REST routes the hooks use with the bodies in
tests/fixtures/api/ (or with bodies a test sets), and records every request.
"""

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

import _paths


def api_fixture(name):
    with open(os.path.join(_paths.FIXTURES, "api", name), encoding="utf-8") as f:
        return json.load(f)


NOW = "2026-10-18T12:00:30Z"


def default_routes():
    hb = api_fixture("hb-response.json")
    return {
        ("POST", r"/api/hb"): (200, hb),
        ("GET", r"/api/inbox\?agent=.*"): (200, api_fixture("inbox.json")),
        ("POST", r"/api/messages/\d+/ack"): (200, {"now": NOW}),
        ("GET", r"/api/live"): (200, {
            "now": NOW,
            "agents": [hb["agent"], dict(hb["agent"], id="worker-20261018-115500-cd34", task="api-docs")],
            "leases": [hb["lease"]],
            "reservations": api_fixture("claim-200.json")["reservations"] + [
                {"id": 9, "lease_id": 5, "task": "api-docs", "glob": "docs/**", "mode": "exclusive"},
                {"id": 10, "lease_id": 5, "task": "api-docs", "glob": "src/shared/util.py", "mode": "shared"},
            ],
            "server": {"last_seq": 41},
        }),
        ("GET", r"/api/plan/tasks/.*"): (200, {"now": NOW, "id": "api-auth", "title": "Token auth for the API"}),
        ("POST", r"/api/check"): (200, {"now": NOW, "violations": []}),
        ("POST", r"/api/reservations"): (200, {"now": NOW}),
        ("POST", r"/api/notify"): (200, {"now": NOW}),
        ("POST", r"/api/agents/.*/finish"): (200, api_fixture("finish-response.json")),
    }


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class FakeCoord(object):
    def __init__(self, port=0):
        self.routes = default_routes()
        self.requests = []
        self.delay = {}  # path regex -> seconds
        self.lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _handle(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    body = raw
                with fake.lock:
                    fake.requests.append({"method": method, "path": self.path, "body": body,
                                          "headers": {k.lower(): v for k, v in self.headers.items()}})
                for rx, s in fake.delay.items():
                    if re.fullmatch(rx, self.path):
                        time.sleep(s)
                status, resp = 404, {"now": NOW, "error": {"code": "not_found", "message": self.path}}
                for (m, rx), r in fake.routes.items():
                    if m == method and re.fullmatch(rx, self.path):
                        status, resp = r
                        break
                data = json.dumps(resp).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.httpd = _Server(("127.0.0.1", port), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05})
        self.thread.daemon = True

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    def find(self, method, rx):
        with self.lock:
            return [r for r in self.requests if r["method"] == method and re.fullmatch(rx, r["path"])]
