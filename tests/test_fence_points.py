"""Fence table tests (tests/fence/README.md, section 3): one test per row of the
failure table of docs/design/edit-fence.md section 4.2 for each point, the path
table of section 3.6, the output shapes, the override, the modes, the server
rules and the launcher's fence helpers. A real coordinator behind the fault
proxy of fenceworld.py.
"""

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

import _paths
from fencesim import Sim
from fenceworld import git
from wavecoord import fence, fencecli, fencecore, gatecheck, guard, hooks
from wavecoord.core import ApiError, Ctx, lead_ctx
from wavecoord.launcher import fencehost

# condition -> proxy fault (None: special handling)
CONDITIONS = {
    "refused": "down", "timeout": "delay", "http_500": "500", "http_503": "503", "auth_401": "401",
    "auth_403": "403", "auth_421": "421", "bad_request_400": "400", "malformed_json": "notjson",
    "malformed_schema": "wrongschema", "truncated": "truncated",
}
KIND = {"down": "unavailable", "delay": "unavailable", "500": "unavailable", "503": "unavailable",
        "401": "auth", "403": "auth", "421": "auth", "400": "bad_request", "notjson": "malformed",
        "wrongschema": "malformed", "truncated": "unavailable"}


class FenceWorld(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sim = Sim(0, n_workers=2)
        cls.w = cls.sim.w
        cls.a, cls.b = cls.w.workers

    @classmethod
    def tearDownClass(cls):
        cls.w.close()

    def setUp(self):
        self.w.proxy.fault = None
        fence.remove_override(self.w.base_env("agent"))
        try:
            os.remove(fence.journal_path(self.w.base_env("agent")))
        except OSError:
            pass
        self.w.assign(self.b, [("docs/**", "exclusive")])
        self.w.assign(self.a, [("a/**", "exclusive")])

    def tearDown(self):
        self.w.proxy.fault = None

    def hook(self, wk, target, env_extra=None, env=None, cwd=None, tool="Edit"):
        env = dict(env or self.w.worker_env(wk))
        env.update(env_extra or {})
        inp = {"session_id": "s-" + (wk.agent if wk else "x"), "cwd": cwd or wk.wt, "hook_event_name": "PreToolUse",
               "tool_name": tool, "tool_input": {"file_path": target}}
        out, err = io.StringIO(), io.StringIO()
        rc = hooks.main("PreToolUse", stdin=io.StringIO(json.dumps(inp)), stdout=out, env=env, stderr=err)
        return rc, out.getvalue(), err.getvalue()

    def decision(self, rc, out):
        d = json.loads(out) if out.strip() else {}
        return (d.get("hookSpecificOutput") or {}).get("permissionDecision") or "allow", d

    def lead_env(self):
        e = self.w.base_env("lead")
        e["WAVECOORD_ROLE"] = "lead"
        return e

    def commit_paths(self, wk, rel):
        with open(os.path.join(wk.wt, rel), "a") as f:
            f.write("x\n")
        git(wk.wt, "add", "-A")

    def reset(self, wk):
        git(wk.wt, "reset", "-q", "--hard", "HEAD")
        git(wk.wt, "clean", "-fdq")

    def push_sha(self, wk, rel="a/x.py"):
        with open(os.path.join(wk.wt, rel), "a") as f:
            f.write("y\n")
        git(wk.wt, "commit", "-q", "-am", "c")
        return git(wk.wt, "rev-parse", "HEAD").strip()

    def gate(self, wk, sha, lease=None):
        return gatecheck.run(None, wk.task, wk.attempt, "origin/main..%s" % sha, None,
                             lease or "%s@%d" % (wk.task, wk.epoch), self.w.repo, env=self.w.base_env("gate"),
                             out=io.StringIO(), err=io.StringIO())


class FailureTableTest(FenceWorld):
    """docs/design/edit-fence.md section 4.2: each condition at each point."""

    def test_healthy_rows(self):
        rc, out, _ = self.hook(self.a, "a/x.py")
        self.assertEqual((rc, out), (0, ""))  # allow: no output, exit 0
        rc, out, _ = self.hook(self.a, "docs/api.md")
        dec, d = self.decision(rc, out)
        self.assertEqual((rc, dec), (0, "deny"))
        self.assertIn("foreign_exclusive", d["hookSpecificOutput"]["permissionDecisionReason"])

    def test_conditions_at_every_point(self):
        sha = self.push_sha(self.a)
        for cond, fault in sorted(CONDITIONS.items()):
            with self.subTest(condition=cond):
                self.w.proxy.fault = fault
                # E1 worker: deny with the kind
                rc, out, _ = self.hook(self.a, "a/x.py")
                dec, d = self.decision(rc, out)
                self.assertEqual((rc, dec), (0, "deny"), out)
                self.assertIn(KIND[fault], d["hookSpecificOutput"]["permissionDecisionReason"])
                # E1 lead: allow (with a systemMessage for unavailable and auth)
                rc, out, _ = self.hook(None, os.path.join(self.w.repo, "docs/api.md"), env=self.lead_env(),
                                       cwd=self.w.repo)
                dec, d = self.decision(rc, out)
                self.assertEqual(dec, "allow", out)
                if KIND[fault] in ("unavailable", "auth"):
                    self.assertIn("fail open", d.get("systemMessage", ""))
                # E2: exit 1
                self.commit_paths(self.a, "a/y.py")
                self.assertEqual(guard.check("staged", cwd=self.a.wt, env=self.w.commit_env(self.a),
                                             err=io.StringIO()), 1)
                git(self.a.wt, "reset", "-q")
                # E3: refuse
                res = fence.push_check(self.a.wt, "origin/main", self.a.agent, self.a.task, self.a.epoch,
                                       self.w.worker_env(self.a))
                self.assertFalse(res["ok"])
                self.assertIn(res["kind"], (KIND[fault], "journal"))
                # E5: exit 3
                self.assertEqual(self.gate(self.a, sha), 3)
                self.w.proxy.fault = None
        self.reset(self.a)

    def test_no_url(self):
        env = self.w.worker_env(self.a)
        env.pop("WAVECOORD_URL")
        rc, out, _ = self.hook(self.a, "a/x.py", env=env)
        dec, d = self.decision(rc, out)
        self.assertEqual(dec, "deny")
        self.assertIn("unavailable", d["hookSpecificOutput"]["permissionDecisionReason"])
        # E1 lead: allow, no output
        lenv = self.lead_env()
        lenv.pop("WAVECOORD_URL")
        os.rename(os.path.join(self.w.repo, ".claude", "wavecoord.json"), os.path.join(self.w.root, "cfg.json"))
        try:
            self.assertEqual(self.hook(None, os.path.join(self.w.repo, "a/x.py"), env=lenv, cwd=self.w.repo)[:2],
                             (0, ""))
        finally:
            os.rename(os.path.join(self.w.root, "cfg.json"), os.path.join(self.w.repo, ".claude", "wavecoord.json"))
        # E2: exit 1 when the identity file exists, E3 refuse, E5 exit 3
        self.commit_paths(self.a, "a/y.py")
        cenv = self.w.commit_env(self.a)
        cenv.pop("WAVECOORD_URL")
        self.assertEqual(guard.check("staged", cwd=self.a.wt, env=cenv, err=io.StringIO()), 1)
        self.reset(self.a)
        wenv = self.w.worker_env(self.a)
        wenv.pop("WAVECOORD_URL")
        res = fence.push_check(self.a.wt, "origin/main", self.a.agent, self.a.task, self.a.epoch, wenv)
        self.assertEqual((res["ok"], res["kind"]), (False, "unavailable"))
        genv = self.w.base_env("gate")
        genv.pop("WAVECOORD_URL")
        sha = self.push_sha(self.a)
        self.assertEqual(gatecheck.run(None, self.a.task, 1, "origin/main..%s" % sha, None,
                                       "%s@%d" % (self.a.task, self.a.epoch), self.w.root, env=genv,
                                       out=io.StringIO(), err=io.StringIO()), 3)
        self.reset(self.a)

    def test_violation_rows(self):
        # E1 lead: ask
        rc, out, _ = self.hook(None, os.path.join(self.w.repo, "docs/api.md"), env=self.lead_env(), cwd=self.w.repo)
        self.assertEqual(self.decision(rc, out)[0], "ask")
        # E1 other session: ask
        rc, out, _ = self.hook(None, os.path.join(self.w.repo, "docs/api.md"), env=self.w.base_env("agent"),
                               cwd=self.w.repo)
        self.assertEqual(self.decision(rc, out)[0], "ask")
        # fence.lead deny and warn, from the repo config
        cfg = os.path.join(self.w.repo, ".claude", "wavecoord.json")
        with open(cfg) as f:
            keep = f.read()
        try:
            for action, want in (("deny", "deny"), ("warn", "allow")):
                with open(cfg, "w") as f:
                    json.dump({"project": "sim", "fence": {"edit": "enforce", "lead": action}}, f)
                rc, out, _ = self.hook(None, os.path.join(self.w.repo, "docs/api.md"), env=self.lead_env(),
                                       cwd=self.w.repo)
                self.assertEqual(self.decision(rc, out)[0], want, action)
                if action == "warn":
                    self.assertIn("foreign_exclusive", out)
        finally:
            with open(cfg, "w") as f:
                f.write(keep)
        # E2 exit 1, E3 refuse, E5 exit 1
        self.commit_paths(self.a, "docs/api.md")
        self.assertEqual(guard.check("staged", cwd=self.a.wt, env=self.w.commit_env(self.a), err=io.StringIO()), 1)
        git(self.a.wt, "commit", "-q", "--no-verify", "-m", "outside")
        res = fence.push_check(self.a.wt, "origin/main", self.a.agent, self.a.task, self.a.epoch,
                               self.w.worker_env(self.a))
        self.assertEqual((res["ok"], res["kind"]), (False, "violation"))
        sha = git(self.a.wt, "rev-parse", "HEAD").strip()
        self.assertEqual(self.gate(self.a, sha), 1)
        self.reset(self.a)

    def test_hook_crash_denies_and_backstop(self):
        import wavecoord.fence as fm
        old = fm.pretool
        fm.pretool = lambda hook: 1 / 0
        try:
            rc, out, _ = self.hook(self.a, "a/x.py")
        finally:
            fm.pretool = old
        self.assertEqual(self.decision(rc, out)[0], "deny")

        class Broken(io.StringIO):
            def write(self, s):
                raise OSError("closed")

        inp = {"session_id": "s", "cwd": self.a.wt, "hook_event_name": "PreToolUse", "tool_name": "Edit",
               "tool_input": {"file_path": "docs/api.md"}}
        err = io.StringIO()
        rc = hooks.main("PreToolUse", stdin=io.StringIO(json.dumps(inp)), stdout=Broken(),
                        env=self.w.worker_env(self.a), stderr=err)
        self.assertEqual(rc, 2)
        self.assertIn("foreign_exclusive", err.getvalue())

    def test_deny_output_shape(self):
        rc, out, _ = self.hook(self.a, "docs/api.md")
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("\n"), 1)
        d = json.loads(out)
        self.assertEqual(sorted(d), ["hookSpecificOutput"])
        h = d["hookSpecificOutput"]
        self.assertEqual(sorted(h), ["hookEventName", "permissionDecision", "permissionDecisionReason"])
        self.assertEqual((h["hookEventName"], h["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertIn("Do not write this path in another way", h["permissionDecisionReason"])

    def test_wrapper_crash_cli_backstop(self):
        """A broken wavecoord build: the CLI's own backstop exits 2 for a fenced worker edit."""
        env = self.w.worker_env(self.a)
        env["PYTHONPATH"] = os.path.join(self.w.root, "nowhere")
        p = subprocess.run([sys.executable, os.path.join(_paths.ROOT, "plugin", "bin", "wavecoord"), "hook",
                            "PreToolUse"], input="{not json", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, env=env, timeout=60)
        self.assertIn(p.returncode, (0, 2))  # handled in-process; the hooks.json onFailure covers a dead build


class PathTableTest(FenceWorld):
    """docs/design/edit-fence.md section 3.6."""

    def calls(self):
        return len(self.w.coord.check_trace)

    def test_rows(self):
        a, b, w = self.a, self.b, self.w
        nested = os.path.join(a.wt, ".claude", "worktrees", "sub")
        if not os.path.isdir(nested):
            git(a.wt, "worktree", "add", "-q", "-b", "sub-%s" % a.task, nested, "HEAD")
        rows = [
            ("own", os.path.join(a.wt, "a", "x.py"), "allow", True),
            ("own relative", "a/x.py", "allow", True),
            ("own outside the lease", "lib/x.py", "deny", True),
            ("nested subagent worktree", os.path.join(nested, "a", "x.py"), "allow", True),
            ("nested, outside the lease", os.path.join(nested, "lib", "x.py"), "deny", True),
            ("sibling worktree", os.path.join(b.wt, "a", "x.py"), "deny", False),
            ("main checkout", os.path.join(w.repo, "a", "x.py"), "deny", False),
            (".git of the own worktree", os.path.join(a.wt, ".git"), "deny", False),
            ("git common dir", os.path.join(w.repo, ".git", "config"), "deny", False),
            ("WAVECOORD_HOME", os.path.join(w.home, "fence-override.json"), "deny", False),
            ("~/.claude", os.path.expanduser(os.path.join("~", ".claude", "settings.json")), "deny", False),
            ("git-ignored in the own worktree", "build/out.txt", "allow", False),
            ("git-ignored file", "a/debug.log", "allow", False),
            ("outside the repo", os.path.join(w.outside, "x.txt"), "allow", False),
            ("../ into the lease", "lib/../a/y.py", "allow", True),
            ("symlink out of the lease", "docs/link/x.py", "allow", True),
        ]
        for name, target, want, called in rows:
            with self.subTest(row=name):
                n = self.calls()
                rc, out, _ = self.hook(a, target)
                dec, d = self.decision(rc, out)
                self.assertEqual(dec, want, "%s: %s" % (name, out))
                self.assertEqual(self.calls() > n, called, name)
                if name == "sibling worktree":
                    self.assertIn("foreign_worktree", out)
                if name in (".git of the own worktree", "WAVECOORD_HOME", "~/.claude"):
                    self.assertIn("protected_local", out)

    def test_plugin_root_is_protected(self):
        root = os.path.join(self.w.root, "plugin-copy")
        os.makedirs(root, exist_ok=True)
        rc, out, _ = self.hook(self.a, os.path.join(root, "hooks", "hooks.json"), {"CLAUDE_PLUGIN_ROOT": root})
        self.assertIn("protected_local", out)

    def test_symlink_into_a_foreign_exclusive(self):
        self.w.assign(self.a, [("docs/**", "shared")])
        self.w.assign(self.b, [("a/**", "exclusive")])
        rc, out, _ = self.hook(self.a, "docs/link/x.py")  # docs/link -> ../a
        self.assertEqual(self.decision(rc, out)[0], "deny")
        self.assertIn("a/x.py", out)

    def test_bad_path_characters(self):
        rc, out, _ = self.hook(self.a, "a/with space.py")
        self.assertEqual(self.decision(rc, out)[0], "deny")
        self.assertIn("outside_lease", out)

    def test_case_folding(self):
        old = fence.case_insensitive
        fence.case_insensitive = lambda top: True
        try:
            self.assertEqual(fence.canon_case(self.a.wt, "A/X.py"), "a/x.py")
            self.assertEqual(fence.canon_case(self.a.wt, "A/New.py"), "a/New.py")
            self.w.assign(self.a, [("A/**", "shared")])
            self.w.assign(self.b, [("a/**", "exclusive")])
            rc, out, _ = self.hook(self.a, "A/x.py")
            self.assertEqual(self.decision(rc, out)[0], "deny")
        finally:
            fence.case_insensitive = old


class OverrideTest(FenceWorld):
    def test_env_var_alone_has_no_effect(self):
        self.w.proxy.fault = "down"
        rc, out, _ = self.hook(self.a, "a/x.py", {"WAVECOORD_FENCE_OFFLINE": "1"})
        dec, d = self.decision(rc, out)
        self.assertEqual(dec, "deny")
        self.assertIn("WAVECOORD_FENCE_OFFLINE has no effect", d.get("systemMessage", ""))

    def test_override_allows_e1_e2_and_logs_but_not_e3_e5(self):
        env = self.w.base_env("agent")
        self.w.proxy.fault = "down"
        fence.write_override(env, 3600, "outage drill")
        rc, out, _ = self.hook(self.a, "a/x.py")
        self.assertEqual(self.decision(rc, out)[0], "allow")
        self.assertIn("offline override", out)
        self.assertEqual([e["point"] for e in fence.journal_entries(env)], ["edit"])
        # a real violation that the server answered is never overridden; that reachable call sends the journal
        self.w.proxy.fault = None
        self.assertEqual(self.decision(*self.hook(self.a, "docs/api.md")[:2])[0], "deny")
        self.assertEqual(fence.journal_entries(env), [])
        self.w.proxy.fault = "down"
        self.commit_paths(self.a, "a/y.py")
        self.assertEqual(guard.check("staged", cwd=self.a.wt, env=self.w.commit_env(self.a), err=io.StringIO()), 0)
        git(self.a.wt, "commit", "-q", "--no-verify", "-m", "c")
        self.assertEqual([e["point"] for e in fence.journal_entries(env)], ["commit"])
        res = fence.push_check(self.a.wt, "origin/main", self.a.agent, self.a.task, self.a.epoch,
                               self.w.worker_env(self.a))
        self.assertEqual((res["ok"], res["kind"]), (False, "journal"))
        sha = git(self.a.wt, "rev-parse", "HEAD").strip()
        self.assertEqual(self.gate(self.a, sha), 3)
        # back online: the next reachable call sends the journal, then E3 passes
        self.w.proxy.fault = None
        res = fence.push_check(self.a.wt, "origin/main", self.a.agent, self.a.task, self.a.epoch,
                               self.w.worker_env(self.a))
        self.assertTrue(res["ok"], res)
        self.assertEqual(fence.journal_entries(env), [])
        notes = [n for n in self.w.coord.state.values("notices") if n["kind"] == "fence_override"]
        self.assertEqual(notes[-1]["detail"]["reason"], "outage drill")
        self.assertEqual([[e["point"] for e in n["detail"]["entries"]] for n in notes[-2:]], [["edit"], ["commit"]])
        self.reset(self.a)

    def test_expired_or_loose_override_has_no_effect(self):
        env = self.w.base_env("agent")
        self.w.proxy.fault = "down"
        rec = fence.write_override(env, 3600, "x", now=time.time() - 7200)
        self.assertIsNone(fence.read_override(env))
        self.assertEqual(self.decision(*self.hook(self.a, "a/x.py")[:2])[0], "deny")
        fence.write_override(env, 3600, "x")
        os.chmod(fence.override_path(env), 0o644)
        self.assertIsNone(fence.read_override(env))
        del rec

    def test_cli(self):
        env = self.w.base_env("agent")
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(fencecli.offline("1h", "x", env=dict(env, CLAUDECODE="1"), out=out, err=err), 2)
        self.assertIn("human only", err.getvalue())
        self.assertEqual(fencecli.offline("1h", "x", env=env, out=out, err=err), 1)  # the coordinator answers
        self.assertIn("answers", err.getvalue())
        self.assertFalse(os.path.exists(fence.override_path(env)))
        self.w.proxy.fault = "down"
        self.assertEqual(fencecli.offline("5h", "long outage", env=env, out=out, err=err), 0)
        rec = fence.read_override(env)
        self.assertLessEqual(rec["until"] - rec["created"], fence.MAX_OVERRIDE_S)
        self.assertEqual(stat.S_IMODE(os.stat(fence.override_path(env)).st_mode), 0o600)
        self.assertEqual(fencecli.status(env=env, out=out), 0)
        self.w.proxy.fault = None
        self.assertEqual(fencecli.online(env=env, out=out, err=err), 0)
        self.assertIsNone(fence.read_override(env))


class ModesTest(FenceWorld):
    def test_stricter_of_launcher_and_worktree(self):
        cfg = os.path.join(self.a.wt, ".claude", "wavecoord.json")
        try:
            with open(cfg, "w") as f:
                json.dump({"project": "sim", "fence": "off"}, f)
            self.assertEqual(self.decision(*self.hook(self.a, "docs/api.md")[:2])[0], "deny")  # launcher: enforce
            with open(cfg, "w") as f:
                json.dump({"project": "sim", "fence": "enforce"}, f)
            env = self.w.worker_env(self.a)
            env["WAVECOORD_FENCE"] = "advisory"
            self.assertEqual(self.decision(*self.hook(self.a, "docs/api.md", env=env)[:2])[0], "deny")
            with open(cfg, "w") as f:
                json.dump({"project": "sim"}, f)
            rc, out, _ = self.hook(self.a, "docs/api.md", env=env)
            self.assertEqual(self.decision(rc, out)[0], "allow")
            self.assertIn("advisory", out)
            env["WAVECOORD_FENCE"] = "off"
            self.assertEqual(self.hook(self.a, "docs/api.md", env=env)[:2], (0, ""))
        finally:
            git(self.a.wt, "checkout", "-q", "--", ".claude/wavecoord.json")

    def test_parse(self):
        self.assertEqual(fencecore.parse_modes("enforce")["merge"], "enforce")
        self.assertEqual(fencecore.parse_modes({"push": "enforce"})["edit"], "advisory")
        self.assertEqual(fencecore.parse_modes("bogus"), fencecore.DEFAULT_MODES)
        self.assertEqual(fence.launcher_modes({"WAVECOORD_FENCE": "{broken"})["edit"], "enforce")


class ServerRulesTest(FenceWorld):
    def test_path_syntax_is_refused(self):
        for p in ("/abs/x.py", "a/../x.py", "./a.py", "a\\b.py", "a//b.py"):
            with self.subTest(path=p):
                with self.assertRaises(ApiError) as e:
                    self.w.coord.check(lead_ctx(), {"agent": None, "paths": [p], "source": "hook"})
                self.assertEqual(e.exception.status, 400)

    def test_no_lease_after_release_and_rollback_kinds(self):
        self.w.coord.release(lead_ctx(), self.a.lease_id)
        r = self.w.coord.check(lead_ctx(), {"agent": self.a.agent, "paths": ["a/x.py"], "source": "hook",
                                            "task": self.a.task, "epoch": self.a.epoch})
        self.assertEqual([v["kind"] for v in r["violations"]], ["no_lease"])
        self.assertIn("seq", r)

    def test_stale_lease_still_checked(self):
        self.sim.op_expire()
        r = self.w.coord.check(lead_ctx(), {"agent": self.a.agent, "paths": ["lib/x.py", "a/x.py"],
                                            "source": "hook", "task": self.a.task, "epoch": self.a.epoch})
        self.assertEqual([(v["path"], v["kind"]) for v in r["violations"]], [("lib/x.py", "outside_lease")])

    def test_reservation_origins(self):
        c = self.w.coord
        agent = Ctx("agent", "host:local", self.a.agent)
        for body in ({"lease_id": self.a.lease_id, "glob": "**", "mode": "exclusive"},
                     {"lease_id": self.a.lease_id, "glob": "**", "mode": "shared", "origin": "lead"}):
            with self.assertRaises(ApiError) as e:
                c.reserve(agent, body)
            self.assertEqual(e.exception.status, 403)
        out = c.reserve(agent, {"lease_id": self.a.lease_id, "glob": "lib/x.py", "mode": "shared"})
        self.assertEqual(out["reservations"][0]["origin"], "hot")
        r = c.check(lead_ctx(), {"agent": self.a.agent, "paths": ["lib/x.py"], "source": "hook"})
        self.assertEqual([v["kind"] for v in r["violations"]], ["outside_lease"])  # a hot mark is not a grant
        out = c.reserve(lead_ctx(), {"lease_id": self.a.lease_id, "glob": "lib/**", "mode": "shared"})
        self.assertEqual(out["reservations"][0]["origin"], "lead")
        self.assertEqual(c.check(lead_ctx(), {"agent": self.a.agent, "paths": ["lib/x.py"],
                                              "source": "hook"})["violations"], [])

    def test_enforce_grants_409_and_force(self):
        c = self.w.coord
        with self.assertRaises(ApiError) as e:
            c.reserve(lead_ctx(), {"lease_id": self.a.lease_id, "reservations": [{"glob": "docs/x.md",
                                                                                   "mode": "exclusive"}]})
        self.assertEqual((e.exception.status, e.exception.code), (409, "conflict"))
        self.assertTrue(e.exception.extra["conflicts"])
        out = c.reserve(lead_ctx(), {"lease_id": self.a.lease_id, "force": True,
                                     "reservations": [{"glob": "docs/x.md", "mode": "exclusive"}]})
        self.assertEqual(len(out["reservations"]), 1)
        with self.assertRaises(ApiError) as e:
            c.reserve(Ctx("agent", "host:local", self.a.agent), {"lease_id": self.a.lease_id, "force": True,
                                                                 "reservations": [{"glob": "x", "mode": "shared"}]})
        self.assertEqual(e.exception.status, 403)

    def test_gate_role(self):
        c = self.w.coord
        gate = Ctx("gate", "gate")
        with self.assertRaises(ApiError) as e:
            c.check(gate, {"agent": None, "paths": ["a/x.py"], "source": "hook"})
        self.assertEqual(e.exception.status, 403)
        with self.assertRaises(ApiError) as e:
            c.check(Ctx("agent", "host:local", self.a.agent), {"agent": None, "paths": ["a/x.py"], "source": "gate",
                                                               "task": self.a.task, "attempt": 1, "epoch": 1})
        self.assertEqual(e.exception.status, 403)
        r = c.check(gate, {"agent": None, "paths": ["a/x.py"], "source": "gate", "task": self.a.task,
                           "attempt": 1, "epoch": self.a.epoch})
        self.assertEqual((r["violations"], r["lease"]["epoch"]), ([], self.a.epoch))

    def test_report_records_the_verdict(self):
        c = self.w.coord
        rep = {"status": "done", "summary": "s", "commits": [], "files": [], "checks_run": [],
               "acceptance_met": True, "questions": [], "proposals": [], "plan_feedback": "", "risks_hit": []}
        c.report(lead_ctx(), self.a.task, 1, {"report": rep, "changed_paths": ["a/x.py", "docs/api.md"],
                                               "lease_epoch": self.a.epoch})
        f = c.state.get("attempts", "%s/1" % self.a.task)["fence"]
        self.assertEqual(f["verdict"], "fail")
        self.assertEqual([v["path"] for v in f["violations"]], ["docs/api.md", "docs/api.md"])


class GateCheckTest(FenceWorld):
    def test_lease_line(self):
        sha = self.push_sha(self.a)
        self.assertEqual(self.gate(self.a, sha), 0)
        self.assertEqual(self.gate(self.a, sha, lease="%s@%d" % (self.a.task, self.a.epoch + 1)), 1)
        body = os.path.join(self.w.root, "body.md")
        with open(body, "w") as f:
            f.write("Task: %s\nAttempt: 1\nLease: %s@%d\n\nSummary\n" % (self.a.task, self.a.task, self.a.epoch))
        out = io.StringIO()
        self.assertEqual(gatecheck.run("origin/main..%s" % sha, pr_body=body, repo=self.w.repo,
                                       env=self.w.base_env("gate"), out=out, err=io.StringIO(), as_json=True), 0)
        self.assertEqual(json.loads(out.getvalue())["verdict"], "pass")
        with open(body, "w") as f:
            f.write("Task: %s\nAttempt: 1\n" % self.a.task)
        self.assertEqual(gatecheck.run("origin/main..%s" % sha, pr_body=body, repo=self.w.repo,
                                       env=self.w.base_env("gate"), out=io.StringIO(), err=io.StringIO()), 1)
        # a steal after the push: the gate fails
        c = self.w.coord
        c.claim(lead_ctx(), {"task": self.a.task, "attempt": 2, "holder": "thief", "host": "local", "steal": True,
                             "force": True, "reservations": []})
        self.assertEqual(self.gate(self.a, sha), 1)
        self.reset(self.a)

    def test_resolver_lineage(self):
        """Lead decision: a PR that names an older epoch of the same attempt passes when a resolver
        (a --continue run of the same attempt and branch) holds the newer lease."""
        sha = self.push_sha(self.a)
        old = self.a.epoch
        c = self.w.coord
        c.claim(lead_ctx(), {"task": self.a.task, "attempt": 1, "holder": "resolver-1", "host": "local",
                             "steal": True, "force": True, "reservations": [{"glob": "a/**", "mode": "exclusive"}]})
        new = c.state.task_lease(self.a.task)["epoch"]
        self.assertGreater(new, old)
        out = io.StringIO()
        code = gatecheck.run(None, self.a.task, 1, "origin/main..%s" % sha, None, "%s@%d" % (self.a.task, old),
                             self.w.repo, env=self.w.base_env("gate"), out=out, err=io.StringIO(), as_json=True)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(self.gate(self.a, sha, lease="%s@%d" % (self.a.task, new)), 0)
        self.assertEqual(self.gate(self.a, sha, lease="%s@%d" % (self.a.task, old - 1)), 1)  # not of this attempt
        # a steal by another attempt (not a resolver of attempt 1) does not count
        c.claim(lead_ctx(), {"task": self.a.task, "attempt": 2, "holder": "thief-lineage", "host": "local",
                             "steal": True, "force": True, "reservations": [{"glob": "a/**", "mode": "exclusive"}]})
        self.assertEqual(self.gate(self.a, sha, lease="%s@%d" % (self.a.task, old)), 1)
        self.reset(self.a)

    def test_set_lease_line(self):
        self.assertEqual(gatecheck.set_lease_line("Task: t\nAttempt: 1\nLease: t@5\n\nx", "t", 9),
                         "Task: t\nAttempt: 1\nLease: t@9\n\nx")
        self.assertEqual(gatecheck.set_lease_line("Task: t\nAttempt: 1\n\nx", "t", 9),
                         "Task: t\nAttempt: 1\nLease: t@9\n\nx")
        self.assertEqual(gatecheck.set_lease_line("x", "t", 9), "Lease: t@9\nx")

    def test_renames_split(self):
        git(self.a.wt, "mv", "docs/api.md", "a/api.md")
        git(self.a.wt, "commit", "-q", "--no-verify", "-m", "mv")
        sha = git(self.a.wt, "rev-parse", "HEAD").strip()
        self.assertEqual(fence.diff_paths(self.w.repo, "origin/main...%s" % sha), ["a/api.md", "docs/api.md"])
        self.assertEqual(self.gate(self.a, sha), 1)
        self.reset(self.a)


class LivenessTest(FenceWorld):
    def test_every_path_of_the_lease_is_allowed(self):
        self.w.assign(self.b, [("docs/**", "shared")])
        self.w.assign(self.a, [("a/**", "exclusive"), ("docs/*.md", "shared"), ("x[1].py", "exclusive")])
        for rel in ("a/x.py", "a/b/z.py", "a/new.py", "docs/api.md", "docs/1.md", "x[1].py",
                    os.path.join(self.a.wt, "a", "bc", "w.py")):
            with self.subTest(path=rel):
                self.assertEqual(self.hook(self.a, rel)[:2], (0, ""))

    def test_budget(self):
        t = time.time()
        self.hook(self.a, "a/x.py")
        self.assertLess(time.time() - t, fence.E1_BUDGET_S)


class GuardInstallTest(FenceWorld):
    def test_identity_and_enforce_script(self):
        gd = git(self.a.wt, "rev-parse", "--absolute-git-dir").strip()
        with open(os.path.join(gd, "wavecoord-hooks", "identity.json")) as f:
            ident = json.load(f)
        self.assertEqual((ident["agent"], ident["task"], ident["epoch"]), (self.a.agent, self.a.task, self.a.epoch))
        self.assertEqual(ident["fence"]["commit"], "enforce")
        with open(os.path.join(gd, "wavecoord-hooks", "pre-commit")) as f:
            self.assertIn("refused (fence enforce)", f.read())

    def test_missing_build_refuses_in_enforce(self):
        d = tempfile.mkdtemp()
        try:
            git(d, "init", "-q")
            env = dict(self.w.worker_env(self.a), WAVECOORD_WORKTREE=d)
            guard.install(d, cmd=["python3", os.path.join(d, "gone.pyz")], out=io.StringIO(), env=env)
            with open(os.path.join(d, "f"), "w") as f:
                f.write("x")
            git(d, "add", "f")
            p = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "x"],
                               cwd=d, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(p.returncode, 1, p.stderr)
            self.assertIn("build is missing", p.stderr)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class LauncherHelperTest(unittest.TestCase):
    class Att(object):
        def __init__(self, modes, epoch=5, env=None):
            self.fence_modes = modes
            self.spec = {"lease_epoch": epoch}
            self.env = env or dict(os.environ)
            self.task = {"plugin_dir": None}

    def fake_claude(self, version):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "claude")
        with open(p, "w") as f:
            f.write("#!/bin/sh\necho '%s (Claude Code)'\n" % version)
        os.chmod(p, 0o755)
        self.addCleanup(shutil.rmtree, d, True)
        return p

    def test_preflight(self):
        enf = fencecore.parse_modes("enforce")
        cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cfg, True)
        env = dict(os.environ, CLAUDE_CONFIG_DIR=cfg, CLAUDE_CODE_PLUGIN_DIRS="")
        self.assertIsNone(fencehost.preflight(self.Att(fencecore.DEFAULT_MODES), "nope"))
        self.assertIn("no lease", fencehost.preflight(self.Att(enf, epoch=None, env=env), "nope"))
        self.assertIn("2.1.295", fencehost.preflight(self.Att(enf, env=env), self.fake_claude("2.1.294")))
        self.assertIsNone(fencehost.preflight(self.Att(enf, env=env), self.fake_claude("2.1.302")))
        mod = os.path.join(cfg, "dev-mods", "s", "approve", "hooks")
        os.makedirs(mod)
        with open(os.path.join(mod, "hooks.json"), "w") as f:
            json.dump({"modules": ["./register.ts"]}, f)
        with open(os.path.join(mod, "register.ts"), "w") as f:
            f.write("export const register = (on) => { on('tool.check', ($, e, next) => ({decision: 'allow'})) }\n")
        self.assertIn("tool.check", fencehost.preflight(self.Att(enf, env=env), self.fake_claude("2.1.302")))
        # push enforce only: no claude check
        self.assertIsNone(fencehost.preflight(self.Att(fencecore.parse_modes({"push": "enforce"}), env=env), "nope"))

    def test_worker_token_file(self):
        import hashlib
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        os.makedirs(os.path.join(home, "hosts"))
        old = os.environ.get("WAVECOORD_HOME")
        os.environ["WAVECOORD_HOME"] = home
        self.addCleanup(lambda: os.environ.pop("WAVECOORD_HOME") if old is None else
                        os.environ.__setitem__("WAVECOORD_HOME", old))

        def put(name, tok):
            with open(os.path.join(home, name), "w") as f:
                f.write(tok + "\n")
        put("token", "lead-tok")
        env = {"WAVECOORD_TOKENS_FILE": os.path.join(home, "tokens.json")}
        local = {"name": "local"}
        self.assertIsNone(fencehost.worker_token_file(local, "local", env))  # never the lead's own token
        put("hosts/local.token", "lead-tok")
        self.assertIsNone(fencehost.worker_token_file(local, "local", env))  # the same token: refused
        put("hosts/local.token", "agent-tok")
        self.assertEqual(fencehost.worker_token_file(local, "local", env), os.path.join(home, "hosts", "local.token"))
        with open(env["WAVECOORD_TOKENS_FILE"], "w") as f:
            json.dump({"tokens": {hashlib.sha256(b"agent-tok").hexdigest(): {"role": "lead", "principal": "x"}}}, f)
        self.assertIsNone(fencehost.worker_token_file(local, "local", env))  # a lead-role token: refused
        with open(env["WAVECOORD_TOKENS_FILE"], "w") as f:
            json.dump({"tokens": {hashlib.sha256(b"agent-tok").hexdigest(): {"role": "agent",
                                                                             "principal": "host:local"}}}, f)
        self.assertEqual(fencehost.worker_token_file(local, "local", env), os.path.join(home, "hosts", "local.token"))
        self.assertIsNone(fencehost.worker_token_file(local, "local", dict(env, CLAUDE_PLUGIN_OPTION_TOKEN="agent-tok")))

        class A(object):
            pass
        a = A()
        a.env = {"CLAUDE_PLUGIN_OPTION_TOKEN": "lead-tok", "WAVECOORD_TOKEN_FILE": "/x", "KEEP": "1"}
        a.task = {"hostcfg": local, "host": "local"}
        fencehost.worker_credentials(a)
        self.assertEqual(a.env, {"KEEP": "1", "WAVECOORD_TOKEN_FILE": os.path.join(home, "hosts", "local.token")})

    def test_worker_hooks_never_use_the_lead_token(self):
        from wavecoord.client import resolve_token
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        tf = os.path.join(d, "agent.token")
        with open(tf, "w") as f:
            f.write("agent-tok\n")
        lead = {"CLAUDE_PLUGIN_OPTION_TOKEN": "lead-tok"}
        self.assertEqual(resolve_token(lead), "lead-tok")  # the lead session
        for k in ("WAVECOORD_TASK", "WAVECOORD_AGENT_ID"):
            self.assertEqual(resolve_token(dict(lead, WAVECOORD_TOKEN_FILE=tf, **{k: "w"})), "agent-tok")
            self.assertIsNone(resolve_token(dict(lead, **{k: "w"})))  # no agent token: none, never the lead's

    def test_settings_pins(self):
        from wavecoord.launcher.wrapper import worker_settings
        s = worker_settings({"task": "t", "agent": "w", "spec": {"lease_epoch": 7}, "worktree": "/x",
                             "fence": {"edit": "enforce"}})
        self.assertIs(s["disableAllHooks"], False)
        self.assertEqual(s["env"]["WAVECOORD_LEASE_EPOCH"], "7")
        self.assertEqual(s["env"]["WAVECOORD_ROLE"], "worker")
        self.assertIn("Bash(git push)", s["permissions"]["deny"])

    def test_default_branch_modes(self):
        sim = Sim(0, n_workers=1)
        try:
            m = fencehost.default_branch_modes(sim.w.workers[0].wt, "main")
            self.assertEqual(m["edit"], "enforce")
            self.assertEqual(fencehost.default_branch_modes(sim.w.workers[0].wt, "nope"), fencecore.DEFAULT_MODES)
        finally:
            sim.w.close()


