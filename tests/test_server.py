"""HTTP, SSE and MCP against a live server on an ephemeral port."""

import hashlib
import http.client
import json
import os
import threading
import time
import unittest

import _coord
from wavecoord import schemaload
from wavecoord.server import Server, Tokens

FIX = os.path.join(_coord._paths.FIXTURES, "api")
TOKENS = {"lead-secret": ("lead", "lead"), "ui-secret": ("ui", "ui"), "gate-secret": ("gate", "gate"),
          "h1-secret": ("agent", "host:build-1"), "h2-secret": ("agent", "host:build-2")}


def tokens_data():
    return {"version": 1, "tokens": {hashlib.sha256(t.encode()).hexdigest():
                                     {"principal": p, "role": r, "created": "2026-10-18T12:00:00Z"}
                                     for t, (r, p) in TOKENS.items()}}


def fixture(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


class ServerCase(unittest.TestCase):
    origins = ["http://ui.example"]

    def setUp(self):
        self.tr = _coord.TempRepo()
        self.clock = _coord.Clock()
        self.coord = _coord.make_coord(self.tr, self.clock, push=False).open()
        self.srv = Server(self.coord, port=0, tokens=Tokens(data=tokens_data()), allow_origins=self.origins).start()

    def tearDown(self):
        self.srv.stop()
        self.coord.close()
        self.tr.cleanup()

    def req(self, method, path, body=None, token="lead-secret", headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = "Bearer " + token
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host or "127.0.0.1:%d" % self.srv.port)
        for k, v in h.items():
            conn.putheader(k, v)
        if data is not None:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        return r.status, (json.loads(raw) if raw else None)

    def assertValid(self, body, ref):
        self.assertEqual(schemaload.errors(body, "api", "#/definitions/" + ref), [], body)


class RestTest(ServerCase):
    def test_health_needs_no_token(self):
        s, b = self.req("GET", "/api/health", token=None)
        self.assertEqual(s, 200)
        self.assertValid(b, "health")
        self.assertEqual(b["started"], "2026-10-18T12:00:00Z")

    def test_auth_errors(self):
        s, b = self.req("GET", "/api/live", token=None)
        self.assertEqual((s, b["error"]["code"]), (401, "unauthorized"))
        self.assertValid(b, "error_body")
        s, b = self.req("GET", "/api/live", token="wrong")
        self.assertEqual(s, 401)
        s, b = self.req("GET", "/api/live", host="evil.example:18741")
        self.assertEqual((s, b["error"]["code"]), (421, "misdirected"))
        self.assertEqual(b["error"]["message"], "Host evil.example:18741 is not allowed")
        self.assertValid(b, "error_body")
        self.srv.peer_ok = lambda addr: False
        s, b = self.req("GET", "/api/health", token=None)
        self.assertEqual((s, b["error"]["code"]), (403, "forbidden"))

    def test_peer_and_bind_rules(self):
        srv = Server.__new__(Server)
        import ipaddress
        srv.cidrs = [ipaddress.ip_network("192.0.2.0/24")]
        self.assertTrue(srv.peer_ok("127.0.0.1"))
        self.assertTrue(srv.peer_ok("::1"))
        self.assertTrue(srv.peer_ok("192.0.2.7"))
        self.assertTrue(srv.peer_ok("::ffff:192.0.2.7"))
        self.assertFalse(srv.peer_ok("198.51.100.1"))
        with self.assertRaises(ValueError):
            Server(self.coord, bind="0.0.0.0", port=0, tokens=Tokens(data=tokens_data()))

    def test_size_limit_and_bad_json(self):
        s, b = self.req("POST", "/api/messages", {"to": {"kind": "lead"}, "body": "x" * 70000})
        self.assertEqual((s, b["error"]["code"]), (413, "too_large"))
        s, b = self.req("POST", "/api/messages", {"to": {"kind": "lead"}, "body": "y" * 4097})
        self.assertEqual(s, 413)
        s, b = self.req("POST", "/api/messages", {"to": {"kind": "nobody"}, "body": "x"})
        self.assertEqual(s, 400)
        s, b = self.req("GET", "/api/nope")
        self.assertEqual(s, 404)
        s, b = self.req("DELETE", "/api/live")
        self.assertEqual(s, 405)
        s, b = self.req("GET", "/mcp")
        self.assertEqual(s, 405)

    def test_claim_hb_finish_flow(self):
        claim = fixture("claim-request.json")
        hdr = {"X-Wavecoord-Agent": claim["holder"]}
        s, b = self.req("POST", "/api/leases", claim, token="h2-secret", headers=hdr)
        self.assertEqual((s, b["error"]["code"]), (403, "forbidden"))  # wrong host token
        s, b = self.req("POST", "/api/leases", claim, token="h1-secret", headers=hdr)
        self.assertEqual(s, 200, b)
        self.assertValid(b, "claim_response")
        self.assertEqual(b["lease"]["epoch"], 1792324800000)
        lid = b["lease"]["id"]
        s, b2 = self.req("POST", "/api/leases", dict(claim, holder="w-other"), token="h1-secret")
        self.assertEqual((s, b2["error"]["code"]), (409, "held"))
        self.assertValid(b2, "error_body")
        hbody = fixture("hb-request.json")
        self.clock.advance(30)
        s, h = self.req("POST", "/api/hb", hbody, token="h1-secret", headers=hdr)
        self.assertEqual(s, 200, h)
        self.assertValid(h, "hb_response")
        self.assertEqual(h["lease"]["expires_at"], "2026-10-18T12:10:30Z")
        s, h = self.req("POST", "/api/hb", hbody, token="h1-secret", headers=hdr)
        self.assertTrue(h["duplicate"])
        s, t = self.req("GET", "/api/tasks/api-auth/lease", token="gate-secret")
        self.assertEqual(s, 200)
        self.assertValid(t, "task_lease")
        s, _ = self.req("GET", "/api/live", token="gate-secret")
        self.assertEqual(s, 403)
        s, f = self.req("POST", "/api/agents/%s/finish" % claim["holder"], fixture("finish-request.json"),
                        token="h1-secret", headers=hdr)
        self.assertEqual(s, 200, f)
        self.assertValid(f, "finish_response")
        self.assertEqual((f["lease"]["id"], f["lease"]["state"]), (lid, "parked"))

    def test_check_matches_fixture(self):
        # Lease ids 1..4 and 6 are released fillers, so the ids match the fixtures (5 and 7).
        for i in range(1, 8):
            if i == 5:
                body = {"task": "api-docs", "attempt": 1, "holder": "worker-20261018-115500-cd34", "host": "build-2",
                        "reservations": [{"glob": "docs/**", "mode": "exclusive"}]}
                s, b = self.req("POST", "/api/leases", body)
            elif i == 7:
                s, b = self.req("POST", "/api/leases", fixture("claim-request.json"), token="h1-secret")
                self.assertEqual(b["conflicts"], fixture("claim-200.json")["conflicts"])
                self.assertEqual(b["lease"], fixture("claim-200.json")["lease"])
            else:
                s, b = self.req("POST", "/api/leases", {"task": "fill-%d" % i, "attempt": 1, "holder": "f%d" % i,
                                                       "host": "build-2"})
                self.req("POST", "/api/leases/%d/release" % b["lease"]["id"], {})
            self.assertEqual((s, b["lease"]["id"]), (200, i), b)
        self.clock.advance(300)
        hdr = {"X-Wavecoord-Agent": "worker-20261018-120000-ab12"}
        s, b = self.req("POST", "/api/check", fixture("check-request.json"), token="h1-secret", headers=hdr)
        self.assertEqual((s, b), (200, fixture("check-response.json")))
        # outside_lease and stale_epoch
        req = dict(fixture("check-request.json"), paths=["lib/x.py"], epoch=5)
        s, b = self.req("POST", "/api/check", req, token="h1-secret", headers=hdr)
        self.assertValid(b, "check_response")
        self.assertEqual(sorted(v["kind"] for v in b["violations"]), ["outside_lease", "stale_epoch"])
        s, b = self.req("POST", "/api/check", {"agent": None, "paths": ["docs/a b.md"], "source": "guard"})
        self.assertEqual([v["kind"] for v in b["violations"]], ["foreign_exclusive"])
        s, b = self.req("POST", "/api/check", {"agent": "x", "paths": []})
        self.assertEqual(s, 400)
        s, b = self.req("POST", "/api/reservations", fixture("reservation-request.json"), token="h1-secret",
                        headers=hdr)
        self.assertEqual((s, [r["glob"] for r in b["reservations"]]), (200, ["src/auth/session.py"]))
        s, b = self.req("POST", "/api/notify", fixture("notify-request.json"), token="h1-secret", headers=hdr)
        self.assertEqual((s, b["notice"]["kind"]), (200, "waiting_on_you"))
        s, b = self.req("GET", "/api/conflicts?globs=docs/**,lib/**")
        self.assertEqual([c["other_glob"] for c in b["conflicts"]], ["docs/**", "docs/api.md"])

    def test_idempotency_header(self):
        m = fixture("message-request.json")
        s1, b1 = self.req("POST", "/api/messages", m, headers={"Idempotency-Key": "msg-1"})
        n = self.coord.log.last_seq
        s2, b2 = self.req("POST", "/api/messages", m, headers={"Idempotency-Key": "msg-1"})
        self.assertEqual((s1, s2), (200, 200))
        self.assertEqual(b1["message"], b2["message"])
        self.assertEqual(self.coord.log.last_seq, n)

    def test_messages_inbox_roles(self):
        s, b = self.req("POST", "/api/messages", fixture("message-request.json"), token="ui-secret")
        self.assertEqual(s, 403)  # no Origin / X-Wavecoord
        s, b = self.req("POST", "/api/messages", fixture("message-request.json"), token="ui-secret",
                        headers={"Origin": "http://ui.example", "X-Wavecoord": "1"})
        self.assertEqual(s, 200, b)
        self.assertEqual(b["message"]["sender"], "ui")
        s, b = self.req("POST", "/api/messages", {"to": {"kind": "lead"}, "body": "x"}, token="h1-secret")
        self.assertEqual(s, 400)  # an agent token must name its agent
        s, b = self.req("POST", "/api/messages", {"to": {"kind": "lead"}, "body": "from w1"}, token="h1-secret",
                        headers={"X-Wavecoord-Agent": "w1"})
        self.assertEqual(s, 200)
        s, ib = self.req("GET", "/api/inbox?agent=lead")
        self.assertValid(ib, "inbox")
        self.assertEqual([m["body"] for m in ib["messages"]], ["from w1"])
        s, _ = self.req("POST", "/api/messages/%d/ack" % ib["messages"][0]["id"], {})
        self.assertEqual(s, 200)
        s, ib = self.req("GET", "/api/inbox?agent=lead")
        self.assertEqual(ib["messages"], [])
        s, _ = self.req("POST", "/api/revert", {"seq": 1}, token="h1-secret", headers={"X-Wavecoord-Agent": "w1"})
        self.assertEqual(s, 403)

    def test_misc_endpoints(self):
        s, b = self.req("GET", "/api/config")
        self.assertEqual((s, b["config"]["log_branch"]), (200, "coord/log"))
        s, b = self.req("POST", "/api/notify", {"kind": "ci", "agent": "lead", "text": "CI is red"})
        self.assertEqual(s, 200)
        s, b = self.req("POST", "/api/proposals", {"summary": "split api-auth", "kind": "split"})
        pid = b["proposal"]["id"]
        s, b = self.req("PATCH", "/api/proposals/%d" % pid, {"decision": "accepted"})
        self.assertEqual(b["proposal"]["state"], "accepted")
        s, b = self.req("POST", "/api/requests", {"task": "api-auth"}, token="ui-secret",
                        headers={"Origin": "http://ui.example", "X-Wavecoord": "1"})
        self.assertEqual(s, 200, b)
        s, b = self.req("GET", "/api/requests")
        self.assertEqual(len(b["requests"]), 1)
        self.req("POST", "/api/leases", fixture("claim-request.json"))
        rep = {"status": "done", "summary": "ok", "commits": ["3f9a1c2"], "files": [], "checks_run": [],
               "acceptance_met": True, "questions": [], "proposals": [], "plan_feedback": "", "risks_hit": []}
        s, b = self.req("POST", "/api/attempts/api-auth/1/report",
                        {"report": rep, "cost_usd": 1.5, "cost_basis": "none", "provider": "zai", "throttled": True,
                         "throttle": ["api_retry:2"], "next_provider": "openrouter", "usage": {"input_tokens": 9}},
                        token="h1-secret")
        self.assertEqual((s, b["attempt"]["state"]), (200, "reported"))
        a = b["attempt"]
        self.assertEqual((a["provider"], a["cost_basis"], a["next_provider"], a["usage"]),
                         ("zai", "none", "openrouter", {"input_tokens": 9}))
        s, b = self.req("POST", "/api/attempts/api-auth/1/report", {"report": rep, "bogus_field": 1})
        self.assertEqual(s, 400)
        s, b = self.req("POST", "/api/attempts/api-auth/1/report", {"report": dict(rep, status="bogus")})
        self.assertEqual(s, 400)
        s, b = self.req("POST", "/api/rebuild", {})
        self.assertEqual((s, b["unchanged"]), (200, True))
        s, b = self.req("POST", "/api/revert", {"seq": 2})  # a notify.post
        self.assertEqual(s, 200, b)


class SseTest(ServerCase):
    def read_events(self, path, headers=None, n=None, timeout=10):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=timeout)
        h = {"Authorization": "Bearer lead-secret"}
        h.update(headers or {})
        conn.request("GET", path, headers=h)
        r = conn.getresponse()
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream")
        out, cur = [], {}
        while n is None or len(out) < n:
            line = r.fp.readline()
            if not line:
                break
            line = line.decode().rstrip("\n")
            if line == "":
                if cur:
                    out.append(cur)
                cur = {}
            elif not line.startswith(":"):
                k, v = line.split(": ", 1)
                cur[k] = v
        conn.close()
        return out

    def test_backlog_and_resume(self):
        for i in range(3):
            self.req("POST", "/api/notify", {"kind": "other", "agent": "lead", "text": "n%d" % i})
        evs = self.read_events("/api/events?since=0&once=1")
        self.assertEqual([int(e["id"]) for e in evs], list(range(1, self.coord.log.last_seq + 1)))
        self.assertEqual(evs[0]["event"], "server.start")
        self.assertEqual(json.loads(evs[-1]["data"])["payload"]["notice"]["text"], "n2")
        evs = self.read_events("/api/events?since=0&once=1", headers={"Last-Event-ID": str(self.coord.log.last_seq - 1)})
        self.assertEqual(len(evs), 1)

    def test_live_stream(self):
        got = []
        start = self.coord.log.last_seq
        t = threading.Thread(target=lambda: got.extend(self.read_events("/api/events", n=2)))
        t.start()
        time.sleep(0.3)
        self.req("POST", "/api/notify", {"kind": "other", "agent": "lead", "text": "a"})
        self.req("POST", "/api/notify", {"kind": "other", "agent": "lead", "text": "b"})
        t.join(10)
        self.assertEqual([int(e["id"]) for e in got], [start + 1, start + 2])
        self.assertEqual([e["event"] for e in got], ["notify.post", "notify.post"])

    def test_events_cli_printer(self):
        import io
        from wavecoord.corecli import follow_events
        self.req("POST", "/api/notify", {"kind": "other", "agent": "lead", "text": "printed"})
        buf = io.StringIO()
        self.assertEqual(follow_events(self.srv.url, since=0, follow=False, out=buf, token="lead-secret"), 0)
        lines = [json.loads(l) for l in buf.getvalue().splitlines()]
        self.assertEqual(lines[-1]["payload"]["notice"]["text"], "printed")


