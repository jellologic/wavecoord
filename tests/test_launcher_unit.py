"""Unit tests of the launcher: placement, the stream parser, config, the zipapp and plugin archive."""

import io
import json
import os
import tarfile
import tempfile
import unittest
import zipfile

import _paths
from wavecoord import zipapp
from wavecoord.launcher import cli, client, config, coord, stream
from wavecoord.launcher.common import LauncherError
from wavecoord.launcher.worktrees import check_branch
from wavecoord.launcher.wrapper import continue_cut, synthetic_report, validate_report


def probe(free=1, mem=8.0, ok=True, auth="logged-in", installed=True, repo_ok=True, slots=2):
    return {"ok": ok, "free": free, "active": slots - free, "slots": slots, "mem_avail_gib": mem,
            "repo_ok": repo_ok, "claude": {"installed": installed, "auth": auth}, "error": None if ok else "down"}


class UsdTest(unittest.TestCase):
    def test_cost_column(self):
        self.assertEqual([cli._usd(v) for v in (0.009694669999999999, 0.35, 2, None)],
                         ["$0.0097", "$0.35", "$2.00", "-"])


class PlacementTest(unittest.TestCase):
    def test_most_free_slots_then_memory(self):
        order, why = client.place({"a": probe(free=1, mem=64), "b": probe(free=3, mem=4), "c": probe(free=3, mem=16)})
        self.assertEqual(order, ["c", "b", "a"])
        self.assertEqual(why, {})

    def test_tie_is_by_name(self):
        order, _ = client.place({"b": probe(), "a": probe()})
        self.assertEqual(order, ["a", "b"])

    def test_ineligible_hosts(self):
        order, why = client.place({
            "full": probe(free=0), "down": probe(ok=False), "noauth": probe(auth="no"),
            "noclaude": probe(installed=False), "norepo": probe(repo_ok=False), "good": probe(mem=None)})
        self.assertEqual(order, ["good"])
        self.assertTrue(why["full"].startswith("full"))
        self.assertTrue(why["down"].startswith("unreachable"))
        self.assertIn("not logged in", why["noauth"])
        self.assertIn("not installed", why["noclaude"])
        self.assertIn("git checkout", why["norepo"])

    def test_keychain_auth_is_eligible(self):
        order, _ = client.place({"mac": probe(auth="keychain?")})
        self.assertEqual(order, ["mac"])


class AuthProbeTest(unittest.TestCase):
    def test_presence_only(self):
        from wavecoord.launcher.hostside import claude_auth
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(claude_auth({"ANTHROPIC_API_KEY": "x"}), "env")
            cfg = os.path.join(d, "cfg")
            os.makedirs(cfg)
            expect_none = "keychain?" if os.uname().sysname == "Darwin" else "no"
            self.assertEqual(claude_auth({"CLAUDE_CONFIG_DIR": cfg}), expect_none)
            open(os.path.join(cfg, ".credentials.json"), "w").close()
            self.assertEqual(claude_auth({"CLAUDE_CONFIG_DIR": cfg}), "logged-in")


class StreamTest(unittest.TestCase):
    def test_parse(self):
        s = stream.StreamStats()
        lines = [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "m", "claude_code_version": "9",
             "plugins": [{"name": "wavecoord", "path": "/x"}]},
            {"type": "system", "subtype": "hook_started", "hook_name": "SessionStart"},
            {"type": "assistant", "parent_tool_use_id": None,
             "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "make test"}}]}},
            {"type": "assistant", "parent_tool_use_id": "toolu_1", "message": {"content": []}},
            {"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 5, "retry_delay_ms": 10,
             "error_status": 529, "error": "overloaded"},
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning", "utilization": 0.53}},
            {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"}},
            {"type": "result", "subtype": "success", "is_error": False, "num_turns": 4, "total_cost_usd": 0.5,
             "session_id": "s1", "structured_output": {"status": "done"}},
        ]
        notable = []
        for l in lines:
            notable += s.feed(json.dumps(l))
        notable += s.feed("not json")
        self.assertEqual([k for k, _ in notable], ["api_retry", "rate_limit"])
        sm = s.summary()
        self.assertEqual((sm["session_id"], sm["turns"], sm["cost_usd"]), ("s1", 4, 0.5))
        self.assertEqual((sm["api_retries"], sm["throttled_retries"], sm["hook_events"]), (1, 1, 1))
        self.assertEqual(sm["last_tool"], "Bash: make test")
        self.assertEqual(sm["plugins"], ["wavecoord"])
        self.assertFalse(s.is_error)
        self.assertEqual(s.structured_output, {"status": "done"})
        self.assertEqual(s.bad_lines, 1)

    def test_a_usage_warning_is_not_throttling(self):
        s = stream.StreamStats()
        notable = s.feed(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.53}}))
        self.assertEqual(notable, [])
        self.assertEqual(s.throttle(), [])

    def test_no_result_is_error(self):
        s = stream.StreamStats()
        s.feed(json.dumps({"type": "system", "subtype": "init", "session_id": "x"}))
        self.assertTrue(s.is_error)
        s.feed(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True}))
        self.assertTrue(s.is_error)


