"""An agent token acts only within what the lead dispatched to it (second review: authority items).

Also the hardening of the HTTP front (timeouts, bounded threads, chunked bodies, NaN, MCP
arguments), the client spool and the coordinator identity checks.
"""

import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import _coord
from test_server import ServerCase, fixture
from wavecoord import const, feedback, jsonschema_lite, mcp, redact
from wavecoord.core import ApiError, Ctx, lead_ctx
from wavecoord.store import State

LEAD = lead_ctx()


def agent(host, aid):
    return Ctx("agent", "host:" + host, aid)


def claim(c, ctx, task, holder, host, globs=(), attempt=1, **kw):
    body = {"task": task, "attempt": attempt, "holder": holder, "host": host,
            "reservations": [{"glob": g, "mode": m} for g, m in globs]}
    body.update(kw)
    return c.claim(ctx, body)


from test_core import VALID_REPORT as REPORT  # noqa: E402


class CoreCase(unittest.TestCase):
    def setUp(self):
        self.tr = _coord.TempRepo()
        self.clock = _coord.Clock()
        self.c = _coord.make_coord(self.tr, self.clock, push=False).open()

    def tearDown(self):
        self.c.close()
        self.tr.cleanup()

    def code(self, fn, *a, **kw):
        try:
            fn(*a, **kw)
        except ApiError as e:
            return e.code
        return None


class ClaimAuthorityTest(CoreCase):
    """Items 98 and 91: an agent renews or resumes only the lease the lead granted, with its grants."""

    def test_agent_cannot_claim_an_undispatched_task(self):
        self.assertEqual(self.code(claim, self.c, agent("h1", "rogue"), "t2", "rogue", "h1", [("**", "exclusive")]),
                         "forbidden")
        self.assertIsNone(self.c.state.task_lease("t2"))

    def test_agent_renews_the_lease_the_lead_granted(self):
        l = claim(self.c, LEAD, "t1", "w1", "h1", [("src/**", "exclusive")])["lease"]
        r = claim(self.c, agent("h1", "w1"), "t1", "w1", "h1")
        self.assertEqual(r["lease"]["id"], l["id"])
        # another attempt of the task, or another holder: the lead's
        self.assertEqual(self.code(claim, self.c, agent("h1", "w1"), "t1", "w1", "h1", attempt=2), "forbidden")
        self.assertEqual(self.code(claim, self.c, agent("h1", "w9"), "t1", "w9", "h1"), "forbidden")

    def park(self, host="h1", holder="w1", globs=(("src/api/**", "exclusive"),)):
        l = claim(self.c, LEAD, "api", holder, host, list(globs))["lease"]
        self.c.finish(agent(host, holder), holder, {"state": "done", "pushed": True, "source": "launcher", "seq": 1})
        self.assertEqual(self.c.state.lease(l["id"])["state"], "parked")
        return l

    def test_resume_keeps_the_grants(self):
        self.park()
        wide = self.code(claim, self.c, agent("h1", "w1-x"), "api", "w1-x", "h1", [("**", "exclusive")], resume=True)
        self.assertEqual(wide, "forbidden")
        r = claim(self.c, agent("h1", "w1-x"), "api", "w1-x", "h1", resume=True)
        self.assertEqual([(x["glob"], x["mode"]) for x in r["reservations"]], [("src/api/**", "exclusive")])

    def test_resume_with_the_same_grants_is_allowed(self):
        self.park()
        r = claim(self.c, agent("h1", "w1-x"), "api", "w1-x", "h1", [("src/api/**", "exclusive")], resume=True)
        self.assertEqual(r["lease"]["holder"], "w1-x")

    def test_no_takeover_across_hosts(self):
        self.park(host="h1")
        self.assertEqual(self.code(claim, self.c, agent("h2", "w2"), "api", "w2", "h2", resume=True), "forbidden")

    def test_gate_lineage_cannot_be_widened(self):
        """Item 91: a takeover with '**' never makes outside_lease pass at E5."""
        l = self.park()
        self.code(claim, self.c, agent("h1", "w1-x"), "api", "w1-x", "h1", [("**", "exclusive")], resume=True)
        ck = self.c.check(LEAD, {"source": "gate", "agent": None, "paths": ["ci/deploy.sh", "src/api/x.py"],
                                 "task": "api", "attempt": 1, "epoch": l["epoch"]})
        self.assertEqual([v["path"] for v in ck["violations"] if v["kind"] == "outside_lease"], ["ci/deploy.sh"])