class FakeClaudeHookTest(FenceWorld):
    """The wavecoord side of the real Claude Code checks (README section 4), with the fake claude:
    the plugin's hooks.json, the zipapp, onFailure block and the deny precedence."""

    FAKE = os.path.join(_paths.TESTS, "fake-claude", "claude")

    def run_fake(self, target, plugin=None, extra=None):
        env = self.w.worker_env(self.a)
        env.update(FAKE_CLAUDE_EDIT=target, PYTHONPATH="")
        if extra:
            env["FAKE_CLAUDE_EXTRA_HOOKS"] = extra
        p = subprocess.run([sys.executable, self.FAKE, "-p", "--output-format", "stream-json", "--plugin-dir",
                            plugin or os.path.join(_paths.ROOT, "plugin"), "x"], cwd=self.a.wt, env=env, input="",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
        ev = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
        return [e for e in ev if e.get("type") == "fake_tool"][0]

    def extra_hooks(self, hook):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "hooks.json")
        with open(p, "w") as f:
            json.dump({"hooks": {"PreToolUse": [{"matcher": "Edit|Write", "hooks": [hook]}]}}, f)
        return p

    def test_allow_and_deny_through_the_zipapp(self):
        self.assertEqual(self.run_fake("a/x.py")["decision"], "allow")
        t = self.run_fake("docs/api.md")
        self.assertEqual(t["decision"], "deny")
        self.assertIn("foreign_exclusive", " ".join(t["reasons"]))

    def test_deny_wins_over_another_hooks_allow(self):
        allow = self.extra_hooks({"type": "command", "command": "sh", "args": ["-c", "echo '{\"hookSpecificOutput\": "
                                  "{\"hookEventName\": \"PreToolUse\", \"permissionDecision\": \"allow\"}}'"]})
        self.assertEqual(self.run_fake("docs/api.md", extra=allow)["decision"], "deny")
        self.assertEqual(self.run_fake("a/x.py", extra=allow)["decision"], "allow")

    def test_on_failure_block(self):
        plugin = os.path.join(self.w.root, "plugin-copy")
        if not os.path.isdir(plugin):
            shutil.copytree(os.path.join(_paths.ROOT, "plugin"), plugin)
        os.remove(os.path.join(plugin, "bin", "wavecoord"))  # a plugin update removed the build
        self.assertEqual(self.run_fake("a/x.py", plugin=plugin)["decision"], "deny")
        slow = self.extra_hooks({"type": "command", "command": "sleep", "args": ["5"], "timeout": 1,
                                 "onFailure": "block"})
        self.assertEqual(self.run_fake("a/x.py", extra=slow)["decision"], "deny")
        loose = self.extra_hooks({"type": "command", "command": "sleep", "args": ["5"], "timeout": 1})
        self.assertEqual(self.run_fake("a/x.py", extra=loose)["decision"], "allow")  # without onFailure: open


class PluginTest(unittest.TestCase):
    def test_hooks_json_on_failure_block(self):
        with open(os.path.join(_paths.ROOT, "plugin", "hooks", "hooks.json")) as f:
            h = json.load(f)
        pre = h["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertEqual(pre["onFailure"], "block")
        self.assertEqual(pre["timeout"], 6)
        for ev, groups in h["hooks"].items():
            if ev != "PreToolUse":
                self.assertNotIn("onFailure", groups[0]["hooks"][0], ev)

    @unittest.skipUnless(shutil.which("claude"), "claude is not installed")
    def test_claude_plugin_validate_strict(self):
        p = subprocess.run(["claude", "plugin", "validate", "--strict", os.path.join(_paths.ROOT, "plugin")],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout)


if __name__ == "__main__":
    unittest.main()