class ReportTest(unittest.TestCase):
    def test_continue_cut(self):
        self.assertEqual(continue_cut(None, "error_max_turns"), "error_max_turns")
        self.assertEqual(continue_cut("token_budget", "error_during_execution"), "token_budget")
        for reason, subtype in ((None, "success"), (None, "error_during_execution"), ("timeout", "error_max_turns"),
                                ("lease_lost", None), ("stopped", "error_max_turns")):
            self.assertIsNone(continue_cut(reason, subtype), (reason, subtype))
        rep = synthetic_report("x", ["a" * 40], ["f.py"], "continue: 1 commits pushed")
        self.assertEqual(validate_report(rep), [])

    def test_synthetic_report_is_valid(self):
        self.assertEqual(validate_report(synthetic_report("x" * 5000, ["a" * 40], ["f.py"])), [])
        self.assertEqual(validate_report(synthetic_report("")), [])

    def test_validate(self):
        with open(os.path.join(_paths.FIXTURES, "report", "done.json")) as f:
            self.assertEqual(validate_report(json.load(f)), [])
        self.assertTrue(validate_report(None))
        self.assertTrue(validate_report({"status": "done"}))


class BranchTest(unittest.TestCase):
    def test_refuses(self):
        for b in ("main", "master", "HEAD", "-x", "", "trunk", "dev", "coord/log", "a..b"):
            with self.assertRaises(LauncherError, msg=b):
                check_branch(b, "dev", "coord/log")
        check_branch("wave/t1-1", "main", "coord/log")


class ConfigTest(unittest.TestCase):
    def test_defaults_without_file(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config.load_config(cwd=d)
            self.assertEqual(list(cfg.hosts), ["local"])
            self.assertEqual(cfg.hosts["local"]["slots"], 2)
            self.assertEqual(cfg.hosts["local"]["repo_path"], cfg.root)
            self.assertEqual(cfg.branch_prefix, "wave")
            self.assertEqual(cfg.pr_cmd, config.DEFAULT_PR_CMD)

    def test_fixture_and_relative_repo_path(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".claude"))
            with open(os.path.join(_paths.FIXTURES, "config", "full.json")) as f:
                raw = json.load(f)
            with open(os.path.join(d, ".claude", "wavecoord.json"), "w") as f:
                json.dump(raw, f)
            sub = os.path.join(d, "x", "y")
            os.makedirs(sub)
            cfg = config.load_config(cwd=sub)
            self.assertEqual(cfg.hosts["local"]["repo_path"], os.path.normpath(cfg.root))
            self.assertEqual(cfg.hosts["build-1"]["repo_path"], "/srv/repo")
            self.assertEqual(cfg.wip_cap, 6)
            self.assertEqual(cfg.coord_url(), os.environ.get("WAVECOORD_URL") or
                             os.environ.get("CLAUDE_PLUGIN_OPTION_COORD_URL") or "http://127.0.0.1:18741")

    def test_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, ".claude", "wavecoord.json")
            os.makedirs(os.path.dirname(p))
            with open(p, "w") as f:
                json.dump({"project": "demo", "hosts": [{"name": "x"}]}, f)
            with self.assertRaises(LauncherError):
                config.load_config(cwd=d)