class HotMarkTest(CoreCase):
    """Item 94: an agent hot mark is one path, never a wildcard."""

    def test_wildcard_hot_mark_refused(self):
        l = claim(self.c, LEAD, "t1", "w1", "h1", [("src/**", "exclusive")])["lease"]
        a = agent("h1", "w1")
        for g in ("**", "web/*", "docs/?.md"):
            self.assertEqual(self.code(self.c.reserve, a, {"lease_id": l["id"], "glob": g, "mode": "shared",
                                                           "origin": "hot"}), "forbidden", g)
        r = self.c.reserve(a, {"lease_id": l["id"], "glob": "web/x[1].py", "mode": "shared", "origin": "hot"})
        self.assertEqual([x["glob"] for x in r["reservations"]], ["web/x[1].py"])


class ReportAuthorityTest(CoreCase):
    """Items 64, 82 and 23: reports of an agent token."""

    def setUp(self):
        CoreCase.setUp(self)
        for task, host, holder in (("t1", "h1", "w1"), ("t2", "h2", "w2")):
            claim(self.c, LEAD, task, holder, host)
            self.c.record(LEAD, "attempt.start", {"host": host, "holder": holder, "branch": "wave/%s-1" % task},
                          task, 1)

    def test_no_report_after_merge(self):
        self.c.report(agent("h1", "w1"), "t1", 1, {"report": REPORT})
        self.c.record(LEAD, "refinery.merge", {"pr": "1", "sha": "a" * 40}, "t1", 1)
        self.assertEqual(self.c.state.get("attempts", "t1/1")["state"], "merged")
        code = self.code(self.c.report, agent("h1", "w1"), "t1", 1, {"report": REPORT, "pushed": True,
                                                                     "pr_url": "https://x.invalid/pull/99"},
                         idem="fresh-key")
        self.assertEqual(code, "conflict")
        self.assertEqual(self.c.state.get("attempts", "t1/1")["state"], "merged")

    def test_merged_is_final_in_the_fold(self):
        st = State()
        a = {"ts": "2026-10-18T12:00:00Z", "task": "t", "attempt": 1, "actor": {}}
        st._on_refinery_merge(dict(a, kind="refinery.merge"), {"pr": "1", "sha": "b" * 40}, None)
        st._on_report_post(dict(a, kind="report.post"), {"report": REPORT}, None)
        self.assertEqual(st.get("attempts", "t/1")["state"], "merged")

    def test_agent_never_sets_pr_url(self):
        self.c.record(LEAD, "attempt.end", {"state": "reported", "pr_url": "https://x.invalid/pull/1"}, "t1", 1)
        self.c.report(agent("h1", "w1"), "t1", 1, {"report": REPORT, "pushed": True,
                                                   "pr_url": "https://x.invalid/pull/2"}, idem="k2")
        self.assertEqual(self.c.state.get("attempts", "t1/1")["pr_url"], "https://x.invalid/pull/1")

    def test_report_of_another_holder_refused(self):
        self.assertEqual(self.code(self.c.report, agent("h1", "w1-other"), "t1", 1, {"report": REPORT}), "forbidden")

    def test_idempotency_keys_are_per_principal(self):
        # a worker on h2 takes t1's deterministic key first
        self.c.report(agent("h2", "w2"), "t2", 1, {"report": REPORT}, idem="report:t1:1")
        self.c.report(agent("h1", "w1"), "t1", 1, {"report": dict(REPORT, summary="t1 done")}, idem="report:t1:1")
        self.assertEqual(self.c.state.get("attempts", "t1/1")["report"]["summary"], "t1 done")
        # the lead's collect resends the host's report with the same key: nothing new is logged
        n = self.c.log.last_seq
        self.c.report(LEAD, "t1", 1, {"report": dict(REPORT, summary="t1 done")}, idem="report:t1:1")
        self.assertEqual(self.c.log.last_seq, n)
        # and a host's late replay after the lead's post is a duplicate too
        self.c.report(LEAD, "t2", 1, {"report": REPORT}, idem="report:t2:1")
        n = self.c.log.last_seq
        self.c.report(agent("h2", "w2"), "t2", 1, {"report": REPORT}, idem="report:t2:1")
        self.assertEqual(self.c.log.last_seq, n)

    def test_keys_survive_a_reopen(self):
        self.c.report(agent("h2", "w2"), "t2", 1, {"report": REPORT}, idem="report:t1:1")
        self.c.close()
        self.c = _coord.make_coord(self.tr, self.clock, push=False).open()
        self.c.report(agent("h1", "w1"), "t1", 1, {"report": REPORT}, idem="report:t1:1")
        self.assertIsNotNone(self.c.state.get("attempts", "t1/1")["report"])