class McpTest(ServerCase):
    def rpc(self, msg, token="lead-secret", headers=None):
        return self.req("POST", "/mcp", msg, token=token, headers=headers)

    def test_initialize_matches_fixture(self):
        fx = fixture("mcp-initialize.json")
        s, b = self.rpc(fx["request"])
        from wavecoord import __version__
        want = json.loads(json.dumps(fx["response"]))
        want["result"]["serverInfo"]["version"] = __version__  # the fixture is an example of an older release
        self.assertEqual((s, b), (200, want))
        s, b = self.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((s, b), (202, None))
        s, b = self.rpc({"jsonrpc": "2.0", "id": 9, "method": "ping"})
        self.assertEqual(b["result"], {})
        s, b = self.rpc(fx["request"], token=None)
        self.assertEqual(s, 401)

    def test_tools_list(self):
        s, b = self.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in b["result"]["tools"]]
        for n in ("plan_ready", "plan_get", "plan_graph", "plan_edit", "dispatch", "dispatch_wave", "attempts",
                  "attempt_get", "inbox", "ack", "send", "conflicts", "resolve", "refinery_status",
                  "refinery_merge", "propose", "config"):
            self.assertIn(n, names)
        for t in b["result"]["tools"]:
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_tools_call_inbox_fixture(self):
        fx = fixture("mcp-tools-call-inbox.json")
        holder = "worker-20261018-120000-ab12"
        hdr = {"X-Wavecoord-Agent": holder}
        self.req("POST", "/api/leases", fixture("claim-request.json"), token="h1-secret", headers=hdr)
        for i in range(11):  # message ids 1..11 go elsewhere
            self.req("POST", "/api/messages", {"to": {"kind": "agent", "id": "someone"}, "body": "x"})
        self.clock.advance(20)
        s, b = self.req("POST", "/api/messages", fixture("message-request.json"))
        self.assertEqual(b["message"]["id"], 12)
        s, b = self.rpc(fx["request"], token="h1-secret", headers=hdr)
        self.assertEqual((s, b), (200, fx["response"]))
        s, b = self.rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                         "params": {"name": "ack", "arguments": {"id": 12}}}, token="h1-secret", headers=hdr)
        self.assertFalse(b["result"]["isError"])
        s, b = self.rpc(fx["request"], token="h1-secret", headers=hdr)
        self.assertEqual(b["result"]["content"][0]["text"], "No messages.")

    def test_tools_call_others(self):
        def call(name, args=None):
            s, b = self.rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                             "params": {"name": name, "arguments": args or {}}})
            self.assertEqual(s, 200)
            return b["result"]

        r = call("send", {"to": {"kind": "all"}, "body": "hi"})
        self.assertFalse(r["isError"], r)
        r = call("config")
        self.assertEqual(json.loads(r["content"][0]["text"])["branch_prefix"], "wave")
        r = call("propose", {"summary": "add a task", "kind": "new_task"})
        self.assertEqual(json.loads(r["content"][0]["text"])["state"], "open")
        self.assertEqual(json.loads(call("attempts")["content"][0]["text"]), [])
        self.assertTrue(call("attempt_get", {"task": "x", "n": 1})["isError"])
        self.assertIn("conflicts", json.loads(call("conflicts")["content"][0]["text"]))
        r = call("plan_ready")  # an empty plan: no tasks (the lead tools are in tests/test_lead*.py)
        self.assertFalse(r["isError"], r)
        self.assertEqual(json.loads(r["content"][0]["text"])["tasks"], [])
        r = call("dispatch", {"task": "x"})
        self.assertTrue(r["isError"])
        self.assertIn("no task x", r["content"][0]["text"])
        s, b = self.rpc({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "nope"}})
        self.assertEqual(b["error"]["code"], -32602)
        s, b = self.rpc({"jsonrpc": "2.0", "id": 7, "method": "nope"})
        self.assertEqual(b["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