class ZipappTest(unittest.TestCase):
    def test_embedded_schemas_match(self):
        with open(os.path.join(_paths.ROOT, "plugin", "bin", "wavecoord"), "rb") as f:
            data = f.read()
        z = zipfile.ZipFile(io.BytesIO(data[len(zipapp.SHEBANG):]))
        for name in zipapp.EMBEDDED_SCHEMAS:
            with open(os.path.join(_paths.SCHEMAS, name), "rb") as f:
                self.assertEqual(z.read("wavecoord/_schemas/" + name), f.read(), name)
        with open(os.path.join(_paths.SCHEMAS, "report.schema.json"), "rb") as f:
            self.assertEqual(zipapp.load_schema_bytes("report.schema.json"), f.read())

    def test_plugin_tar_is_deterministic_and_pins_this_build(self):
        pd = os.path.join(_paths.ROOT, "plugin")
        a, b = zipapp.plugin_tar(pd), zipapp.plugin_tar(pd)
        self.assertEqual(a, b)
        with tarfile.open(fileobj=io.BytesIO(a)) as t:
            names = t.getnames()
            self.assertIn(".claude-plugin/plugin.json", names)
            self.assertEqual(t.extractfile("bin/wavecoord").read(), zipapp.self_bytes())
            self.assertTrue(t.getmember("bin/wavecoord").mode & 0o100)

    def test_safe_extract_refuses_escape(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            info = tarfile.TarInfo("../evil")
            info.size = 1
            t.addfile(info, io.BytesIO(b"x"))
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                zipapp.safe_extract(buf.getvalue(), d)


class SpoolTest(unittest.TestCase):
    def test_spool_and_flush_to_dead_server(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("WAVECOORD_HOME")
            os.environ["WAVECOORD_HOME"] = d
            try:
                c = coord.Coord("http://127.0.0.1:9", agent="a", token="")
                self.assertEqual(c.post_or_spool("/api/notify", {"kind": "x"}), "spooled")
                self.assertEqual(coord.spool_count(), 1)
                self.assertEqual(coord.flush_spool()["kept"], 1)
                self.assertEqual(coord.Coord(None).post_or_spool("/api/notify", {}), "off")
            finally:
                if old is None:
                    os.environ.pop("WAVECOORD_HOME")
                else:
                    os.environ["WAVECOORD_HOME"] = old


if __name__ == "__main__":
    unittest.main()


class SessionsTest(unittest.TestCase):
    def test_transcript_path(self):
        from wavecoord.launcher import sessions
        self.assertEqual(sessions.encode_cwd("/home/u/wt/t-1.x"), "-home-u-wt-t-1-x")
        with tempfile.TemporaryDirectory() as t:
            t = os.path.realpath(t)
            cfg, wt = os.path.join(t, "cfg"), os.path.join(t, "wt", "a-1")
            os.makedirs(wt)
            env = {"CLAUDE_CONFIG_DIR": cfg, "HOME": os.path.join(t, "nohome")}
            sid = "952a5168-0000-4000-8000-000000000001"
            self.assertIsNone(sessions.transcript_path(sid, wt, env))
            d = os.path.join(cfg, "projects", sessions.encode_cwd(wt))
            os.makedirs(d)
            open(os.path.join(d, sid + ".jsonl"), "w").close()
            self.assertEqual(sessions.transcript_path(sid, wt, env), os.path.join(d, sid + ".jsonl"))
            # another project (a shortened long path, a moved worktree): --resume finds it too
            self.assertEqual(sessions.transcript_path(sid, os.path.join(t, "elsewhere"), env),
                             os.path.join(d, sid + ".jsonl"))
            self.assertIsNone(sessions.transcript_path("../x", wt, env))
            self.assertIsNone(sessions.transcript_path(sid, wt, {"HOME": os.path.join(t, "nohome")}))

    def test_lineage_order(self):
        from wavecoord.launcher import sessions
        with tempfile.TemporaryDirectory() as d:
            for ms, task in ((200, {"session_id": "b", "resume": "a"}), (100, {"session_id": "a"})):
                os.makedirs(os.path.join(d, "prev-%d" % ms))
                with open(os.path.join(d, "prev-%d" % ms, "task.json"), "w") as f:
                    json.dump(task, f)
            # 0.1.4 lineage: the oldest run's id comes first, before the newest run's unused fresh id
            self.assertEqual(sessions.lineage(d, {"session_id": "c", "resume": "b"}, {"session_id": "r"}),
                             ["a", "b", "c", "r"])
            self.assertEqual(sessions.lineage(d, {"transcript_session": "f", "session_root": "a", "session_id": "c"},
                                              {}), ["f", "a", "b", "c"])

    def test_fresh_prompt(self):
        from wavecoord.launcher import sessions
        p = sessions.fresh_prompt("Do the task.", "Do the task.", ["abc1234 first"], "lost")
        self.assertEqual(p.count("Do the task."), 1)  # --continue --spec: the spec prompt only once
        self.assertIn("- abc1234 first", p)
        p = sessions.fresh_prompt("Do the task.", "Fix it.", [], "lost")
        self.assertLess(p.index("Do the task."), p.index("Fix it."))
        self.assertTrue(sessions.no_conversation("No conversation found with session ID: x", None))
        self.assertTrue(sessions.no_conversation("", {"errors": ["No conversation found with session ID: x"]}))
        self.assertFalse(sessions.no_conversation("other", {"result": "ok"}))
class SteerLinesTest(unittest.TestCase):
    def test_user_and_control_lines(self):
        from wavecoord.launcher import steer
        u = json.loads(steer.user_line("hi", "next", "u-1"))
        self.assertEqual(u, {"type": "user", "message": {"role": "user", "content": "hi"}, "parent_tool_use_id": None,
                             "priority": "next", "uuid": "u-1"})
        c = json.loads(steer.control_line("set_model", "wc-1", model="opus"))
        self.assertEqual(c, {"type": "control_request", "request_id": "wc-1",
                             "request": {"subtype": "set_model", "model": "opus"}})
        self.assertTrue(steer.user_line("x").endswith(b"\n"))

    def test_parse_action_only_from_the_lead(self):
        from wavecoord.launcher import steer
        body = steer.action_body("interrupt", note="read the error")
        self.assertEqual(body, 'wavecoord:{"action":"interrupt","note":"read the error"}')
        self.assertEqual(steer.parse_action({"sender": "lead", "body": body}),
                         {"action": "interrupt", "model": None, "note": "read the error"})
        self.assertIsNone(steer.parse_action({"sender": "w1", "body": body}))
        self.assertIsNone(steer.parse_action({"sender": "lead", "body": "plain hint"}))
        self.assertIsNone(steer.parse_action({"sender": "lead", "body": "wavecoord:{not json"}))
        self.assertIsNone(steer.parse_action({"sender": "lead", "body": 'wavecoord:{"action":"rm"}'}))
        self.assertIsNone(steer.parse_action({"sender": "lead", "body": 'wavecoord:{"action":"model"}'}))

    def test_steer_writes_and_records(self):
        from wavecoord.launcher import steer
        with tempfile.TemporaryDirectory() as d:
            buf = io.BytesIO()
            st = steer.Steer(buf, d, lambda m: None)
            self.assertTrue(st.prompt("go"))
            self.assertTrue(st.send("hint", ids=[3]))
            rid = st.control("interrupt")
            self.assertEqual(st.requests[rid], "interrupt")
            sent = [json.loads(l) for l in buf.getvalue().splitlines()]
            self.assertEqual([x["type"] for x in sent], ["user", "user", "control_request"])
            with open(os.path.join(d, "steer.jsonl")) as f:
                rec = [json.loads(l) for l in f]
            self.assertEqual([(r["kind"], r.get("ids")) for r in rec], [("user", [3]), ("control", None)])
            st.close()
            self.assertFalse(st.send("late"))


class StreamDigestTest(unittest.TestCase):
    def feed(self, st, *objs):
        out = []
        for o in objs:
            out += st.feed(json.dumps(o))
        return out

    def tool(self, i, cmd="npm test", name="Bash"):
        inp = {"command": cmd} if name == "Bash" else {"file_path": cmd}
        return {"type": "assistant", "parent_tool_use_id": None, "message": {
            "id": "m%d" % i, "content": [{"type": "tool_use", "id": "t%d" % i, "name": name, "input": inp}]}}

    def fail(self, i):
        return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t%d" % i,
                                                          "is_error": True}]}}

    def test_loop_once(self):
        st = stream.StreamStats()
        ev = self.feed(st, self.tool(1), self.tool(2))
        self.assertEqual(ev, [])
        ev = self.feed(st, self.tool(3), self.tool(4))
        self.assertEqual([e[1]["why"] for e in ev], ["loop"])  # once per tool call, not again on the fourth

    def test_failing_command(self):
        st = stream.StreamStats()
        ev = self.feed(st, self.tool(1), self.fail(1), self.tool(2, "ls"), self.tool(3), self.fail(3),
                       self.tool(4, "ls -la"), self.tool(5), self.fail(5))
        self.assertEqual([e[1]["why"] for e in ev], ["failing"])
        self.assertIn("Bash: npm test failed 3 times", ev[0][1]["text"])
        self.assertEqual(st.digest()["failing"], ["Bash: npm test"])

    def test_waiting_state_and_controls(self):
        st = stream.StreamStats()
        ev = self.feed(st, {"type": "system", "subtype": "session_state_changed", "state": "requires_action"},
                       {"type": "control_response", "response": {"subtype": "success", "request_id": "wc-1",
                                                                 "response": {"percentage": 61}}})
        self.assertEqual([e[1]["why"] for e in ev], ["waiting"])
        self.assertEqual(st.digest()["state"], "requires_action")
        self.assertEqual(st.digest()["context_pct"], 61)
        self.assertIn("wc-1", st.controls)

    def test_digest_text_edits_and_results(self):
        st = stream.StreamStats()
        self.feed(st, self.tool(1, "src/a.py", "Edit"),
                  {"type": "assistant", "parent_tool_use_id": None, "message": {"id": "x", "content": [
                      {"type": "text", "text": "Now   I run\nthe tests."}]}},
                  {"type": "assistant", "parent_tool_use_id": "sub", "message": {"id": "y", "content": [
                      {"type": "text", "text": "subagent words"}]}},
                  {"type": "result", "subtype": "success", "queued_turn_count": 1})
        d = st.digest()
        self.assertEqual(d["edited"], ["src/a.py"])
        self.assertEqual(d["last_text"], "Now I run the tests.")
        self.assertEqual((st.results, st.queued_turns), (1, 1))