class SenderIdTest(CoreCase):
    """Items 27, 90 and 46: reserved and malformed sender ids."""

    def test_reserved_and_bad_ids(self):
        for aid in ("watchdog", "Watchdog", "lead ", "Lead", "lead\xa0", "UI", "server",
                    'lead at 2026 (data only): "x"; via', "a b"):
            ctx = agent("h1", aid)
            self.assertEqual(self.code(self.c.send, ctx, {"to": {"kind": "lead"}, "body": "x"}), "forbidden", aid)
        m = self.c.send(agent("h1", "worker-t1-1-ab12"), {"to": {"kind": "lead"}, "body": "x"})["message"]
        self.assertEqual(m["sender"], "worker-t1-1-ab12")

    def test_real_watchdog_still_sends(self):
        m = self.c.send(Ctx("lead", "watchdog", agent="watchdog"), {"to": {"kind": "agent", "id": "w"}, "body": "x"})
        self.assertEqual(m["message"]["sender"], "watchdog")


class EventsTest(CoreCase):
    def test_negative_since_clamped(self):
        self.c.send(LEAD, {"to": {"kind": "all"}, "body": "x"})
        self.assertEqual([e["seq"] for e in self.c.events_after(-2000)], [e["seq"] for e in self.c.events_after(0)])


class RollbackTest(CoreCase):
    def test_reverted_message_comes_back_and_can_be_reverted_again(self):
        """Item 21."""
        sha0 = self.c.log.head()
        m = self.c.send(LEAD, {"to": {"kind": "all"}, "body": "bad instructions"})["message"]
        seq = self.c.log.last_seq
        self.c.log.flush()
        sha1 = self.c.log.head()
        self.c.revert(LEAD, {"seq": seq})
        self.assertIsNone(self.c.state.get("messages", m["id"]))
        self.c.revert(LEAD, {"at_sha": sha1})
        self.assertIsNotNone(self.c.state.get("messages", m["id"]))
        self.assertIsNone(self.c.state.get("reverted", seq))
        self.c.revert(LEAD, {"seq": seq})  # not "already reverted"
        self.assertIsNone(self.c.state.get("messages", m["id"]))
        self.assertTrue(sha0)
        before = self.c.state.dump()
        self.assertTrue(self.c.rebuild(LEAD)["unchanged"], before)


class WatchdogKeyTest(CoreCase):
    def test_stale_again_after_a_reclaim(self):
        """Item 20: a lease revived without new activity is marked stale again, and logged."""
        from wavecoord.watchdog import Watchdog
        lid = claim(self.c, LEAD, "api", "w1", "h1")["lease"]["id"]
        wd = Watchdog(self.c, head=lambda b: None, interval_s=1, nudge_s=60, stale_s=30)
        self.clock.advance(100)
        wd.check()
        self.clock.advance(40)
        self.assertEqual(wd.check()["stale"], [lid])
        claim(self.c, agent("h1", "w1"), "api", "w1", "h1")  # the same holder re-claims: revived
        self.assertEqual(self.c.state.lease(lid)["state"], "active")
        self.clock.advance(100)
        wd.check()
        self.clock.advance(40)
        self.assertEqual(wd.check()["stale"], [lid])
        self.assertEqual(self.c.state.lease(lid)["state"], "stale")
        kinds = [e["kind"] for e in self.c.log.events]
        self.assertEqual(kinds.count("lease.stale"), 2)


