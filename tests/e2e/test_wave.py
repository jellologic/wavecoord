"""Localhost end to end: one wave from plan to merge, driven like a lead Claude session.

A real `wavecoord serve` (the zipapp built from this tree) runs on an ephemeral
port with a tokens file. The consumer repo has a bare `origin`, a builtin plan
in `plan/`, one local host with 3 slots, `pr_cmd` and `merge_cmd` that act on
the bare repo (tests/fake-gh) and the fake `claude` on PATH, steered per task
by FAKE_CLAUDE_MODES. The steps follow (a)-(j) of the scenario; the refinery
step (g) runs twice, before and after the conflict of (f). The lead side talks to the server only as Claude would:
MCP `tools/call` over HTTP, the Monitor command `wavecoord events --follow
--for lead`, and the `wavecoord` CLI. No model, no network.
"""

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import _paths

FAKE_CLAUDE_DIR = os.path.join(_paths.TESTS, "fake-claude")
FAKE_GH = os.path.join(_paths.TESTS, "fake-gh", "gh")
PY = sys.executable
CHANGELOG = "CHANGELOG.md"


def sh(args, cwd, env=None, check=True):
    p = subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, timeout=120)
    if check and p.returncode != 0:
        raise AssertionError("%s failed (%d): %s %s" % (" ".join(args), p.returncode, p.stdout, p.stderr))
    return p.stdout.strip()


def spec(tid, paths, needs=(), shared=(), provider=None, fallback=None, prompt=None):
    ex = ["  model: sonnet", "  effort: low", "  max_turns: 5", "  budget_usd: 1", "  timeout_min: 2"]
    if provider:
        ex.append("  provider: %s" % provider)
    if fallback:
        ex.append("  provider_fallback: [%s]" % ", ".join(fallback))
    lines = ["---", "id: %s" % tid, "title: Task %s" % tid, "phase: p1", "needs: [%s]" % ", ".join(needs),
             "paths: [%s]" % ", ".join(paths)]
    if shared:
        lines.append("shared_paths: [%s]" % ", ".join(shared))
    lines += ["class: B", "size: S", "state: todo", "exec:"] + ex + [
        "---", "", "## Context", "Context of %s." % tid, "", "## Risks", "- risk: scope creep",
        "  mitigation: paths are reserved", "  kind: llm", "", "## Acceptance", "- `true`", "",
        "## Prompt", prompt or "Do task %s." % tid, ""]
    return "\n".join(lines)


