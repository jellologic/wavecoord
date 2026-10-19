#!/usr/bin/env python3
"""Live smoke test of listen and steer with a real `claude` (docs/launcher.md, "Listen and steer").

The unit and e2e tests use a fake claude. This script checks the parts that only a
real Claude Code can show: that it reads the stream-json user lines and control
requests the wrapper writes on stdin, and that the inbox relay's socket format
works. It calls a model (a few short runs), so it is not part of the test suite.

    python3 scripts/smoke-live.py                      # this host, model haiku
    python3 scripts/smoke-live.py --model sonnet --keep
    python3 scripts/smoke-live.py --ssh build-1=build-1.lan --origin git@forge:me/wc-smoke.git

Everything runs in a throwaway directory: a repo with a bare origin, a coordinator
(`wavecoord serve`) with its own tokens, and WAVECOORD_HOME. Claude uses your
normal login. An --ssh host needs claude logged in there, and --origin: a remote
both machines can push to (the script pushes the seed commit; the host clones it).

Checks, per host:
  hint      a hint sent mid-run is read: the pushed branch has HINT.txt with a random word
  steer     set_model and interrupt get control responses; the note after the
            interrupt is read: the branch has NOTE.txt with a random word
  listen    progress notices reach the coordinator with a context percentage;
            stuck notices for a loop (a warning only: the model decides how it loops)
  relay     (this host) a plain `claude -p` worker session gets a message through its
            inbox socket and repeats a random word
  spec      (this host) a two-task requirement of the spec tree, through the coordinator: each wave
            dispatched from the tree, the requirement in the worker prompt, status commits on main
            (in_progress, in_review, done), the PR trailers, and the requirement verified at the
            merge of its last task. PRs use the fake gh of tests/ against the throwaway origin.

Exit 0 when every required check passed.
"""

import argparse
import json
import os
import random
import shutil
import string
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WAVECOORD = os.path.join(ROOT, "plugin", "bin", "wavecoord")
FAKE_GH = os.path.join(ROOT, "tests", "fake-gh", "gh")
PARENT_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_MESSAGING_SOCKET",
              "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PLUGIN_OPTION_TOKEN", "CLAUDE_PLUGIN_OPTION_COORD_URL",
              "WAVECOORD_URL", "WAVECOORD_TOKEN", "WAVECOORD_TOKEN_FILE", "WAVECOORD_ROLE", "WAVECOORD_TASK",
              "WAVECOORD_AGENT_ID", "WAVECOORD_ATTEMPT_DIR", "WAVECOORD_STATE_DIR")
ALLOWED = "Bash(sleep *),Bash(false),Bash(git *),Bash(ls *),Write,Edit"  # a throwaway repo; a prompt nobody answers stalls a run


def run_ok(p):
    return p.returncode == 0


def word():
    return "w" + "".join(random.choice(string.ascii_lowercase) for _ in range(8))


def sh(args, cwd=None, env=None, check=True, timeout=120):
    p = subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError("%s: exit %s\n%s%s" % (" ".join(args), p.returncode, p.stdout[-2000:], p.stderr[-2000:]))
    return p