class RecordRedactTest(CoreCase):
    def test_secrets_never_reach_the_log(self):
        """Item 84: command output in a lead record is redacted."""
        fake = "ghp_" + "FAKEFAKEFAKE0123456789abcd"
        with mock.patch.dict(os.environ, {"GH_TOKEN": "s3cr3t-value-from-env", "HOME": self.tr.root}):
            self.c.record(LEAD, "refinery.block", {"pr": "1", "reason": "check", "detail":
                                                    "exit 1: token=%s env=s3cr3t-value-from-env" % fake}, "t1", 1)
        raw = json.dumps(self.c.log.events[-1])
        self.assertNotIn(fake, raw)
        self.assertNotIn("s3cr3t-value-from-env", raw)
        self.assertIn("[redacted]", raw)

    def test_redact_formats(self):
        parts = ["github" + "_pat_" + "A" * 30, "glpat" + "-" + "b" * 24, "xox" + "b-" + "1" * 20,
                 "sk-" + "proj-" + "c" * 30, "AKIA" + "ABCDEFGHIJKLMNOP", "AIza" + "d" * 35,
                 "Authorization: Bearer " + "e" * 30]
        for p in parts:
            self.assertNotIn(p.split()[-1], redact.redact("x %s y" % p, values=[]), p)
        self.assertEqual(redact.redact("plain text", values=[]), "plain text")


class LogResilienceTest(unittest.TestCase):
    """Items 17 and 18."""

    def setUp(self):
        self.tr = _coord.TempRepo()

    def tearDown(self):
        self.tr.cleanup()

    def ev(self, i):
        return {"ts": "2026-10-18T12:00:00Z", "kind": "notify.post", "actor": {"agent": None, "host": "local",
                                                                             "session_id": None},
                "task": None, "attempt": None, "payload": {"i": i}, "idem": None}

    def test_transient_commit_failure_recovers(self):
        from wavecoord.log import EventLog, LogError, git
        log = EventLog(self.tr.repo, os.path.join(self.tr.home, "log"), commit_window_ms=0).open(start_threads=False)
        log.append(self.ev(1))
        log.wait(1)
        lock = git(log.log_dir, "rev-parse", "--git-path", "index.lock").strip()
        lock = lock if os.path.isabs(lock) else os.path.join(log.log_dir, lock)
        open(lock, "w").close()
        log.append(self.ev(2))
        with self.assertRaises(LogError):
            log.wait(2)
        self.assertIsNotNone(log.failed)
        os.remove(lock)
        e3, _ = log.append(self.ev(3))  # the retry commits the kept batch first
        log.wait(3)
        self.assertIsNone(log.failed)
        self.assertEqual(log.committed_seq, 3)
        log.close()

    def test_transient_commit_failure_recovers_with_threads(self):
        from wavecoord.log import EventLog, LogError, git
        log = EventLog(self.tr.repo, os.path.join(self.tr.home, "log"), commit_window_ms=0).open()
        try:
            log.append(self.ev(1))
            log.wait(1)
            lock = git(log.log_dir, "rev-parse", "--git-path", "index.lock").strip()
            lock = lock if os.path.isabs(lock) else os.path.join(log.log_dir, lock)
            open(lock, "w").close()
            log.append(self.ev(2))
            with self.assertRaises(LogError):
                log.wait(2)
            os.remove(lock)
            deadline = time.time() + 10
            while log.failed and time.time() < deadline:
                time.sleep(0.1)
            self.assertIsNone(log.failed)
            log.append(self.ev(3))
            log.wait(3)
        finally:
            log.close()

    def test_fork_refused_at_start_and_on_push(self):
        from wavecoord.log import EventLog, LogError, git
        a = EventLog(self.tr.repo, os.path.join(self.tr.home, "log"), commit_window_ms=0).open(start_threads=False)
        a.append(self.ev(1))
        a.wait(1)
        self.assertTrue(a.push_once())
        # a second clone of the same origin, with its own server
        clone = os.path.join(self.tr.root, "clone")
        _coord.git(self.tr.root, "clone", "-q", self.tr.remote, clone)
        b = EventLog(clone, os.path.join(self.tr.root, "home2", "log"), commit_window_ms=0).open(start_threads=False)
        self.assertEqual(b.last_seq, 1)
        a.append(self.ev(2))
        a.wait(2)
        b.append(self.ev(3))
        b.wait(2)
        self.assertTrue(a.push_once())
        self.assertFalse(b.push_once())
        self.assertIn("forked", b.forked or "")
        with self.assertRaises(LogError):
            b.append(self.ev(4))
        b.close()
        with self.assertRaises(LogError) as cm:
            EventLog(clone, os.path.join(self.tr.root, "home2", "log"), commit_window_ms=0).open(start_threads=False)
        self.assertIn("forked", str(cm.exception))
        a.close()
        self.assertTrue(git)


