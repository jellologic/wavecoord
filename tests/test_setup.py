"""`wavecoord init` and `wavecoord doctor` in a temp repo with a bare origin, with the fake claude and gh on PATH.

No network, no model: a small HTTP stub plays the coordinator."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import _paths
from _coord import git

from wavecoord import __version__

FAKE_CLAUDE = os.path.join(_paths.TESTS, "fake-claude")
FAKE_GH = os.path.join(_paths.TESTS, "fake-gh")


def read(path):
    with open(path) as f:
        return f.read()


class StubCoord(object):
    """/api/health and /api/config, with one accepted token."""

    def __init__(self, version=__version__, token=None):
        self.version, self.token = version, token
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.startswith("/api/health"):
                    return self.reply(200, {"ok": True, "api": 1, "schema": 1, "version": "wavecoord " + stub.version})
                if self.headers.get("Authorization") != "Bearer %s" % stub.token:
                    return self.reply(401, {"error": {"code": "unauthorized", "message": "bad token"}})
                if self.path == "/api/config":
                    return self.reply(200, {"config": {"plan": {"adapter": "builtin", "dir": "plan/tasks",
                                                                "selected_by": "no specs/ on the default branch"}}})
                return self.reply(404, {})

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


class SetupCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wc-setup-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.home)
        os.makedirs(self.bin)
        os.symlink(shutil.which("git"), os.path.join(self.bin, "git"))
        os.symlink(sys.executable, os.path.join(self.bin, "python3"))
        self.remote = os.path.join(self.tmp, "remote.git")
        self.repo = os.path.join(self.tmp, "myapp")
        git(self.tmp, "init", "-q", "--bare", self.remote)
        git(self.tmp, "init", "-q", self.repo)
        git(self.repo, "remote", "add", "origin", self.remote)
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")
        git(self.repo, "push", "-q", "-u", "origin", "main")
        git(self.repo, "remote", "set-head", "origin", "main")
        self.plugins = os.path.join(self.tmp, "plugins.json")
        self.options = os.path.join(self.tmp, "options.json")
        self.gh_state = os.path.join(self.tmp, "gh.json")

    def env(self, gh=True, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("WAVECOORD_", "CLAUDE", "FAKE_", "XDG_", "ANTHROPIC_", "GIT_", "NO_COLOR"))}
        path = [self.bin, FAKE_CLAUDE] + ([FAKE_GH] if gh else []) + ["/usr/bin", "/bin"]
        env.update(HOME=self.home, PATH=os.pathsep.join(path), PYTHONPATH=_paths.SRC, PYTHONDONTWRITEBYTECODE="1",
                   FAKE_CLAUDE_PLUGINS=self.plugins, FAKE_CLAUDE_PLUGIN_OPTIONS=self.options,
                   FAKE_GH_STATE=self.gh_state, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
        env.update(extra)
        return env

    def wc(self, *args, **kw):
        gh = kw.pop("gh", True)
        stdin = kw.pop("stdin", None)
        return subprocess.run([sys.executable, "-m", "wavecoord"] + list(args), cwd=kw.pop("cwd", self.repo),
                              env=self.env(gh=gh, **kw), input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=180)

    def config(self):
        with open(os.path.join(self.repo, ".claude", "wavecoord.json")) as f:
            return json.load(f)

    def files(self, *roots):
        out = {}
        for root in roots:
            for d, dirs, fs in os.walk(root):
                dirs[:] = [x for x in dirs if x != ".git" and x != "Caches"]
                for f in fs:
                    p = os.path.join(d, f)
                    with open(p, "rb") as fh:
                        out[p] = fh.read()
        return out

    def install_plugin(self, version=__version__, enabled=True):
        with open(self.plugins, "w") as f:
            json.dump([{"id": "wavecoord@wavecoord", "version": version, "scope": "user", "enabled": enabled}], f)

    def commit_config(self):
        git(self.repo, "add", ".claude/wavecoord.json")
        git(self.repo, "commit", "-q", "-m", "wavecoord config")
        git(self.repo, "push", "-q", "origin", "main")

    def doctor(self, *args, **kw):
        p = self.wc("doctor", "--json", "--quick", *args, **kw)
        data = json.loads(p.stdout)
        return p, {c["name"]: c for c in data["checks"]}, data


class InitTest(SetupCase):
    def test_workers_may_merge_for_a_resolver(self):
        from wavecoord import initrepo
        self.assertIn("Bash(git merge *)", initrepo.WORKER_TOOLS.split(","))

    def test_dry_run_writes_nothing(self):
        before = self.files(self.home, self.repo)
        p = self.wc("init", "--dry-run", "--server", "--specs", "--pr", "--configure-plugin")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.files(self.home, self.repo), before)
        self.assertIn("would write .claude/wavecoord.json", p.stdout)
        self.assertIn("would make the lead token", p.stdout)
        self.assertIn("would install the server service", p.stdout)
        self.assertIn("would create specs/README.md", p.stdout)
        self.assertIn("would commit .claude/wavecoord.json, specs/README.md on wavecoord/setup", p.stdout)
        self.assertIn("nothing was written", p.stdout)
        self.assertFalse(os.path.exists(self.gh_state))

    def test_writes_files_and_is_idempotent(self):
        p = self.wc("init", "--no-doctor")
        self.assertEqual(p.returncode, 0, p.stderr)
        cfg = self.config()
        self.assertEqual(cfg["project"], "remote")  # the origin repo's name (remote.git)
        self.assertEqual(cfg["coord_url"], "http://127.0.0.1:18741")
        self.assertEqual(cfg["fence"], "advisory")
        self.assertEqual(cfg["wip_cap"], 2)
        self.assertEqual([h["name"] for h in cfg["hosts"]], ["local"])
        wc = os.path.join(self.home, ".wavecoord")
        tokens = json.loads(read(os.path.join(wc, "tokens.json")))["tokens"]
        roles = sorted((r["role"], r["principal"]) for r in tokens.values())
        self.assertEqual(roles, [("agent", "host:local"), ("lead", "lead")])
        for rel in ("token", os.path.join("hosts", "local.token")):
            mode = stat.S_IMODE(os.stat(os.path.join(wc, rel)).st_mode)
            self.assertEqual(mode, 0o600, rel)
        lead = read(os.path.join(wc, "token")).strip()
        self.assertNotIn(lead, p.stdout + p.stderr)  # never echoed without --show-token
        self.assertIn("coord_url  http://127.0.0.1:18741", p.stdout)
        self.assertIn("git add .claude/wavecoord.json && git commit", p.stdout)

        snap = self.files(self.home, self.repo)
        p = self.wc("init", "--no-doctor")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.files(self.home, self.repo), snap)
        self.assertIn(".claude/wavecoord.json: up to date", p.stdout)
        self.assertIn("the lead token: kept", p.stdout)

        p = self.wc("init", "--no-doctor", "--show-token")
        self.assertIn(lead, p.stdout)

    def test_merges_into_existing_config(self):
        os.makedirs(os.path.join(self.repo, ".claude"))
        existing = {"project": "demo", "check_cmd": "make test",
                    "hosts": [{"name": "build-1", "ssh": "builder@build-1.example.internal", "slots": 4,
                               "repo_path": "~/src/demo"}]}
        with open(os.path.join(self.repo, ".claude", "wavecoord.json"), "w") as f:
            json.dump(existing, f)
        p = self.wc("init", "--no-doctor", "--project", "other", "--fence", "enforce")
        self.assertEqual(p.returncode, 0, p.stderr)
        cfg = self.config()
        for k, v in existing.items():
            self.assertEqual(cfg[k], v)
        self.assertEqual(cfg["coord_url"], "http://127.0.0.1:18741")
        self.assertEqual(cfg["fence"], "enforce")
        self.assertNotIn("wip_cap", cfg)  # its default follows the hosts that are there
        self.assertIn('kept project = "demo"', p.stdout)
        self.assertIn("ssh host build-1 has no token yet", p.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".wavecoord", "hosts", "build-1.token")))

    def test_bad_existing_config_is_not_touched(self):
        os.makedirs(os.path.join(self.repo, ".claude"))
        path = os.path.join(self.repo, ".claude", "wavecoord.json")
        with open(path, "w") as f:
            f.write("{not json")
        p = self.wc("init", "--no-doctor")
        self.assertEqual(p.returncode, 1)
        self.assertIn("not valid JSON", p.stderr)
        self.assertEqual(read(path), "{not json")

    def test_never_overwrites_tokens(self):
        wc = os.path.join(self.home, ".wavecoord")
        os.makedirs(os.path.join(wc, "hosts"))
        for rel, text in (("token", "mine\n"), (os.path.join("hosts", "local.token"), "agent-mine\n")):
            with open(os.path.join(wc, rel), "w") as f:
                f.write(text)
            os.chmod(os.path.join(wc, rel), 0o600)
        p = self.wc("init", "--no-doctor")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(read(os.path.join(wc, "token")), "mine\n")
        self.assertEqual(read(os.path.join(wc, "hosts", "local.token")), "agent-mine\n")
        self.assertFalse(os.path.exists(os.path.join(wc, "tokens.json")))

    def test_specs_pr_and_plugin_options(self):
        self.install_plugin()
        p = self.wc("init", "--no-doctor", "--specs", "--pr", "--configure-plugin")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "specs", "README.md")))
        # the PR branch has both files; the default branch and the user's checkout are untouched
        files = git(self.repo, "ls-tree", "-r", "--name-only", "origin/wavecoord/setup").split()
        self.assertEqual(sorted(files), [".claude/wavecoord.json", "specs/README.md"])
        self.assertEqual(git(self.remote, "ls-tree", "-r", "--name-only", "main").split(), [])
        prs = json.loads(read(self.gh_state))["prs"]
        self.assertEqual([(x["head"], x["base"]) for x in prs], [("wavecoord/setup", "main")])
        self.assertIn("PR: https://example.invalid/pull/1", p.stdout)
        opts = json.loads(read(self.options))
        lead = read(os.path.join(self.home, ".wavecoord", "token")).strip()
        self.assertEqual(opts, {"coord_url": "http://127.0.0.1:18741", "lead": "true", "token": lead})
        self.assertNotIn(lead, p.stdout + p.stderr)
        # doctor points to that PR; a second --pr opens no second PR for the same config
        p, c, _ = self.doctor()
        self.assertEqual(c["config"]["fix"], "merge the PR of branch wavecoord/setup, then: git pull")
        p = self.wc("init", "--no-doctor", "--pr")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("branch wavecoord/setup on origin has this config already", p.stdout)
        self.assertEqual(len(json.loads(read(self.gh_state))["prs"]), 1)
        # a changed config gets a new branch name
        cfg = self.config()
        cfg["check_cmd"] = "make test"
        with open(os.path.join(self.repo, ".claude", "wavecoord.json"), "w") as f:
            json.dump(cfg, f)
        p = self.wc("init", "--no-doctor", "--pr")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("wavecoord/setup-2", p.stdout)

    def test_runs_doctor_at_the_end(self):
        p = self.wc("init")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("wavecoord doctor:", p.stdout)
        self.assertIn("config", p.stdout)
        self.assertIn("not on origin/main", p.stdout)


class DoctorTest(SetupCase):
    def test_fresh_machine_failures_and_fixes(self):
        p, c, data = self.doctor(gh=False, FAKE_CLAUDE_LOGGED_IN="0")
        self.assertEqual(p.returncode, 1)
        self.assertFalse(data["ok"])
        self.assertEqual(c["python"]["status"], "ok")
        self.assertEqual(c["default branch"]["status"], "ok")
        self.assertEqual(c["gh"]["status"], "fail")
        self.assertIn("gh auth login", c["gh"]["fix"])
        self.assertEqual((c["claude"]["status"], c["claude"]["fix"]), ("fail", "claude auth login"))
        self.assertEqual(c["plugin"]["status"], "fail")
        self.assertIn("claude plugin install wavecoord@wavecoord", c["plugin"]["fix"])
        self.assertEqual((c["config"]["status"], c["config"]["fix"]), ("fail", "wavecoord init"))
        self.assertEqual(c["coord_url"]["status"], "fail")
        self.assertEqual(c["lead token"]["status"], "fail")
        self.assertIn("wavecoord tokens provision --role lead", c["lead token"]["fix"])
        self.assertEqual(c["service"]["status"], "warn")
        # text output: one line per check, a fix line under each problem
        p = self.wc("doctor", "--quick", gh=False, FAKE_CLAUDE_LOGGED_IN="0")
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("\033[", p.stdout)  # no color without a terminal
        lines = p.stdout.splitlines()
        i = [n for n, l in enumerate(lines) if l.startswith("fail") and " config " in l][0]
        self.assertEqual(lines[i + 1].split(), ["fix:", "wavecoord", "init"])
        self.assertRegex(lines[-1], r"^\d+ failed, \d+ warnings, \d+ ok$")
        p = self.wc("doctor", "--quick", "--color", "always", gh=False)
        self.assertIn("\033[31mfail", p.stdout)

    def test_no_origin(self):
        git(self.repo, "remote", "set-url", "origin", "https://someone:hunter2@example.invalid/x.git")
        p, c, _ = self.doctor()
        self.assertEqual(c["origin"]["detail"], "https://***@example.invalid/x.git")
        self.assertNotIn("hunter2", p.stdout)
        git(self.repo, "remote", "remove", "origin")
        p, c, _ = self.doctor()
        self.assertEqual(c["origin"]["status"], "fail")
        self.assertIn("git remote add origin", c["origin"]["fix"])
        self.assertNotIn("default branch", c)

    def test_all_good_after_init(self):
        self.install_plugin()
        p = self.wc("init", "--no-doctor", "--configure-plugin")
        self.assertEqual(p.returncode, 0, p.stderr)
        lead = read(os.path.join(self.home, ".wavecoord", "token")).strip()
        coord = StubCoord(token=lead)
        self.addCleanup(coord.stop)
        cfg = self.config()
        cfg["coord_url"] = coord.url
        with open(os.path.join(self.repo, ".claude", "wavecoord.json"), "w") as f:
            json.dump(cfg, f, indent=2)
        with open(self.options) as f:
            opts = json.load(f)
        opts["coord_url"] = coord.url
        with open(self.options, "w") as f:
            json.dump(opts, f)
        p, c, _ = self.doctor()
        self.assertEqual(c["config"]["status"], "fail")
        self.assertIn("wavecoord init --pr", c["config"]["fix"])
        self.commit_config()
        p, c, data = self.doctor()
        self.assertEqual(p.returncode, 0, json.dumps(data, indent=1))
        for name in ("gh", "claude", "plugin", "plugin options", "config", "coord_url", "coordinator", "token",
                     "lead token", "host token local", "plan"):
            self.assertEqual(c[name]["status"], "ok", (name, c[name]))
        self.assertIn("from the coordinator", c["plan"]["detail"])
        self.assertIn("/wavecoord:spec", c["plan"]["detail"])
        self.assertEqual(c["hosts"]["status"], "warn")  # --quick

        # the coordinator runs another build, and refuses a changed token
        coord.version = "0.0.1"
        coord.token = "other"
        p, c, _ = self.doctor()
        self.assertEqual(p.returncode, 1)
        self.assertEqual(c["coordinator"]["status"], "warn")
        self.assertIn("wavecoord install-server", c["coordinator"]["fix"])
        self.assertEqual(c["token"]["status"], "fail")
        coord.stop()
        p, c, _ = self.doctor()
        self.assertEqual(c["coordinator"]["status"], "fail")
        self.assertIn("wavecoord install-server", c["coordinator"]["fix"])
        self.assertNotIn("token", c)

    def test_token_problems(self):
        self.wc("init", "--no-doctor")
        wc = os.path.join(self.home, ".wavecoord")
        os.chmod(os.path.join(wc, "token"), 0o644)
        os.remove(os.path.join(wc, "hosts", "local.token"))
        p, c, _ = self.doctor()
        self.assertEqual(c["lead token"]["status"], "fail")
        self.assertEqual(c["lead token"]["fix"], "chmod 600 ~/.wavecoord/token")
        self.assertEqual(c["host token local"]["status"], "warn")
        self.assertIn("--role agent --host local", c["host token local"]["fix"])
        with open(os.path.join(wc, "hosts", "local.token"), "w") as f:
            f.write("unknown\n")
        os.chmod(os.path.join(wc, "hosts", "local.token"), 0o600)
        p, c, _ = self.doctor()
        self.assertEqual(c["host token local"]["status"], "fail")
        self.assertIn("is not in", c["host token local"]["detail"])

    def test_plugin_states(self):
        self.install_plugin(version="0.0.1")
        p, c, _ = self.doctor()
        self.assertEqual(c["plugin"]["status"], "warn")
        self.assertIn("claude plugin update wavecoord@wavecoord", c["plugin"]["fix"])
        self.assertEqual(c["plugin options"]["status"], "fail")
        self.assertIn("wavecoord init --configure-plugin", c["plugin options"]["fix"])
        self.install_plugin(enabled=False)
        p, c, _ = self.doctor()
        self.assertEqual((c["plugin"]["status"], c["plugin"]["fix"]),
                         ("fail", "claude plugin enable wavecoord@wavecoord"))

    def test_custom_pr_cmd_needs_no_gh_and_service_pin(self):
        os.makedirs(os.path.join(self.repo, ".claude"))
        with open(os.path.join(self.repo, ".claude", "wavecoord.json"), "w") as f:
            json.dump({"project": "demo", "pr_cmd": "my-forge pr open", "merge_cmd": ["my-forge", "merge"],
                       "pr_state_cmd": ["my-forge", "state"]}, f)
        if sys.platform == "darwin":
            unit = os.path.join(self.home, "Library", "LaunchAgents", "io.github.wavecoord.demo.plist")
        else:
            unit = os.path.join(self.home, ".config", "systemd", "user", "wavecoord-demo.service")
        os.makedirs(os.path.dirname(unit))
        with open(unit, "w") as f:
            f.write("ExecStart=python3 -I /x/.wavecoord/bin/wavecoord-0123456789abcdef serve\n")
        p, c, _ = self.doctor(gh=False)
        # the refinery's fence check still reads the PR body with gh pr view: needed, not "not needed"
        self.assertEqual(c["gh"]["status"], "warn")
        self.assertIn("gh pr view", c["gh"]["detail"])
        self.assertIn("Other forges", c["gh"]["fix"])
        self.assertEqual(c["service"]["status"], "warn")
        self.assertIn("runs build 0123456789abcdef", c["service"]["detail"])
        p, c, _ = self.doctor()
        self.assertEqual(c["gh"]["status"], "ok")
        self.assertIn("used for the refinery's fence check", c["gh"]["detail"])
        # fence merge mode enforce holds every merge whose PR body it cannot read: a failure
        with open(os.path.join(self.repo, ".claude", "wavecoord.json"), "w") as f:
            json.dump({"project": "demo", "pr_cmd": "my-forge pr open", "merge_cmd": ["my-forge", "merge"],
                       "pr_state_cmd": ["my-forge", "state"], "fence": {"merge": "enforce"}}, f)
        p, c, _ = self.doctor(gh=False)
        self.assertEqual(c["gh"]["status"], "fail")
        self.assertIn("enforce", c["gh"]["detail"])

    def test_gh_not_logged_in_still_points_at_other_forges(self):
        """CI runners have a gh that is not logged in: the fix line keeps the pointer to "Other forges"."""
        os.makedirs(os.path.join(self.repo, ".claude"))
        with open(os.path.join(self.repo, ".claude", "wavecoord.json"), "w") as f:
            json.dump({"project": "demo", "pr_cmd": "my-forge pr open", "merge_cmd": ["my-forge", "merge"],
                       "pr_state_cmd": ["my-forge", "state"]}, f)
        gh = os.path.join(self.bin, "gh")
        with open(gh, "w") as f:
            f.write("#!/bin/sh\necho 'You are not logged into any GitHub hosts.' >&2\nexit 1\n")
        os.chmod(gh, 0o755)
        p, c, _ = self.doctor(gh=False)
        self.assertEqual(c["gh"]["status"], "warn")
        self.assertIn("not logged in", c["gh"]["detail"])
        self.assertIn("Other forges", c["gh"]["fix"])

    def test_host_probe(self):
        self.wc("init", "--no-doctor")
        p = self.wc("doctor", "--json", CLAUDE_CODE_OAUTH_TOKEN="fake-not-a-secret")
        c = {x["name"]: x for x in json.loads(p.stdout)["checks"]}
        self.assertEqual(c["host local"]["status"], "ok", c["host local"])
        self.assertIn("slots", c["host local"]["detail"])


class SetupPathsTest(SetupCase):
    """Course findings: WAVECOORD_HOME, the service port, the plugin id of a --plugin-dir session."""

    def test_host_tokens_follow_wavecoord_home(self):
        wch = os.path.join(self.tmp, "wc-root")
        p = self.wc("init", "--no-doctor", WAVECOORD_HOME=wch)
        self.assertEqual(p.returncode, 0, p.stderr)
        # the launcher reads <WAVECOORD_HOME>/hosts/<host>.token: init writes it there
        from wavecoord.launcher.common import host_token_file
        self.assertEqual(host_token_file("local", {"WAVECOORD_HOME": wch}), os.path.join(wch, "hosts", "local.token"))
        self.assertTrue(os.path.isfile(os.path.join(wch, "hosts", "local.token")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".wavecoord", "hosts")))
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".wavecoord", "token")))  # the lead's stays
        p, c, _ = self.doctor(WAVECOORD_HOME=wch)
        self.assertEqual(c["host token local"]["status"], "ok", c["host token local"])
        self.assertIn(os.path.join(wch, "hosts", "local.token"), c["host token local"]["detail"])
        self.assertIn("launcher root %s" % wch, c["homes"]["detail"])
        self.assertIn("server state of remote: %s (WAVECOORD_HOME)" % wch, c["homes"]["detail"])
        # without it, doctor looks in ~/.wavecoord/hosts/, says so, and finds no token there
        p, c, _ = self.doctor()
        self.assertEqual(c["host token local"]["status"], "warn")
        self.assertIn("~/.wavecoord/hosts/local.token", c["host token local"]["detail"])
        self.assertIn("launcher root ~/.wavecoord (hosts/, attempts/, client/; default)", c["homes"]["detail"])
        self.assertIn("server state of remote: ~/.wavecoord/remote (default)", c["homes"]["detail"])

    def test_server_port_from_coord_url(self):
        from unittest import mock
        from wavecoord import initrepo, installsrv
        self.assertEqual(initrepo.serve_address("http://127.0.0.1:18800"), (18800, None))
        self.assertEqual(initrepo.serve_address("http://localhost:18741"), (None, None))
        port, note = initrepo.serve_address("http://coord.example.internal:18800")
        self.assertEqual(port, 18800)
        self.assertIn("--bind <address> --port 18800", note)
        self.assertEqual(initrepo.serve_address("https://coord.example.internal")[0], None)
        p = self.wc("init", "--no-doctor", "--dry-run", "--server", "--coord-url", "http://127.0.0.1:18800")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("(wavecoord install-server --port 18800)", p.stdout)
        # the installed unit runs serve --port <the URL's port>
        seen = {}

        def fake_plan(repo, **kw):
            seen.clear()
            seen.update(kw)
            raise ValueError("stop here")
        with open(os.devnull, "w") as devnull:
            init = initrepo.Init(mock.Mock(dry_run=False), out=devnull)
            init.top = self.repo
            with mock.patch.object(installsrv, "plan", fake_plan):
                init.step_server({"coord_url": "http://127.0.0.1:18800"})
                self.assertEqual(seen.get("port"), 18800)
                init.step_server({"coord_url": "http://127.0.0.1:18741"})
                self.assertIsNone(seen.get("port"))
        argv = installsrv.plan(self.repo, platform="linux", port=18800, zipapp_bytes=b"x")["argv"]
        self.assertEqual(argv[argv.index("--port") + 1], "18800")

    def test_inline_plugin(self):
        plugin_dir = os.path.join(_paths.ROOT, "plugin")
        log = os.path.join(self.tmp, "configure.log")
        # nothing installed, no hint: not installed, and the message names the --plugin-id way
        p, c, _ = self.doctor()
        self.assertEqual(c["plugin"]["status"], "fail")
        self.assertIn("--plugin-id wavecoord@inline", c["plugin"]["detail"])
        # a claude --plugin-dir session: CLAUDE_PLUGIN_ROOT names the plugin folder
        p, c, _ = self.doctor(CLAUDE_PLUGIN_ROOT=plugin_dir, FAKE_CLAUDE_CONFIGURE_LOG=log)
        self.assertEqual(c["plugin"]["status"], "ok", c["plugin"])
        self.assertIn("wavecoord@inline", c["plugin"]["detail"])
        self.assertIn("--plugin-id wavecoord@inline", c["plugin options"]["fix"])
        self.assertIn("wavecoord@inline --json", read(log))
        # or the flag
        p, c, _ = self.doctor("--plugin-id", "wavecoord@inline")
        self.assertEqual(c["plugin"]["status"], "ok", c["plugin"])
        p, c, _ = self.doctor("--plugin-id", "wavecoord@elsewhere")
        self.assertEqual(c["plugin"]["status"], "fail")
        self.assertIn("no plugin wavecoord@elsewhere", c["plugin"]["detail"])
        # init --configure-plugin --plugin-id configures that id
        os.remove(log)
        p = self.wc("init", "--no-doctor", "--configure-plugin", "--plugin-id", "wavecoord@inline",
                    FAKE_CLAUDE_CONFIGURE_LOG=log)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Plugin options for wavecoord@inline", p.stdout)
        self.assertEqual(read(log), "wavecoord@inline --values-stdin\n")
        self.assertEqual(json.loads(read(self.options))["lead"], "true")
        # and finds it by itself in a --plugin-dir session
        os.remove(log)
        p = self.wc("init", "--no-doctor", "--configure-plugin", CLAUDE_PLUGIN_ROOT=plugin_dir,
                    FAKE_CLAUDE_CONFIGURE_LOG=log)
        self.assertEqual(read(log), "wavecoord@inline --values-stdin\n")
        # an installed plugin keeps its id
        self.install_plugin()
        os.remove(log)
        p = self.wc("init", "--no-doctor", "--configure-plugin", FAKE_CLAUDE_CONFIGURE_LOG=log)
        self.assertEqual(read(log), "wavecoord@wavecoord --values-stdin\n")


if __name__ == "__main__":
    unittest.main()