class Smoke(object):
    def __init__(self, args):
        self.args = args
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wc-smoke-"))
        self.home = os.path.join(self.tmp, "wchome")
        self.repo = os.path.join(self.tmp, "repo")
        self.results = []  # (host, check, ok, detail); ok None = warning
        env = {k: v for k, v in os.environ.items() if k not in PARENT_ENV}
        env.update(WAVECOORD_HOME=self.home, WAVECOORD_TOKENS_FILE=os.path.join(self.home, "tokens.json"),
                   WAVECOORD_TOKEN_FILE=os.path.join(self.home, "token"), WAVECOORD_PROGRESS_S="10",
                   WAVECOORD_INBOX_POLL_S="2", WAVECOORD_NOTIFY_MIN_INTERVAL_S="0",
                   FAKE_GH_STATE=os.path.join(self.tmp, "gh.json"),
                   # the refinery calls `gh pr view`: the fake answers for the throwaway origin, never GitHub
                   PATH=os.pathsep.join([os.path.dirname(FAKE_GH), os.environ.get("PATH", "")]))
        self.env = env
        self.server = None

    def log(self, msg):
        sys.stdout.write("[smoke %5.0fs] %s\n" % (time.time() - self.t0, msg))
        sys.stdout.flush()

    def wc(self, *args, check=True, timeout=180):
        return sh([sys.executable, WAVECOORD] + list(args), cwd=self.repo, env=self.env, check=check, timeout=timeout)

    def record(self, host, check, ok, detail=""):
        self.results.append((host, check, ok, detail))
        self.log("%s %s: %s %s" % (host, check, {True: "PASS", False: "FAIL", None: "WARN"}[ok], detail))

    # -- setup
    def setup(self):
        os.makedirs(self.home)
        origin = self.args.origin or os.path.join(self.tmp, "origin.git")
        if not self.args.origin:
            sh(["git", "init", "-q", "--bare", "-b", "main", origin])
        sh(["git", "init", "-q", "-b", "main", self.repo])
        hosts = [{"name": "local", "slots": 3, "repo_path": ".",
                  "claude_flags": ["--permission-mode", "acceptEdits", "--allowedTools", ALLOWED]}]
        for name, dest, path in self.args.ssh_hosts:
            hosts.append({"name": name, "ssh": dest, "slots": 2, "repo_path": path,
                          "claude_flags": ["--permission-mode", "acceptEdits", "--allowedTools", ALLOWED]})
        gh = [sys.executable, FAKE_GH]
        cfg = {"project": "wcsmoke", "hosts": hosts, "wip_cap": 6, "spec": {"status_flush_s": 0},
               "pr_cmd": gh + ["pr", "create", "--head", "{branch}", "--base", "{base}", "--title", "{title}",
                               "--body-file", "{body_file}"],
               "merge_cmd": gh + ["pr", "merge", "{pr}", "--merge", "--match-head-commit", "{sha}"]}
        os.makedirs(os.path.join(self.repo, ".claude"))
        with open(os.path.join(self.repo, ".claude", "wavecoord.json"), "w") as f:
            json.dump(cfg, f, indent=1)
        with open(os.path.join(self.repo, "README.md"), "w") as f:
            f.write("wavecoord live smoke test\n")
        for a in (["add", "-A"], ["-c", "user.name=smoke", "-c", "user.email=smoke@example.invalid", "commit", "-q",
                                  "-m", "seed"], ["remote", "add", "origin", origin], ["push", "-q", "origin", "main"]):
            sh(["git"] + a, cwd=self.repo)
        for name, dest, path in self.args.ssh_hosts:
            self.log("cloning %s on %s:%s" % (origin, dest, path))
            sh(["ssh", dest, "rm -rf %s && git clone -q %s %s" % (path, origin, path)], timeout=300)
        self.wc("tokens", "provision", "--role", "lead", "--out", os.path.join(self.home, "token"))
        for h in hosts:
            self.wc("tokens", "provision", "--role", "agent", "--host", h["name"], "--out",
                    os.path.join(self.home, "hosts", h["name"] + ".token"))
        self.server = subprocess.Popen([sys.executable, WAVECOORD, "serve", "--repo", self.repo, "--home",
                                        os.path.join(self.home, "server"), "--port", "0"], env=self.env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        line = self.server.stdout.readline()
        if not line.startswith("wavecoord serve: http"):
            raise RuntimeError("the server did not start: %r %s" % (line, self.server.stderr.read()[-1000:]))
        self.url = line.split()[2]
        self.env["WAVECOORD_URL"] = self.url
        self.env["WAVECOORD_TOKEN"] = open(os.path.join(self.home, "token")).read().strip()
        self.log("coordinator %s, repo %s" % (self.url, self.repo))
        return [h["name"] for h in hosts]

    # -- helpers
    def launch(self, host, task, prompt, max_turns=30):
        pf = os.path.join(self.tmp, "%s-%s.md" % (task, host))
        with open(pf, "w") as f:
            f.write(prompt)
        out = self.wc("launch", "--task", task, "--prompt-file", pf, "--host", host, "--model", self.args.model,
                      "--max-turns", str(max_turns), "--timeout-min", "10", "--json").stdout
        rec = json.loads(out)
        self.log("%s: launched %s (agent %s)" % (host, rec["id"], rec["agent"]))
        return rec

    def stream(self, aid, host):
        p = self.wc("attempts", "logs", aid, "--host", host, "--raw", "--tail", "2000", check=False)
        out = []
        for l in p.stdout.splitlines():
            l = l.strip()
            if l.startswith("{"):
                try:
                    out.append(json.loads(l))
                except ValueError:
                    pass
        return out

    def wait_tool(self, aid, host, timeout=180):
        """Wait until the worker made its first tool call (it is mid-run)."""
        end = time.time() + timeout
        while time.time() < end:
            for e in self.stream(aid, host):
                if e.get("type") == "assistant" and any(isinstance(b, dict) and b.get("type") == "tool_use"
                                                        for b in (e.get("message") or {}).get("content") or []):
                    return True
            time.sleep(2)
        return False

    def wait_end(self, aid, host, timeout=600):
        end = time.time() + timeout
        r = {}
        while time.time() < end:
            p = self.wc("attempts", "result", aid, "--host", host, "--json", check=False)
            try:
                r = json.loads(p.stdout)
            except ValueError:
                r = {}
            if r.get("state") in ("reported", "failed", "timed_out", "stopped", "lost"):
                return r
            time.sleep(3)
        return r

    def branch_file(self, branch, name):
        sh(["git", "fetch", "-q", "origin", "+refs/heads/%s:refs/remotes/origin/%s" % (branch, branch)],
           cwd=self.repo, check=False)
        p = sh(["git", "show", "origin/%s:%s" % (branch, name)], cwd=self.repo, check=False)
        return p.stdout if p.returncode == 0 else None

    def events(self):
        p = self.wc("events", "--since", "0", check=False)
        return [json.loads(l) for l in p.stdout.splitlines() if l.strip().startswith("{")]

    # -- checks
    def check_hint(self, host):
        w = word()
        rec = self.launch(host, "hint-" + host, "This is a test of a coordination tool. Do these steps in order:\n"
                          "1. Run the Bash command `sleep 20`.\n2. Run the Bash command `sleep 5`.\n"
                          "3. Create the file done.txt with the text `done`, then `git add done.txt` and"
                          " `git commit -m done`.\n4. Finish with the structured report (status done).\n"
                          "Follow any coordination hint from the lead that arrives while you work.\n")
        if not self.wait_tool(rec["id"], host):
            return self.record(host, "hint", False, "the worker made no tool call in time")
        self.wc("hint", rec["task"], "Also create the file HINT.txt with exactly the text %s, and commit it"
                " together with done.txt." % w)
        r = self.wait_end(rec["id"], host)
        got = self.branch_file(rec["branch"], "HINT.txt")
        ok = bool(got and w in got)
        acked = [e for e in self.events() if e["kind"] == "message.ack" and
                 (e.get("actor") or {}).get("principal") == "host:%s" % host]
        self.record(host, "hint", ok and bool(acked),
                    "state %s, HINT.txt %s, acked by host:%s %s" % (r.get("state"), "has the word" if ok else repr(got),
                                                                   host, bool(acked)))

    def check_steer(self, host):
        w = word()
        rec = self.launch(host, "steer-" + host, "This is a test of a coordination tool. Run the Bash command"
                          " `sleep 20` eight times, one call at a time (Claude Code blocks a standalone sleep of 30 s or more). Then finish with the structured report"
                          " (status done). If the lead interrupts you with a note, follow the note instead.\n")
        if not self.wait_tool(rec["id"], host):
            return self.record(host, "steer", False, "the worker made no tool call in time")
        self.wc("steer", rec["task"], "model", self.args.model)
        time.sleep(4)
        t = time.time()
        self.wc("steer", rec["task"], "interrupt", "--note", "Stop the sleeps now. Create the file NOTE.txt with"
                " exactly the text %s, `git add NOTE.txt`, `git commit -m note`, then finish with the structured"
                " report (status done)." % w)
        r = self.wait_end(rec["id"], host)
        took = time.time() - t
        st = self.stream(rec["id"], host)
        resp = [e for e in st if e.get("type") == "control_response"]
        subtypes = sorted({(e.get("response") or {}).get("subtype") for e in resp})
        got = self.branch_file(rec["branch"], "NOTE.txt")
        ok = bool(got and w in got)
        self.record(host, "steer: set_model", bool(resp) and "error" not in subtypes,
                    "control responses %s" % subtypes)
        self.record(host, "steer: interrupt + note", ok, "state %s in %.0fs after the interrupt, NOTE.txt %s" % (
            r.get("state"), took, "has the word" if ok else repr(got)))
        # the interrupt came in the middle of a 20 s sleep: Claude Code ends that turn with aborted_tools
        cut = [e for e in st if e.get("type") == "result" and e.get("terminal_reason") == "aborted_tools"]
        self.record(host, "steer: interrupt cuts the running tool", bool(cut),
                    "a result with terminal_reason aborted_tools" if cut else "no aborted_tools result")

    def check_listen(self, host):
        rec = self.launch(host, "listen-" + host, "This is a test of a monitoring tool. Run the Bash command `false`"
                          " four times, one call at a time, even though it fails. Then run `sleep 15` once. Then"
                          " finish with the structured report (status done).\n", max_turns=20)
        self.wait_end(rec["id"], host)
        notes = [e["payload"]["notice"] for e in self.events() if e["kind"] == "notify.post"
                 and (e["payload"].get("notice") or {}).get("task") == rec["task"]]
        prog = [n for n in notes if (n.get("detail") or {}).get("event") == "progress"]
        stuck = sorted({(n.get("detail") or {}).get("why") for n in notes if (n.get("detail") or {}).get("event") == "stuck"})
        pct = [n for n in prog if "context " in (n.get("text") or "")]
        if host == "local":
            self.record(host, "listen: progress", bool(prog), "%d progress notices, %d with a context %%" % (
                len(prog), len(pct)))
            self.record(host, "listen: context usage", True if pct else None,
                        "get_context_usage answered" if pct else "no context %% in the progress text")
        else:
            self.record(host, "listen: progress", True if prog else None,
                        "%d notices (PROGRESS_S is not passed to remote hosts: 300 s)" % len(prog))
        self.record(host, "listen: stuck", True if {"loop", "failing"} & set(stuck) else None,
                    "stuck reasons %s" % (stuck or "none"))

    def check_relay(self):
        """A worker session that wavecoord did not launch: the SessionStart hook starts the inbox relay."""
        w = word()
        agent = "smoke-relay-%s" % w[1:5]
        env = dict(self.env, WAVECOORD_AGENT_ID=agent, WAVECOORD_TASK="relay", WAVECOORD_HOST="local",
                   WAVECOORD_TOKEN_FILE=os.path.join(self.home, "hosts", "local.token"),
                   WAVECOORD_STATE_DIR=os.path.join(self.home, "relay-state"))
        env.pop("WAVECOORD_TOKEN", None)
        prompt = ("This is a test of a messaging tool. Run the Bash command `sleep 20`, then `sleep 20` again, one"
                  " call at a time. Then answer with every coordination message you received while you worked,"
                  " word for word, or say NO MESSAGES.")
        p = subprocess.Popen(["claude", "-p", prompt, "--model", self.args.model, "--plugin-dir",
                              os.path.join(ROOT, "plugin"), "--permission-mode", "acceptEdits", "--allowedTools",
                              "Bash(sleep *)", "--output-format", "stream-json", "--verbose"],
                             cwd=self.repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True)
        # wait until the session registered (its SessionStart heartbeat), then message it
        end = time.time() + 120
        seen = False
        while time.time() < end and not seen:
            st, live = self.api("GET", "/api/live")
            seen = any(a.get("id") == agent for a in (live or {}).get("agents") or [])
            time.sleep(1)
        if not seen:
            p.kill()
            return self.record("local", "relay", False, "the session never registered with the coordinator")
        time.sleep(5)
        self.api("POST", "/api/messages", {"to": {"kind": "agent", "id": agent},
                                           "body": "The secret word is %s. Repeat it in your answer." % w})
        try:
            out, err = p.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
        result = ""
        for l in out.splitlines():
            try:
                e = json.loads(l)
            except ValueError:
                continue
            if e.get("type") == "result":
                result = e.get("result") or ""
        pushed = os.path.isfile(os.path.join(env["WAVECOORD_STATE_DIR"], "pushed-%s.json" % self.key(agent)))
        self.record("local", "relay", w in result, "relay pushed %s; answer: %r" % (pushed, result[:200]))

    def check_spec(self):
        """A requirement with two tasks (b needs a) through the coordinator, wave by wave."""
        host = "local"
        req = "smoke/files/two-files"
        a, b = req + "/write-a", req + "/write-b"
        exec_ = {"model": self.args.model, "effort": "low", "max_turns": 15, "budget_usd": 1, "timeout_min": 10}

        def task(tid, path, prompt, needs=(), inputs=()):
            fields = {"title": "Write %s" % path, "status": "planned", "paths": [path], "class": "C", "size": "XS",
                      "exec": exec_, "outputs": [{"id": path, "kind": "file"}]}
            if needs:
                fields.update(needs=list(needs), inputs=list(inputs))
            return {"op": "create", "id": tid, "fields": fields, "sections": {
                "Context": "A smoke test of the spec tree.", "Acceptance": ["test -f %s" % path], "Prompt": prompt}}

        ops = [
            {"op": "create", "id": "smoke", "fields": {"title": "Smoke", "status": "active"},
             "sections": {"Intent": "Prove the spec tree end to end."}},
            {"op": "create", "id": "smoke/files", "fields": {"title": "Files", "status": "active"},
             "sections": {"Intent": "Two files, one after the other."}},
            {"op": "create", "id": req, "fields": {"title": "Two files", "status": "approved"},
             "sections": {"Statement": "When the smoke test runs, the repository shall contain out/a.txt and out/b.txt.",
                          "Verify": ["test -f out/a.txt && test -f out/b.txt"]}},
            task(a, "out/a.txt", "Create the file out/a.txt with the single line: a. Then run `git add out/a.txt` and"
                                 " `git commit -m a`. Then finish with the structured report (status done)."),
            task(b, "out/b.txt", "Create the file out/b.txt with the single line: b. Then run `git add out/b.txt` and"
                                 " `git commit -m b`. Then finish with the structured report (status done).",
                 needs=[a], inputs=[{"id": "out/a.txt", "kind": "file", "from": a}]),
        ]
        p = subprocess.run([sys.executable, WAVECOORD, "spec", "apply", "-"], cwd=self.repo, env=self.env,
                           input=json.dumps({"ops": ops}), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True)
        if p.returncode != 0:
            return self.record(host, "spec: tree", False, p.stderr[-300:])
        for args in (["add", "-A"], ["-c", "user.name=smoke", "-c", "user.email=smoke@example.invalid", "commit",
                                     "-q", "-m", "spec tree"], ["push", "-q", "origin", "main"]):
            sh(["git"] + args, cwd=self.repo)
        self.record(host, "spec: tree", run_ok(self.wc("spec", "check", check=False)), "check on the tree")

        def status_on_main(tid):
            sh(["git", "fetch", "-q", "origin"], cwd=self.repo)
            path = "specs/%s/tasks/%s.md" % tuple(tid.rsplit("/", 1))
            text = sh(["git", "show", "origin/main:" + path], cwd=self.repo, check=False).stdout
            for line in text.split("\n"):
                if line.startswith("status: "):
                    return line[8:].strip()
            return None

        verified = None
        for tid in (a, b):
            st, out = self.api("POST", "/api/dispatch", {"wave": True})
            got = [d["task"] for d in (out or {}).get("dispatched") or []]
            cid = tid.replace("/", ".")
            if got != [cid]:
                return self.record(host, "spec: wave %s" % tid, False, "dispatched %s: %s" % (got, out))
            d = out["dispatched"][0]
            prompt_path = os.path.join(self.home, "attempts", "wcsmoke", d["id"], "prompt.md")
            prompt = open(prompt_path).read() if os.path.exists(prompt_path) else ""
            self.record(host, "spec: dispatch %s" % tid.rsplit("/", 1)[1], "## Requirement" in prompt and
                        status_on_main(tid) == "in_progress", "status on main %s; requirement in the prompt %s" % (
                            status_on_main(tid), "## Requirement" in prompt))
            r = self.wait_end(d["id"], host)
            self.api("POST", "/api/collect", {})
            review = status_on_main(tid)
            st, m = self.api("POST", "/api/refinery/merge", {})
            merged = [x for x in (m or {}).get("results") or [] if x.get("task") == cid]
            ok = bool(merged and merged[0]["state"] == "merged")
            spec = [e for e in self.events() if e["kind"] == "refinery.merge" and e.get("task") == cid]
            verified = ((spec[-1]["payload"].get("spec") or {}).get("verified")) if spec else None
            self.record(host, "spec: merge %s" % tid.rsplit("/", 1)[1], ok and review == "in_review" and
                        status_on_main(tid) == "done", "report %s; in_review before the merge %s; %s; done on main %s;"
                        " requirement verified %s" % (r.get("state"), review == "in_review",
                                                     merged[0]["state"] if merged else m, status_on_main(tid) == "done",
                                                     verified))
            if not ok:
                return
        self.record(host, "spec: requirement verified at the last merge", verified is True, "verified %s" % verified)
        prs = json.load(open(self.env["FAKE_GH_STATE"]))["prs"]
        trailer = [p for p in prs if "Spec-Task: %s" % b in p.get("body", "")]
        self.record(host, "spec: PR trailers", bool(trailer), "Spec-Task in the PR body of %s" % b)

    @staticmethod
    def key(agent):
        import hashlib
        return hashlib.sha256(agent.encode("utf-8")).hexdigest()[:16]

    def api(self, method, path, body=None):
        sys.path.insert(0, os.path.join(ROOT, "src"))
        from wavecoord.corecli import api
        return api(self.url, method, path, body, token=self.env["WAVECOORD_TOKEN"])

    def versions(self, hosts):
        out = {"local": sh(["claude", "--version"], check=False).stdout.strip()}
        for name, dest, _ in self.args.ssh_hosts:
            out[name] = sh(["ssh", dest, "claude --version"], check=False).stdout.strip()
        return out

    def run(self):
        self.t0 = time.time()
        try:
            hosts = self.setup()
            vers = self.versions(hosts)
            for h in hosts:
                for c in self.args.checks:
                    if c in ("relay", "spec"):
                        continue
                    try:
                        getattr(self, "check_" + c)(h)
                    except Exception as e:  # noqa: BLE001 - one broken check must not hide the others
                        self.record(h, c, False, "error: %s" % e)
            if "spec" in self.args.checks:
                try:
                    self.check_spec()
                except Exception as e:  # noqa: BLE001
                    self.record("local", "spec", False, "error: %s" % e)
            if "relay" in self.args.checks:
                try:
                    self.check_relay()
                except Exception as e:  # noqa: BLE001
                    self.record("local", "relay", False, "error: %s" % e)
        finally:
            if self.server:
                self.server.terminate()
                try:
                    self.server.wait(20)
                except subprocess.TimeoutExpired:
                    self.server.kill()
        print("\n## wavecoord live smoke (%s, model %s)\n" % (time.strftime("%Y-%m-%d"), self.args.model))
        for h, v in sorted(vers.items()):
            print("- %s: Claude Code %s" % (h, v))
        print("\n| Host | Check | Result | Detail |\n|---|---|---|---|")
        for h, c, ok, d in self.results:
            print("| %s | %s | %s | %s |" % (h, c, {True: "pass", False: "FAIL", None: "warn"}[ok], d.replace("|", "/")))
        failed = [r for r in self.results if r[2] is False]
        print("\n%s. State: %s" % ("FAILED" if failed else "OK", self.tmp if (self.args.keep or failed) else "removed"))
        if not (self.args.keep or failed):
            shutil.rmtree(self.tmp, ignore_errors=True)
        return 1 if failed else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--checks", default="hint,steer,listen,relay,spec",
                    help="comma-separated: hint,steer,listen,relay,spec")
    ap.add_argument("--ssh", action="append", default=[], metavar="NAME=DEST[:PATH]",
                    help="an ssh host (default path ~/wc-smoke-repo); needs --origin")
    ap.add_argument("--origin", help="a git remote that this machine and the ssh hosts can push to")
    ap.add_argument("--keep", action="store_true", help="keep the temp directory")
    args = ap.parse_args(argv)
    args.checks = [c.strip() for c in args.checks.split(",") if c.strip()]
    args.ssh_hosts = []
    for s in args.ssh:
        name, _, rest = s.partition("=")
        dest, _, path = rest.partition(":")
        args.ssh_hosts.append((name, dest or name, path or "wc-smoke-repo"))
    if args.ssh_hosts and not args.origin:
        ap.error("--ssh needs --origin (a remote the hosts can clone and push)")
    if not shutil.which("claude"):
        ap.error("claude is not on PATH")
    return Smoke(args).run()


if __name__ == "__main__":
    sys.exit(main())