class HttpHardeningTest(ServerCase):
    def raw(self, data, read=True, timeout=10):
        s = socket.create_connection(("127.0.0.1", self.srv.port), timeout=timeout)
        s.sendall(data)
        out = b""
        if read:
            try:
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    out += chunk
            except socket.timeout:
                pass
        s.close()
        return out

    def test_chunked_refused(self):
        """Item 24."""
        body = b'{"pr":"12"}'
        req = (b"POST /api/refinery/merge HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer lead-secret\r\n"
               b"Transfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n%x\r\n%s\r\n0\r\n\r\n"
               % (self.srv.port, len(body), body))
        out = self.raw(req)
        self.assertTrue(out.startswith(b"HTTP/1.0 411") or out.startswith(b"HTTP/1.1 411"), out[:80])

    def test_negative_since_refused(self):
        """Item 26."""
        s, b = self.req("GET", "/api/events?since=-2000&once=1", token="h1-secret")
        self.assertEqual(s, 400)

    def test_nan_refused(self):
        """Item 30."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        data = b'{"kind": "other", "agent": "lead", "text": "x", "detail": {"v": NaN}}'
        conn.request("POST", "/api/notify", data, {"Host": "127.0.0.1:%d" % self.srv.port,
                                                   "Authorization": "Bearer lead-secret",
                                                   "Content-Type": "application/json"})
        r = conn.getresponse()
        self.assertEqual(r.status, 400, r.read())
        conn.close()
        self.assertTrue(jsonschema_lite.validate(float("nan"), {"type": "number"}))
        self.assertTrue(jsonschema_lite.validate(float("inf"), {"type": "number"}))

    def test_non_object_bodies_are_400(self):
        """Item 32."""
        for path in ("/api/dispatch", "/api/plan/edit", "/api/revert", "/api/messages/1/ack", "/api/collect"):
            s, b = self.req("POST", path, [1, 2])
            self.assertEqual(s, 400, (path, b))

    def test_reserved_agent_header(self):
        for aid in ("watchdog", "Lead", "lead ", "lead\xa0"):
            s, b = self.req("POST", "/api/messages", {"to": {"kind": "lead"}, "body": "x"}, token="h1-secret",
                            headers={"X-Wavecoord-Agent": aid.encode("latin-1").decode("latin-1")})
            self.assertEqual(s, 403, (aid, b))

    def test_idle_connection_times_out(self):
        """Item 25: a client that never sends its body is dropped."""
        h = self.srv.httpd.RequestHandlerClass
        self.assertEqual(h.timeout, const.get("REQ_TIMEOUT_S"))
        old = h.timeout
        h.timeout = 0.5
        try:
            t0 = time.time()
            out = self.raw(b"POST /api/messages HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: 10\r\n\r\n"
                           % self.srv.port, timeout=10)
            self.assertLess(time.time() - t0, 5)
            self.assertNotIn(b"200 OK", out)
        finally:
            h.timeout = old

    def test_connection_cap(self):
        """Item 25: connections over MAX_CONNS get a 503 and a close, and the server lives on."""
        slots = self.srv.httpd._slots
        held = []
        while slots.acquire(blocking=False):
            held.append(1)
        try:
            s = socket.create_connection(("127.0.0.1", self.srv.port), timeout=10)
            out = s.recv(200)  # answered at accept, before any request is read
            s.close()
            self.assertTrue(out.startswith(b"HTTP/1.1 503"), out[:60])
        finally:
            for _ in held:
                slots.release()
        s, b = self.req("GET", "/api/health")
        self.assertEqual(s, 200)

    def test_health_proof(self):
        """Item 85."""
        from wavecoord.launcher import coord as coordmod
        s, b = self.req("GET", "/api/health?challenge=%s&host=build-1" % ("ab" * 16), token=None)
        self.assertEqual((s, len(b["proofs"])), (200, 1))
        url = "http://127.0.0.1:%d" % self.srv.port
        self.assertEqual(coordmod.verify_coordinator(url, "build-1", "h1-secret"), (True, True))
        self.assertEqual(coordmod.verify_coordinator(url, "build-1", "h2-secret"), (True, False))
        s, b = self.req("GET", "/api/health?challenge=zz&host=build-1", token=None)
        self.assertEqual(s, 400)

    def test_squatter_fails_the_proof(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from wavecoord.launcher import coord as coordmod
        seen = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                data = json.dumps({"ok": True, "proofs": ["0" * 64]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        httpd = HTTPServer(("127.0.0.1", 0), H)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            url = "http://127.0.0.1:%d" % httpd.server_address[1]
            self.assertEqual(coordmod.verify_coordinator(url, "build-1", "h1-secret"), (True, False))
            self.assertEqual(seen, [None])  # the token never went to it
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_wildcard_bind_worker_url(self):
        """Item 29."""
        from wavecoord.server import Server, Tokens
        srv = Server.__new__(Server)
        srv.bind, srv.port, srv.allow_hosts_cfg = "0.0.0.0", 18799, ["coord.example.internal:18741"]
        self.assertEqual(srv.worker_url(), "http://coord.example.internal:18741")
        srv.allow_hosts_cfg = ["coord.example.internal"]
        self.assertEqual(srv.worker_url(), "http://coord.example.internal:18799")
        srv.allow_hosts_cfg = []
        self.assertEqual(srv.worker_url(), "http://127.0.0.1:18799")
        srv.bind = "127.0.0.1"
        self.assertEqual(srv.worker_url(), "http://127.0.0.1:18799")
        self.assertTrue(Tokens)

    def test_mcp_arguments_validated(self):
        """Item 31."""
        claim(self.coord, LEAD, "t1", "w1", "build-1", [("src/**", "exclusive")])
        s, b = self.rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                         "params": {"name": "conflicts", "arguments": {"globs": "src"}}})
        self.assertTrue(b["result"]["isError"])
        self.assertIn("bad_request", b["result"]["content"][0]["text"])
        s, b = self.rpc({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                         "params": {"name": "conflicts", "arguments": {"globs": ["src/x.py"]}}})
        self.assertFalse(b["result"]["isError"])
        self.assertEqual(len(json.loads(b["result"]["content"][0]["text"])["conflicts"]), 1)
        s, b = self.rpc({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                         "params": {"name": "attempts", "arguments": {"task": "t1", "bogus": 1}}})
        self.assertTrue(b["result"]["isError"])

    def rpc(self, msg, token="lead-secret", headers=None):
        return self.req("POST", "/mcp", msg, token=token, headers=headers)


class FeedbackLineTest(unittest.TestCase):
    def test_trailing_newline_is_quoted(self):
        """Item 97."""
        ev = {"seq": 7, "ts": "T", "kind": "lease.release", "task": "api", "attempt": 1,
              "actor": {"agent": "w1"}, "payload": {"lease_id": 3, "by": "w1", "reason": "merged\n"}}
        line = feedback.lead_line(ev)
        self.assertNotIn("\n", line)
        self.assertTrue(line.endswith(feedback.DATA_ONLY))
        self.assertTrue(jsonschema_lite.validate("abc\n", {"type": "string", "pattern": "^[a-z]+$"}))
        self.assertEqual(jsonschema_lite.validate("abc", {"type": "string", "pattern": "^[a-z]+$"}), [])


class SpoolTest(unittest.TestCase):
    """Items 19, 22 and 93: the hooks spool."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def client(self, url, answers):
        from wavecoord.client import Client, Unreachable
        c = Client(url, token="t", spool_dir=self.dir)
        sent = []

        def send(method, path, body, headers):
            a = answers.pop(0) if answers else 200
            if a is None:
                raise Unreachable("down")
            sent.append(path)
            if callable(a):
                a()
                return 200, {}
            return a, {}
        c._send = send
        c.sent = sent
        return c

    def paths(self, url):
        from wavecoord.client import spool_file
        try:
            with open(spool_file(self.dir, url)) as f:
                return [json.loads(x)["path"] for x in f if x.strip()]
        except OSError:
            return []

    def test_5xx_keeps_the_entry(self):
        c = self.client("http://a", [None])
        c.spool_append("POST", "/x/finish", {}, "k", "w")
        c2 = self.client("http://a", [503])
        self.assertEqual(c2.spool_replay(), 0)
        self.assertEqual(self.paths("http://a"), ["/x/finish"])
        c3 = self.client("http://a", [400])
        self.assertEqual(c3.spool_replay(), 1)
        self.assertEqual(self.paths("http://a"), [])

    def test_order_kept_with_writes_during_replay(self):
        c = self.client("http://a", [])
        for p in ("/x/A", "/x/B-ack", "/x/C-finish"):
            c.spool_append("POST", p, {}, p, "w")
        new = self.client("http://a", [])
        r = self.client("http://a", [lambda: new.spool_append("POST", "/x/D-new", {}, "d", "w"), None])
        r.spool_replay()
        self.assertEqual(self.paths("http://a"), ["/x/B-ack", "/x/C-finish", "/x/D-new"])

    def test_spool_is_per_coordinator(self):
        c = self.client("http://127.0.0.1:1", [])
        c.spool_append("POST", "/api/messages/5/ack", {}, "k", "lead")
        other = self.client("http://127.0.0.1:2", [])
        self.assertEqual(other.spool_replay(), 0)
        self.assertEqual(other.sent, [])
        same = self.client("http://127.0.0.1:1", [])
        self.assertEqual(same.spool_replay(), 1)