class Monitor(object):
    """`wavecoord events --follow --for lead`, as the wave-run skill arms it; collects the lines."""

    def __init__(self, argv, env, cwd):
        self.p = subprocess.Popen(argv, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  universal_newlines=True)
        self.lines = []
        self.q = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            self.lines.append(line.rstrip("\n"))
            self.q.put(line)

    def wait_for(self, *needles, timeout=60):
        """Wait until every needle is in some line. -> the matching lines."""
        end = time.time() + timeout
        while time.time() < end:
            hits = [next((l for l in self.lines if n in l), None) for n in needles]
            if all(hits):
                return hits
            try:
                self.q.get(timeout=0.2)
            except queue.Empty:
                pass
        raise AssertionError("monitor lines never showed %s; got:\n%s" % (needles, "\n".join(self.lines)))

    def stop(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(10)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait(5)
        self.p.stdout.close()
        self.p.stderr.close()


class WaveE2E(unittest.TestCase):
    maxDiff = None

    # ----- setup: a consumer repo, a host checkout, tokens, the server -----

    def setUp(self):
        self.t0 = time.time()
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wc-e2e-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        t = self.tmp
        self.home = os.path.join(t, "home")
        self.wc = os.path.join(self.home, ".wavecoord")
        os.makedirs(os.path.join(self.home, ".claude"))
        with open(os.path.join(self.home, ".claude", ".credentials.json"), "w") as f:
            f.write("{}\n")  # the host probe only checks that claude has credentials
        gitcfg = os.path.join(t, "gitconfig")
        with open(gitcfg, "w") as f:
            f.write("[user]\n\tname = e2e\n\temail = e2e@example.invalid\n[init]\n\tdefaultBranch = main\n"
                    "[commit]\n\tgpgsign = false\n")
        # the zipapp a user runs, built from this tree
        bindir = os.path.join(t, "bin")
        os.makedirs(bindir)
        from wavecoord import zipapp
        self.zipapp = os.path.join(bindir, "wavecoord")
        with open(self.zipapp, "wb") as f:
            f.write(zipapp.build_bytes())
        os.chmod(self.zipapp, 0o755)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("WAVECOORD_", "CLAUDE", "ANTHROPIC_", "FAKE_", "GIT_")) and k != "API_TIMEOUT_MS"}
        self.modes = os.path.join(t, "modes.json")
        self.runs = os.path.join(t, "runs.jsonl")
        self.gh_state = os.path.join(t, "gh.json")
        self.gate = os.path.join(t, "docs-gate")
        self.wave_gate = os.path.join(t, "wave-gate")
        env.update({
            "HOME": self.home, "PATH": os.pathsep.join([FAKE_CLAUDE_DIR, bindir, os.environ.get("PATH", "")]),
            "GIT_CONFIG_GLOBAL": gitcfg, "GIT_CONFIG_NOSYSTEM": "1",
            "WAVECOORD_PLUGIN_DIR": os.path.join(_paths.ROOT, "plugin"),
            "WAVECOORD_HB_INTERVAL_S": "0.5", "WAVECOORD_POLL_S": "0.2", "WAVECOORD_PLAN_FETCH_S": "0",
            "WAVECOORD_STOP_GRACE_S": "2", "WAVECOORD_PUSH_RETRY_S": "1", "WAVECOORD_NOTIFY_MIN_INTERVAL_S": "0",
            "WAVECOORD_WATCHDOG_S": "0", "WAVECOORD_SSE_PING_S": "1",
            "FAKE_CLAUDE_MODES": self.modes, "FAKE_CLAUDE_RUNS": self.runs, "FAKE_GH_STATE": self.gh_state,
            "FAKE_CLAUDE_TRANSCRIPTS": "1",
        })
        self.env = env
        self.write_modes({})
        # origin (bare) with the consumer config on main
        self.origin = os.path.join(t, "origin.git")
        sh(["git", "init", "-q", "--bare", "-b", "main", self.origin], t, env)
        self.repo = os.path.join(t, "repo")  # the lead machine's checkout: the server runs here
        sh(["git", "clone", "-q", self.origin, self.repo], t, env)
        self.host_repo = os.path.join(t, "hostrepo")  # repo_path of the local host
        alt_env = os.path.join(t, "providers", "alt.env")
        os.makedirs(os.path.dirname(alt_env))
        with open(alt_env, "w") as f:
            f.write("FAKE_PROVIDER=alt\n")
        os.chmod(alt_env, 0o600)
        cfg = {
            "project": "demo", "branch_prefix": "wave", "wip_cap": 3, "auto_resolve": False,
            "plan": {"adapter": "builtin", "dir": "plan"},
            "hosts": [{"name": "local", "slots": 3, "repo_path": self.host_repo}],
            "providers": {"alt": {"env_file": alt_env, "cost_basis": "none"}},
            "pr_cmd": [PY, FAKE_GH, "pr", "create", "--head", "{branch}", "--base", "{base}", "--title", "{title}",
                       "--body-file", "{body_file}"],
            "merge_cmd": [PY, FAKE_GH, "pr", "merge", "{pr}", "--merge", "--match-head-commit", "{sha}"],
        }
        os.makedirs(os.path.join(self.repo, ".claude"))
        with open(os.path.join(self.repo, ".claude", "wavecoord.json"), "w") as f:
            json.dump(cfg, f, indent=1)
        with open(os.path.join(self.repo, CHANGELOG), "w") as f:
            f.write("# Changelog\n")
        sh(["git", "add", "-A"], self.repo, env)
        sh(["git", "commit", "-q", "-m", "wavecoord config"], self.repo, env)
        sh(["git", "push", "-q", "origin", "main"], self.repo, env)
        sh(["git", "clone", "-q", self.origin, self.host_repo], t, env)
        # tokens, as the quick start makes them: the lead token for the lead's CLI and Monitor, and an
        # agent token for the workers of the local host
        self.tokens = os.path.join(self.wc, "tokens.json")
        self.lead_token_file = os.path.join(self.wc, "token")
        self.wcli("tokens", "provision", "--role", "lead", "--tokens", self.tokens, "--out", self.lead_token_file)
        os.makedirs(os.path.join(self.wc, "hosts"))
        self.wcli("tokens", "provision", "--role", "agent", "--host", "local", "--tokens", self.tokens,
                  "--out", os.path.join(self.wc, "hosts", "local.token"))
        with open(self.lead_token_file) as f:
            self.lead_token = f.read().strip()
        self.start_server()

    def start_server(self):
        log = open(os.path.join(self.tmp, "serve.log"), "a")
        self.addCleanup(log.close)
        self.srv = subprocess.Popen([PY, "-I", self.zipapp, "serve", "--repo", self.repo, "--port", "0",
                                     "--tokens", self.tokens], cwd=self.repo, env=self.env,
                                    stdout=subprocess.PIPE, stderr=log, universal_newlines=True)
        self.addCleanup(self.stop_server)
        line = self.srv.stdout.readline()
        if not line.startswith("wavecoord serve: http://"):
            self.fail("serve did not start: %r\n%s" % (line, self.serve_log()))
        self.url = line.split()[2]
        self.env["WAVECOORD_URL"] = self.url  # for the CLI calls of the lead; the server itself does not use it

    def stop_server(self):
        if self.srv.poll() is None:
            self.srv.send_signal(signal.SIGTERM)
            try:
                self.srv.wait(20)
            except subprocess.TimeoutExpired:
                self.srv.kill()
                self.srv.wait(5)
        self.srv.stdout.close()

    def serve_log(self):
        with open(os.path.join(self.tmp, "serve.log")) as f:
            return f.read()[-4000:]

    # ----- the lead's means: CLI, REST, MCP -----

    def wcli(self, *args, check=True, token_file=None):
        env = dict(self.env, WAVECOORD_TOKEN_FILE=token_file or os.path.join(self.wc, "token"))
        p = subprocess.run([PY, "-I", self.zipapp] + list(args), cwd=self.repo, env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=300)
        if check and p.returncode != 0:
            self.fail("wavecoord %s: exit %d\n%s\n%s" % (" ".join(args), p.returncode, p.stdout, p.stderr))
        return p

    def rest(self, method, path, body=None, token=None):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + (token or self.lead_token),
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "null")

    def rpc(self, method, params=None, rid=1):
        body = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            body["params"] = params
        req = urllib.request.Request(self.url + "/mcp", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": "Bearer " + self.lead_token,
                                              "Content-Type": "application/json",
                                              "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode())

    def tool(self, name, args=None, ok=True):
        """One MCP tools/call, as Claude Code sends it. -> the parsed JSON (or text) result."""
        res = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        self.assertNotIn("error", res, res)
        text = res["result"]["content"][0]["text"]
        try:
            out = json.loads(text)
        except ValueError:
            out = text
        if ok:
            self.assertFalse(res["result"]["isError"], "%s(%s) failed: %s" % (name, args, text))
        return out

    # ----- helpers -----

    def write_modes(self, modes):
        tmp = self.modes + ".tmp"
        with open(tmp, "w") as f:
            json.dump(modes, f)
        os.replace(tmp, self.modes)

    def gh(self):
        with open(self.gh_state) as f:
            return json.load(f)

    def fake_merge(self, n):
        """The user merges a plan PR (not a refinery item) with the same fake gh."""
        sh([PY, FAKE_GH, "pr", "merge", str(n), "--merge"], self.repo, self.env)

    def runs_log(self):
        try:
            with open(self.runs) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def events(self, kind=None):
        """Every event so far, through the CLI reader of the log branch (`wavecoord log --json`)."""
        out = self.wcli("log", "--repo", self.repo, "--json").stdout
        evs = [json.loads(l) for l in out.splitlines() if l.strip()]
        return [e for e in evs if kind is None or e["kind"] == kind]

    def collect_until(self, ids, timeout=90):
        """Call the collect tool until every attempt id in `ids` is collected. -> {id: item}."""
        got, end = {}, time.time() + timeout
        while time.time() < end:
            out = self.tool("collect")
            for c in out["collected"]:
                self.assertNotIn("error", c, c)
                got[c["id"]] = c
            if set(ids) <= set(got):
                return got
            time.sleep(0.3)
        self.fail("not collected in time: %s (got %s)\n%s" % (ids, sorted(got), self.serve_log()))

    def step(self, name):
        sys.stderr.write("  [e2e %5.1fs] %s\n" % (time.time() - self.t0, name))
        sys.stderr.flush()

    # ----- the scenario -----

    def test_wave(self):
        # Claude Code's handshake and tool list
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "claude-code", "version": "e2e"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "wavecoord")
        names = [t["name"] for t in self.rpc("tools/list")["result"]["tools"]]
        for want in ("plan_edit", "plan_ready", "dispatch_wave", "collect", "resolve", "refinery_merge",
                     "proposal_decide"):
            self.assertIn(want, names)
        mon = Monitor([PY, "-I", self.zipapp, "events", "--follow", "--for", "lead", "--url", self.url,
                       "--state-dir", os.path.join(self.tmp, "lead-state")],
                      dict(self.env, WAVECOORD_TOKEN_FILE=self.lead_token_file), self.repo)
        self.addCleanup(mon.stop)

        # (a) plan_edit creates 4 specs in one plan PR; the user merges it
        self.step("a: plan PR")
        specs = {"api": spec("api", ["api/**"], shared=[CHANGELOG]),
                 "docs": spec("docs", ["docs/**"], shared=[CHANGELOG]),
                 "web": spec("web", ["web/**"]),
                 "release": spec("release", ["release/**"], needs=["api"], provider="alt", fallback=["anthropic"])}
        out = self.tool("plan_edit", {"edits": [{"op": "create", "id": k, "text": v} for k, v in specs.items()],
                                      "title": "plan: wave 1"})
        self.assertEqual(out["branch"], "plan/1")
        self.assertEqual(sorted(out["files"]), ["plan/api.md", "plan/docs.md", "plan/release.md", "plan/web.md"])
        self.assertEqual(out["pr"], "https://example.invalid/pull/1")
        self.assertEqual(self.gh()["prs"][0]["head"], "plan/1")
        self.assertNotIn("plan", sh(["git", "ls-tree", "--name-only", "main"], self.origin).split())
        self.fake_merge(1)
        self.assertEqual(self.tool("plan_validate")["ok"], True)

        # (b) plan_ready: 3 ready tasks with disjoint paths, 1 blocked by its needs
        self.step("b: plan_ready")
        ready = self.tool("plan_ready")
        self.assertEqual(sorted(t["id"] for t in ready["tasks"]), ["api", "docs", "web"])
        self.assertEqual(ready["why_not"], {"release": "needs not done: api"})
        self.assertEqual(ready["wip"], {"in_flight": 0, "cap": 3})

        # (c) dispatch_wave: 3 attempts in 3 worktrees, disjoint exclusive reservations
        self.step("c: dispatch_wave")
        self.write_modes({
            "api": {"wait_for": self.wave_gate, "wait_steer": 1, "echo_steer": True,
                    "commit": {"api/server.py": "print('api')\n", CHANGELOG: "# Changelog\n- api\n"}},
            "web": {"wait_for": self.wave_gate, "commit": {"web/index.html": "<p>web</p>\n"},
                    "send": "web: index page is in",
                    "report": {"proposals": [{"kind": "new_task", "task": "web",
                                              "summary": "Add a smoke test for the web page"}]}},
            "docs": {"wait_for": self.gate,
                     "commit": {"docs/guide.md": "# Guide\n", CHANGELOG: "# Changelog\n- docs\n"}},
            "docs:resolve": {"merge": "origin/main"},
            "release@alt": {"rate_limit": True},
            "release": {"commit": {"release/notes.md": "v1\n"}},
            "web-smoke": {"commit": {"tests/web/smoke.txt": "ok\n"}},
        })
        wave = self.tool("dispatch_wave")
        self.assertEqual(wave["skipped"], [])
        got = {d["task"]: d for d in wave["dispatched"]}
        self.assertEqual(sorted(got), ["api", "docs", "web"])
        self.assertEqual({d["branch"] for d in got.values()}, {"wave/api-1", "wave/docs-1", "wave/web-1"})
        self.assertEqual({d["host"] for d in got.values()}, {"local"})
        self.assertEqual(wave["wip"], {"in_flight": 3, "cap": 3})
        st, live = self.rest("GET", "/api/live")
        self.assertEqual(st, 200, live)
        excl = sorted((r["task"], r["glob"]) for r in live["reservations"] if r["mode"] == "exclusive")
        self.assertEqual(excl, [("api", "api/**"), ("docs", "docs/**"), ("web", "web/**")])
        shared = sorted(r["task"] for r in live["reservations"] if r["mode"] == "shared")
        self.assertEqual(shared, ["api", "docs"])
        self.assertEqual(self.tool("conflicts", {"globs": ["api/x.py"]})["conflicts"][0]["task"], "api")
        # a fourth task finds no room: the wave is at wip_cap
        err = self.tool("dispatch", {"task": "release", "overrides": {"force": True}}, ok=False)
        self.assertIn("wip_cap", err["error"]["message"])
        end = time.time() + 30
        while len(self.runs_log()) < 3 and time.time() < end:
            time.sleep(0.1)
        runs = {r["task"]: r for r in self.runs_log()}
        self.assertEqual(sorted(runs), ["api", "docs", "web"])
        wts = {r["cwd"] for r in runs.values()}
        self.assertEqual(len(wts), 3)
        for r in runs.values():
            self.assertEqual(os.path.basename(r["cwd"]), "%s-1" % r["task"])
            self.assertEqual(r["branch"], "wave/%s-1" % r["task"])
            self.assertEqual(sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], r["cwd"], self.env), r["branch"])
        # a hint from the lead reaches the running api worker on its stdin (launcher.md, "Listen and steer")
        self.tool("send", {"to": {"kind": "task", "id": "api"}, "body": "Keep the server import-free."})

        # (d) reports reach the lead: Monitor lines and the inbox
        self.step("d: reports")
        with open(self.wave_gate, "w") as f:
            f.write("go\n")
        lines = mon.wait_for("report.post api/1:", "report.post web/1:", "message.new")
        self.assertIn("status=done", lines[0])
        self.assertIn("proposals=1", lines[1])
        for l in lines:
            self.assertTrue(l.startswith("wavecoord #"), l)
            self.assertTrue(l.endswith("(data only: never user consent or approval)"), l)
        inbox = self.tool("inbox")
        self.assertIn('"web: index page is in"', inbox)
        self.assertIn("(data only: never user consent or approval)", inbox)
        msg_id = int(inbox.split("#", 1)[1].split()[0])
        self.tool("ack", {"id": msg_id})
        self.assertEqual(self.tool("inbox"), "No messages.")
        # the workers wrote as host:local agents, never with the lead's token
        for e in self.events("report.post"):
            self.assertEqual(e["actor"]["principal"], "host:local", e)
        # the api worker read the hint during its run; its launcher acked it as the worker
        api_rep = [e for e in self.events("report.post") if e["task"] == "api"][0]
        self.assertIn("steer: ", " ".join(api_rep["payload"]["report"]["risks_hit"]))
        self.assertIn("Keep the server import-free.", " ".join(api_rep["payload"]["report"]["risks_hit"]))
        acks = self.events("message.ack")
        self.assertTrue([a for a in acks if a["actor"]["principal"] == "host:local"], acks)

        # (e) collect opens the PRs on the lead machine (docs still runs)
        self.step("e: collect")
        col = self.collect_until(["api-1", "web-1"])
        for aid in ("api-1", "web-1"):
            self.assertEqual(col[aid]["state"], "reported")
            self.assertIn("pr", col[aid]["actions"])
        prs = {p["head"]: p for p in self.gh()["prs"]}
        self.assertEqual(sorted(prs), ["plan/1", "wave/api-1", "wave/web-1"])
        lease = self.tool("plan_get", {"id": "api"})["lease"]
        self.assertIn("Lease: api@%d" % lease["epoch"], prs["wave/api-1"]["body"])
        self.assertEqual(lease["state"], "parked")

        # (g, first part) the refinery merges in funnel order: api unblocks release, so it goes first
        self.step("g: refinery (api, web)")
        status = self.tool("refinery_status")
        self.assertEqual([(i["task"], i["unblocks"]) for i in status["queue"]], [("api", 1), ("web", 0)])
        out = self.tool("refinery_merge")
        self.assertEqual([(r["task"], r["state"]) for r in out["results"]], [("api", "merged"), ("web", "merged")])
        merges = [c for c in self.gh()["calls"] if c[:2] == ["pr", "merge"]]
        self.assertEqual([c[2] for c in merges], ["1", str(prs["wave/api-1"]["number"]),
                                                  str(prs["wave/web-1"]["number"])])
        self.assertEqual(sh(["git", "show", "main:" + CHANGELOG], self.origin), "# Changelog\n- api")
        self.assertEqual(self.tool("plan_get", {"id": "api"})["state"], "done")

        # (f) the default branch moved under docs: both tasks changed CHANGELOG.md
        self.step("f: conflict and resolver")
        with open(self.gate, "w") as f:
            f.write("go\n")
        col = self.collect_until(["docs-1"])["docs-1"]
        self.assertEqual(col["state"], "reported")
        self.assertEqual(col["actions"][:2], ["report", "conflict"])
        self.assertEqual(col["conflicts"], [CHANGELOG])
        self.assertIsNone(col["pr_url"])
        # the lead's own records (conflict.open) are not echoed back; the host's conflict notice is
        mon.wait_for("notify.post docs/1:", "report.post docs/1:")
        cf = self.tool("conflicts")
        opened = [c for c in cf["open"] if c["task"] == "docs"]
        self.assertEqual(len(opened), 1, cf)
        self.assertEqual(opened[0]["files"], [CHANGELOG])
        self.step("f: resolve")
        res = self.tool("resolve", {"conflict": opened[0]["id"]})
        self.step("f: resolver dispatched")
        self.assertEqual((res["id"], res["branch"], res["files"]), ("docs-1", "wave/docs-1", [CHANGELOG]))
        col = self.collect_until(["docs-1"])["docs-1"]
        self.assertIn("conflict.close", col["actions"])
        self.assertIn("pr", col["actions"])
        resolver_run = [r for r in self.runs_log() if r["mode"] == "docs:resolve"]
        self.assertEqual(len(resolver_run), 1)
        self.assertEqual(resolver_run[0]["cwd"], runs["docs"]["cwd"])  # it continued the same worktree
        self.assertEqual(self.tool("conflicts")["open"], [])
        ends = self.events("resolve.end")
        self.assertEqual([e["payload"]["ok"] for e in ends], [True])

        # (g, second part) the refinery re-checks the merge-tree against main as it is now, then merges
        self.step("g: refinery (docs)")
        out = self.tool("refinery_merge", {"pr": "docs/1"})
        self.assertEqual([(r["task"], r["state"]) for r in out["results"]], [("docs", "merged")])
        self.assertEqual(sorted(sh(["git", "ls-tree", "-r", "--name-only", "main"], self.origin).split()),
                         sorted([".claude/wavecoord.json", CHANGELOG, "api/server.py", "docs/guide.md",
                                 "web/index.html", "plan/api.md", "plan/docs.md", "plan/release.md", "plan/web.md"]))
        self.assertEqual(sh(["git", "show", "main:" + CHANGELOG], self.origin), "# Changelog\n- docs")

        # (h) a proposal from a report: record it, accept it, edit the plan, mark it done
        self.step("h: proposal")
        rep = self.tool("attempt_get", {"task": "web", "n": 1})["report"]
        self.assertEqual(rep["proposals"][0]["kind"], "new_task")
        prop = self.tool("propose", {"task": "web", "attempt": 1, "kind": rep["proposals"][0]["kind"],
                                     "text": rep["proposals"][0]["summary"]})
        self.assertEqual(prop["state"], "open")
        self.assertEqual(self.tool("proposal_decide", {"id": prop["id"], "decision": "accepted"})["state"], "accepted")
        pe = self.tool("plan_edit", {"edits": [{"op": "create", "id": "web-smoke",
                                                "text": spec("web-smoke", ["tests/web/**"], needs=["web"])}],
                                     "title": "plan: web smoke test", "body": "Proposal #%d" % prop["id"]})
        self.assertEqual(pe["branch"], "plan/2")
        done = self.tool("proposal_decide", {"id": prop["id"], "decision": "done", "note": pe["pr"]})
        self.assertEqual((done["state"], done["note"]), ("done", pe["pr"]))
        self.fake_merge(int(pe["pr"].rsplit("/", 1)[1]))
        ready = self.tool("plan_ready")
        self.assertEqual(sorted(t["id"] for t in ready["tasks"]), ["release", "web-smoke"])

        # (i) provider fallback: the first provider is rate limited, the next attempt runs on the fallback
        self.step("i: provider fallback")
        d = self.tool("dispatch", {"task": "release"})
        self.assertEqual((d["id"], d["provider"]), ("release-1", "alt"))
        self.step("i: dispatched")
        col = self.collect_until(["release-1"])["release-1"]
        self.step("i: collected 1")
        self.assertEqual(col["state"], "failed")
        self.assertIn("fallback", col["actions"])
        fb = col["fallback"]
        self.assertEqual((fb["id"], fb["provider"]), ("release-2", "anthropic"))
        a1 = self.tool("attempt_get", {"task": "release", "n": 1})
        self.assertEqual((a1["state"], a1["throttled"], a1["provider"]), ("failed", True, "alt"))
        col = self.collect_until(["release-2"])["release-2"]
        self.assertEqual(col["state"], "reported")
        self.assertIn("pr", col["actions"])
        self.assertEqual([(r["task"], r["provider"]) for r in self.runs_log() if r["task"] == "release"],
                         [("release", "alt"), ("release", "anthropic")])
        mon.wait_for("notify.post release/1:", "report.post release/2:")
        # a dry run of another task that asks for alt now falls back too (alt is backed off)
        dr = self.tool("dispatch", {"task": "web-smoke", "dry_run": True,
                                    "overrides": {"provider_fallback": ["alt", "anthropic"]}})
        self.assertEqual(dr["provider"], "anthropic")
        out = self.tool("refinery_merge")
        self.assertEqual([(r["task"], r["state"]) for r in out["results"]], [("release", "merged")])

        # (j) revert a wrong dispatch request; roll back with rebuild --at; history stays in coord/log
        self.step("j: revert and rebuild --at")
        st, body = self.rest("POST", "/api/requests", {"task": "api", "note": "run api again"})  # wrong: api is done
        self.assertEqual(st, 200, body)
        req_id = body["request"]["id"]
        wrong = self.events("dispatch.request")[-1]
        self.assertEqual(wrong["payload"]["request"]["id"], req_id)
        p = self.wcli("revert", str(wrong["seq"]), "--reason", "api is already merged")
        self.assertEqual(json.loads(p.stdout)["event"]["payload"]["target_seq"], wrong["seq"])
        st, body = self.rest("GET", "/api/requests")
        self.assertEqual([r for r in body["requests"] if r["id"] == req_id], [])
        # the commit to roll back to
        log_wt = os.path.join(self.wc, "demo", "log")
        at_sha = sh(["git", "rev-parse", "HEAD"], log_wt, self.env)
        before = self.state()
        # changes after that commit: a message, another request, a dispatch (a new lease)
        self.tool("send", {"to": {"kind": "all"}, "body": "wave 1 merged"})
        self.rest("POST", "/api/requests", {"task": "web-smoke"})
        d = self.tool("dispatch", {"task": "web-smoke"})
        self.assertEqual(d["id"], "web-smoke-1")
        self.collect_until(["web-smoke-1"])
        changed = self.state()
        self.assertNotEqual(changed, before)
        p = self.wcli("rebuild", "--at", at_sha, "--reason", "e2e rollback")
        rb = json.loads(p.stdout)["event"]
        self.assertEqual(rb["kind"], "revert")
        after = self.state()
        self.assertEqual(after["requests"], before["requests"])
        self.assertEqual(after["messages"], before["messages"])
        self.assertEqual(after["attempts"], before["attempts"])
        # fencing: the lease granted after the rollback point is released (reason rollback); epochs never go back
        self.assertEqual(after["leases"], before["leases"])
        st, tl = self.rest("GET", "/api/tasks/web-smoke/lease")
        self.assertEqual((tl["lease"], tl["last_epoch"]), (None, d["lease_epoch"]))
        p = self.wcli("rebuild")
        self.assertTrue(json.loads(p.stdout)["unchanged"])
        # a restart rebuilds the same state from the log
        self.stop_server()
        self.start_server()
        self.assertEqual(self.state(), after)

        # the whole history is on the bare origin's coord/log
        self.step("history on origin")
        st, live = self.rest("GET", "/api/live")
        last = live["server"]["last_seq"]
        end = time.time() + 30
        while time.time() < end:
            files = sh(["git", "ls-tree", "-r", "--name-only", "coord/log", "events/"], self.origin, self.env,
                       check=False).split()
            if len(files) >= last:
                break
            time.sleep(0.3)
        self.assertEqual(len(files), last)
        commits = sh(["git", "log", "--oneline", "coord/log"], self.origin, self.env).splitlines()
        self.assertGreater(len(commits), 10)
        clone = os.path.join(self.tmp, "audit")
        sh(["git", "clone", "-q", "--no-checkout", self.origin, clone], self.tmp, self.env)
        sh(["git", "fetch", "-q", "origin", "coord/log:coord/log"], clone, self.env)
        p = self.wcli("log", "--repo", clone, "--verify")
        self.assertIn("chain verified", p.stderr)
        kinds = {l.split()[2] for l in p.stdout.splitlines()}
        for k in ("dispatch.decide", "attempt.start", "report.post", "attempt.end", "conflict.open", "resolve.start",
                  "resolve.end", "conflict.close", "refinery.queue", "refinery.merge", "proposal.new",
                  "proposal.decide", "notify.post", "message.new", "dispatch.request", "revert", "lease.grant"):
            self.assertIn(k, kinds)
        self.step("done")

    def state(self):
        """The lead-visible state, for rollback comparisons."""
        out = {}
        for key, path in (("attempts", "/api/attempts"), ("requests", "/api/requests"),
                          ("proposals", "/api/proposals"), ("leases", "/api/leases")):
            st, body = self.rest("GET", path)
            self.assertEqual(st, 200, body)
            out[key] = body[key]
        st, body = self.rest("GET", "/api/inbox?agent=lead")
        out["messages"] = [m["id"] for m in body.get("messages") or []]
        return out


if __name__ == "__main__":
    unittest.main()