class TokenTrustTest(unittest.TestCase):
    """Item 83: the lead token goes only to a coordinator the user configured, or loopback."""

    def test_repo_url_gets_no_user_token(self):
        from wavecoord.client import Client, url_trusted
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, ".claude"))
            with open(os.path.join(d, ".claude", "wavecoord.json"), "w") as f:
                json.dump({"coord_url": "http://coord.example.invalid:9"}, f)
            env = {"CLAUDE_PLUGIN_OPTION_TOKEN": "LEAD-TOKEN", "HOME": d}
            c = Client.from_env(env=env, cwd=d)
            self.assertEqual(c.url, "http://coord.example.invalid:9")
            self.assertIsNone(c.token)
            env["CLAUDE_PLUGIN_OPTION_COORD_URL"] = "http://coord.example.invalid:9"
            self.assertEqual(Client.from_env(env=env, cwd=d).token, "LEAD-TOKEN")
            self.assertTrue(url_trusted("http://127.0.0.1:18741", {}))
            self.assertTrue(url_trusted("http://[::1]:18741", {}))
            self.assertFalse(url_trusted("http://127.0.0.1.example.invalid:1", {}))
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class TokenFileModeTest(unittest.TestCase):
    """Items 28 and 88: tokens provision --out writes mode 600 over a looser file."""

    def test_out_over_existing_0644(self):
        from wavecoord import corecli
        d = tempfile.mkdtemp()
        try:
            out = os.path.join(d, "h.token")
            with open(out, "w") as f:
                f.write("old\n")
            os.chmod(out, 0o644)
            args = mock.Mock(role="agent", host="b1", tokens=os.path.join(d, "tokens.json"), out=out)
            with mock.patch("sys.stdout"):
                corecli.cmd_tokens(args)
            self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
            with open(out) as f:
                self.assertNotEqual(f.read(), "old\n")
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class LocalTokenTest(unittest.TestCase):
    """Item 87: a local host without hosts/<host>.token gets one provisioned."""

    def test_local_token_provisioned(self):
        d = tempfile.mkdtemp()
        try:
            tf = os.path.join(d, ".wavecoord", "tokens.json")
            os.makedirs(os.path.dirname(tf))
            with open(tf, "w") as f:
                json.dump({"version": 1, "tokens": {}}, f)
            with mock.patch.dict(os.environ, {"HOME": d, "WAVECOORD_HOME": os.path.join(d, ".wavecoord"),
                                              "WAVECOORD_TOKENS_FILE": tf}):
                from wavecoord.launcher.client import Launcher
                p = Launcher.local_token_file(mock.Mock(), "local")
                self.assertEqual(p, os.path.join(d, ".wavecoord", "hosts", "local.token"))
                self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
                with open(p) as f:
                    tok = f.read().strip()
                with open(tf) as f:
                    ent = json.load(f)["tokens"][hashlib.sha256(tok.encode()).hexdigest()]
                self.assertEqual((ent["role"], ent["principal"]), ("agent", "host:local"))
                self.assertEqual(Launcher.local_token_file(mock.Mock(), "local"), p)  # once
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
