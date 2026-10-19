"""Seeded defects (tests/fence/README.md, section 2).

For each of the 39 defects, the test applies its patch (tests/fencedefects.py)
and runs (1) the defect's named table check and (2) the randomized
simulation on the first fixed seeds. At least one must fail, else the
defect is not caught and the suite fails. Without a patch, every table
check passes. The result table is printed.

WAVECOORD_FENCE_DEFECT_SEEDS=<n> runs the simulation on n seeds per defect
(default 2).
"""

import io
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
import multiprocessing

import _paths  # noqa: F401
import fencedefects
from fencesim import Sim
from fenceworld import git
from wavecoord import fence, gatecheck, guard
from wavecoord.core import ApiError, lead_ctx


def fs_case_insensitive():
    d = tempfile.mkdtemp()
    try:
        open(os.path.join(d, "a"), "w").close()
        return os.path.exists(os.path.join(d, "A"))
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


CI_FS = fs_case_insensitive()


class Table(object):
    """Named checks: each returns None when the fence behaves, else a description of the failure."""

    def __init__(self, ci=None):
        self.ci = ci
        self.sim = Sim(0, ci=ci, n_workers=2)
        self.w = self.sim.w
        self.a, self.b = self.w.workers

    def close(self):
        self.w.close()

    def edit(self, wk, target, env_extra=None):
        env = self.w.worker_env(wk)
        env.update(env_extra or {})
        inp = {"session_id": "s", "cwd": wk.wt, "hook_event_name": "PreToolUse", "tool_name": "Edit",
               "tool_input": {"file_path": target}}
        return self.sim.run_hook("PreToolUse", inp, env)[0]

    def expect(self, got, want, what):
        return None if got == want else "%s: got %s, want %s" % (what, got, want)

    # one check per defect
    def D01(self):
        self.w.assign(self.a, [("a/b", "exclusive")])
        self.w.assign(self.b, [("a/**", "shared")])
        return self.expect(self.edit(self.b, "a/bc/w.py"), "allow", "a/bc/w.py with a foreign exclusive a/b") or \
            self.expect(self.edit(self.a, "a/bc/w.py"), "deny", "a/bc/w.py outside the lease a/b")

    def D02(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        self.w.proxy.fault = "trickle"
        try:
            return self.expect(self.edit(self.a, "a/x.py"), "deny", "an answer slower than the budget")
        finally:
            self.w.proxy.fault = None

    def D03(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        self.w.coord.claim(lead_ctx(), {"task": self.a.task, "attempt": 2, "holder": self.a.agent, "host": "local",
                                        "steal": True, "force": True,
                                        "reservations": [{"glob": "a/**", "mode": "exclusive"}]})
        return self.expect(self.edit(self.a, "a/x.py"), "deny", "an edit with the old epoch after a new grant")

    def D04(self):
        self.w.assign(self.a, [("docs/**", "shared")])
        self.w.assign(self.b, [("docs/**", "shared")])
        return self.expect(self.edit(self.b, "docs/api.md"), "allow", "two shared holders")

    def D05(self):
        self.w.assign(self.a, [("docs/**", "exclusive")])
        self.w.assign(self.b, [("docs/**", "shared")])
        return self.expect(self.edit(self.b, "docs/api.md"), "deny", "inside a foreign exclusive")

    def D06(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        self.w.proxy.fault = "down"
        try:
            return self.expect(self.edit(self.a, "a/x.py", {"WAVECOORD_FENCE_OFFLINE": "1"}), "deny",
                               "coordinator down, WAVECOORD_FENCE_OFFLINE=1, no override file")
        finally:
            self.w.proxy.fault = None

    def D07(self):
        self.w.assign(self.a, [("docs/**", "shared")])
        self.w.assign(self.b, [("a/**", "exclusive")])
        return self.expect(self.edit(self.a, "docs/link/x.py"), "deny", "docs/link/x.py is a/x.py (symlink)") or \
            self.expect(self.edit(self.a, os.path.join(self.a.wt, "docs", "..", "a", "y.py")), "deny", "docs/../a/y.py")

    def D08(self):
        self.w.assign(self.b, [("a/**", "exclusive")])
        self.w.assign(self.a, [("A/**", "shared")])
        return self.expect(self.edit(self.a, "A/x.py"), "deny", "A/x.py on a case-insensitive file system")

    def D09(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        s = self.sim.agent_req({"lease_id": self.a.lease_id, "glob": "docs/api.md", "mode": "shared",
                                "origin": "hot"}, self.a.agent)
        if s != 200:
            return "hot mark refused: %s" % s
        return self.expect(self.edit(self.a, "docs/api.md"), "deny", "a hot mark does not widen the lease")

    def D10(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        self.w.coord.release(lead_ctx(), self.a.lease_id)
        return self.expect(self.edit(self.a, "a/x.py"), "deny", "an edit after a release")

    def D11(self):
        self.w.assign(self.a, [("a/x*.py", "exclusive")])
        return self.expect(self.edit(self.a, "a/xyz/q.go"), "deny", "a/xyz/q.go is not in a/x*.py")

    def D12(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        first = self.edit(self.a, "a/x.py")
        self.w.coord.claim(lead_ctx(), {"task": self.a.task, "attempt": 2, "holder": "thief", "host": "local",
                                        "steal": True, "force": True, "reservations": []})
        return self.expect(first, "allow", "first edit") or \
            self.expect(self.edit(self.a, "a/x.py"), "deny", "the same edit after a steal")

    def D13(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        with open(os.path.join(self.a.wt, ".claude", "wavecoord.json"), "w") as f:
            json.dump({"project": "sim", "fence": "off"}, f)
        try:
            return self.expect(self.edit(self.a, "docs/api.md"), "deny", "the worktree config sets fence off")
        finally:
            git(self.a.wt, "checkout", "-q", "--", ".claude/wavecoord.json")

    def D14(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        self.sim.op_expire()
        if self.w.coord.state.lease(self.a.lease_id)["state"] != "stale":
            return "the lease did not turn stale"
        return self.expect(self.edit(self.a, "docs/api.md"), "deny", "outside the lease while it is stale")

    def D15(self):
        self.w.assign(self.b, [("docs/**", "exclusive")])
        self.w.assign(self.a, [("a/**", "exclusive")])
        git(self.a.wt, "mv", "docs/api.md", "a/api.md")
        rc = guard.check("staged", cwd=self.a.wt, env=self.w.commit_env(self.a), err=io.StringIO())
        git(self.a.wt, "reset", "-q", "--hard", "HEAD")
        return self.expect(rc, 1, "a rename of a foreign file into the lease")

    def D16(self):
        self.w.assign(self.a, [("x[1].py", "exclusive")])
        return self.expect(self.edit(self.a, "x1.py"), "deny", "x1.py is not x[1].py") or \
            self.expect(self.edit(self.a, "x[1].py"), "allow", "x[1].py is in the lease")

    def D17(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        return self.expect(self.edit(self.a, os.path.join(self.b.wt, "a", "x.py")), "deny", "a sibling worktree")

    def D18(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        with open(os.path.join(self.a.wt, "docs", "api.md"), "a") as f:
            f.write("x\n")
        git(self.a.wt, "add", "-A")
        rc = guard.check("staged", cwd=self.a.wt, env=self.w.commit_env(self.a), err=io.StringIO())
        git(self.a.wt, "reset", "-q", "--hard", "HEAD")
        return self.expect(rc, 1, "a commit outside the lease from a Bash env with no identity")

    def D19(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        with open(os.path.join(self.a.wt, "a", "x.py"), "a") as f:
            f.write("x\n")
        git(self.a.wt, "commit", "-q", "-am", "c")
        sha = git(self.a.wt, "rev-parse", "HEAD").strip()
        self.w.proxy.fault = "down"
        try:
            code = gatecheck.run(None, self.a.task, 1, "origin/main..%s" % sha, None, "%s@%d" % (self.a.task, self.a.epoch),
                                 self.w.repo, env=self.w.base_env("gate"), out=io.StringIO(), err=io.StringIO())
        finally:
            self.w.proxy.fault = None
        return None if code != 0 else "gate-check exit 0 with the coordinator down"

    def D20(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        self.w.proxy.fault = "notjson"
        try:
            return self.expect(self.edit(self.a, "a/x.py"), "deny", "a 200 answer that is not JSON")
        finally:
            self.w.proxy.fault = None

    def D21(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        s = self.sim.agent_req({"lease_id": self.a.lease_id, "glob": "**", "mode": "exclusive"}, self.a.agent)
        return self.expect(s, 403, "an agent token adds an exclusive **")

    def D38(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        s = self.sim.agent_req({"lease_id": self.a.lease_id, "glob": "**", "mode": "shared", "origin": "hot"},
                               self.a.agent)
        return self.expect(s, 403, "an agent token adds a wildcard hot mark **")

    def D39(self):
        self.w.assign(self.a, [("a/**", "exclusive")])
        s = self.sim.agent_req({"task": "rogue", "attempt": 1, "holder": "rogue-1", "host": "local",
                                "reservations": [{"glob": "**", "mode": "exclusive"}]}, "rogue-1", "/api/leases")
        return self.expect(s, 403, "an agent token claims a task the lead never dispatched")

    def D22(self):
        self.w.assign(self.a, [("docs/**", "exclusive")])
        self.w.assign(self.b, [("a/**", "exclusive")])
        try:
            self.w.coord.reserve(lead_ctx(), {"lease_id": self.b.lease_id,
                                              "reservations": [{"glob": "docs/api.md", "mode": "exclusive"}]})
        except ApiError as e:
            return self.expect(e.code, "conflict", "an overlapping exclusive grant")
        return "an overlapping exclusive grant was made in enforce mode"

    def D23(self):
        return journal_retry_check(self.w)

    def D24(self):
        return config_change_check(self.w)

    def D25(self):
        return spec_change_check(self.w)

    def D26(self):
        return config_change_check(self.w)

    def D27(self):
        return config_change_check(self.w)

    def D28(self):
        return spec_change_check(self.w)

    def D29(self):
        return spec_change_check(self.w)

    def D30(self):
        return spec_change_check(self.w)

    def D31(self):
        return config_change_check(self.w)

    def D32(self):
        return config_change_check(self.w)

    def D33(self):
        return submodule_check(self.w)

    def D34(self):
        return nested_repo_check(self.w)

    def D35(self):
        return protected_case_check(self.w)

    def D36(self):
        return journal_size_check(self.w)

    def D37(self):
        return push_git_error_check(self.w)


def _scratch(w, name):
    d = os.path.join(w.root, name)
    os.makedirs(d)
    return d


def submodule_check(w):
    """A gitlink (submodule) change is a path at E2, E3 and E5, also when .gitmodules says ignore = all and the
    git config says diff.ignoreSubmodules = all. -> None or the failure."""
    r = _scratch(w, "sm")
    git(r, "init", "-q")
    git(r, "config", "diff.ignoreSubmodules", "all")
    with open(os.path.join(r, "a.txt"), "w") as f:
        f.write("a\n")
    git(r, "add", "a.txt")
    git(r, "commit", "-q", "-m", "a")
    s1 = git(r, "rev-parse", "HEAD").strip()
    with open(os.path.join(r, ".gitmodules"), "w") as f:
        f.write('[submodule "vendor/sub"]\n\tpath = vendor/sub\n\turl = ../sub\n\tignore = all\n')
    git(r, "add", ".gitmodules")
    git(r, "update-index", "--add", "--cacheinfo", "160000,%s,vendor/sub" % s1)
    git(r, "commit", "-q", "-m", "sub")
    s2 = git(r, "rev-parse", "HEAD").strip()
    git(r, "checkout", "-q", "-b", "w")
    git(r, "update-index", "--cacheinfo", "160000,%s,vendor/sub" % s2)
    staged = guard.staged_paths(r)
    if "vendor/sub" not in (staged or []):
        return "E2: the staged gitlink change is not a path (%s)" % staged
    with open(os.path.join(r, "a.txt"), "a") as f:
        f.write("b\n")
    git(r, "commit", "-q", "-am", "w")
    pushed = guard.push_paths(r, "main..w", None)
    if "vendor/sub" not in (pushed or []):
        return "E3: the pushed gitlink change is not a path (%s)" % pushed
    seen = []

    def check_fn(body):
        seen.extend(body["paths"])
        return fence.CheckResult([{"kind": "outside_lease", "path": p} for p in body["paths"] if p != "a.txt"], 1,
                                 lease={"task": "t", "epoch": 1, "state": "active"})
    code = gatecheck.run(None, "t", 1, "main..w", lease="t@1", repo=r, out=io.StringIO(), err=io.StringIO(),
                         check_fn=check_fn, merge_mode="enforce")
    if "vendor/sub" not in seen or code != 1:
        return "E5: a gitlink change outside the lease passed (exit %s, checked %s)" % (code, seen)
    return None


def nested_repo_check(w):
    """E1: a file in a nested repo of another git repo (a submodule checkout) is checked by its path in the own
    worktree, so a lease src/** does not cover vendor/sub/src/main.c. -> None or the failure."""
    wt = _scratch(w, "nest")
    git(wt, "init", "-q")
    sub = os.path.join(wt, "vendor", "sub")
    os.makedirs(os.path.join(sub, "src"))
    git(sub, "init", "-q")
    target = os.path.join(sub, "src", "main.c")
    t = fence.classify(target, wt, {"WAVECOORD_WORKTREE": wt, "WAVECOORD_TASK": "t"}, True)
    if (t.kind, t.rel) != ("check", "vendor/sub/src/main.c"):
        return "a file in a nested repo: %r, want check vendor/sub/src/main.c" % t
    return None


def protected_case_check(w):
    """E1: a protected local folder in another case (~/.CLAUDE on a folding disk) is still protected.
    On a case-sensitive disk the probe is patched to fold (as D08). -> None or the failure."""
    root = _scratch(w, "pcase")
    cfg = os.path.join(root, ".claude")
    os.makedirs(cfg)
    env = {"CLAUDE_CONFIG_DIR": cfg, "WAVECOORD_TASK": "t"}
    old = fence.folds_case
    if not CI_FS:
        fence.folds_case = lambda p: True
    try:
        t = fence.classify(os.path.join(root, ".CLAUDE", "settings.json"), root, env, True)
    finally:
        fence.folds_case = old
    return None if t.kind == "protected_local" else "a Write to .CLAUDE/settings.json: %r, want protected_local" % t


def journal_size_check(w):
    """A large override journal (long reasons, long paths) reaches the server in one reachable flush: each
    request stays under the server's body limit. -> None or the failure."""
    from wavecoord.client import Client, Deadline
    env = w.base_env("agent")
    fence.write_override(env, 3600, "r" * 500)
    try:
        ovr = fence.read_override(env)
        wk = w.workers[0]
        for i in range(400):
            fence.journal_append(env, ovr, wk.agent, wk.task, wk.epoch, "src/%s/f%04d.py" % ("d" * 60, i), "commit")
        left = fence.journal_flush(Client.from_env(agent=wk.agent, deadline=Deadline(20), env=env), env, wk.agent)
        if left or fence.journal_entries(env):
            return "%d journal entries left after a reachable flush" % len(fence.journal_entries(env))
        return None
    finally:
        fence.remove_override(env)
        try:
            os.remove(fence.journal_path(env))
        except OSError:
            pass


def push_git_error_check(w):
    """The pre-push guard in enforce mode refuses when git cannot compute the pushed paths. -> None or the
    failure."""
    wk = w.workers[0]
    bad = "1" * 40
    rc = guard.check("push", "-", enforce=True, cwd=wk.wt, env=w.commit_env(wk), err=io.StringIO(),
                     stdin=io.StringIO("refs/heads/x %s refs/heads/x %s\n" % ("2" * 40, bad)))
    return None if rc == 1 else "a push whose paths git cannot compute passed (exit %s)" % rc


def config_change_check(w):
    """A task PR must not change the lead's config (edit-fence.md, section 8). The lease covers `.claude/**`, so
    only the config check can refuse it. -> None or the failure."""
    wk = w.workers[0]
    w.assign(wk, [(".claude/**", "exclusive"), ("plugin/**", "exclusive")])
    cfgp = os.path.join(wk.wt, ".claude", "wavecoord.json")

    def gate(change, mode=None, body=None):
        git(wk.wt, "checkout", "-q", "-f", "-B", wk.branch, "origin/main")
        change()
        git(wk.wt, "add", "-A")
        git(wk.wt, "commit", "-q", "--no-verify", "-m", "c")
        sha = git(wk.wt, "rev-parse", "HEAD").strip()
        out = io.StringIO()
        lease = "%s@%d" % (wk.task, wk.epoch)
        code = gatecheck.run(None, wk.task, 1, "origin/main..%s" % sha, None, None if body else lease, w.repo,
                             env=w.base_env("gate"), out=out, err=io.StringIO(), as_json=True, merge_mode=mode,
                             body_text=body.replace("{lease}", lease) if body else None)
        d = json.loads(out.getvalue())
        return code, sorted(v["key"] for v in d.get("config_changes") or [])

    def cfg_edit(fn):
        def change():
            with open(cfgp) as f:
                c = json.load(f)
            fn(c)
            with open(cfgp, "w") as f:
                json.dump(c, f)
        return change

    def write(rel, text):
        def change():
            p = os.path.join(wk.wt, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(text)
        return change

    try:
        cases = [
            ("fence off", cfg_edit(lambda c: c.update(fence="off")), None, (1, ["fence"])),
            ("claude_flags", cfg_edit(lambda c: c.update(hosts=[{"name": "local", "slots": 1, "repo_path": ".",
                                                                 "claude_flags": ["--dangerously-skip-permissions"]}])),
             None, (1, ["hosts[local].claude_flags", "hosts[local].repo_path"])),
            ("pr_cmd", cfg_edit(lambda c: c.update(pr_cmd="sh -c 'curl x | sh'")), None, (1, ["pr_cmd"])),
            ("pr_state_cmd and coord_url", cfg_edit(lambda c: c.update(pr_state_cmd="x", coord_url="http://h:1")),
             None, (1, ["coord_url", "pr_state_cmd"])),
            ("host prepare_cmd and ssh", cfg_edit(lambda c: c.update(hosts=[{"name": "h", "slots": 1, "repo_path": ".",
                                                                             "ssh": "u@h", "prepare_cmd": "x"}])),
             None, (1, ["hosts[h].prepare_cmd", "hosts[h].repo_path", "hosts[h].ssh"])),
            ("providers and merge_cmd", cfg_edit(lambda c: c.update(providers={"x": {}}, merge_cmd="true")), None,
             (1, ["merge_cmd", "providers"])),
            ("settings hooks", write(".claude/settings.json", json.dumps({"disableAllHooks": True})), None,
             (1, ["disableAllHooks"])),
            ("plugin hooks", write("plugin/hooks/hooks.json", "{}"), None, (1, ["the plugin hooks (plugin/hooks/hooks.json)"])),
            ("not a parse", write(".claude/wavecoord.json", "{"), None,
             (1, [".claude/wavecoord.json (the new file does not parse)"])),
            ("auto_steer", cfg_edit(lambda c: c.update(auto_steer="hints")), None, (1, ["auto_steer"])),
            ("an unguarded key", cfg_edit(lambda c: c.update(wip_cap=5)), None, (0, [])),
            # settings deny by default (D32): every key but $schema runs commands, widens trust or steers the lead
            ("settings permissions", write(".claude/settings.json", json.dumps({"permissions": {"allow": []}})), None,
             (1, ["permissions"])),
            ("settings statusLine and apiKeyHelper",
             write(".claude/settings.json", json.dumps({"statusLine": {"type": "command", "command": "x"},
                                                        "apiKeyHelper": "x", "enableAllProjectMcpServers": True})),
             None, (1, ["apiKeyHelper", "enableAllProjectMcpServers", "statusLine"])),
            ("settings $schema only", write(".claude/settings.json", json.dumps({"$schema": "x"})), None, (0, [])),
            ("an MCP server", write(".mcp.json", json.dumps({"mcpServers": {"x": {"command": "sh"}}})), None,
             (1, ["the Claude Code config (.mcp.json)"])),
            ("a project agent", write(".claude/agents/reviewer.md", "x"), None,
             (1, ["the Claude Code config (.claude/agents/reviewer.md)"])),
            # any case, any depth (D31)
            ("settings in another case", write(".claude/Settings.json", json.dumps({"hooks": {}})), None,
             (1, ["the Claude Code config (.claude/Settings.json)"])),
            ("plugin hooks in another case", write("plugin/Hooks/hooks.json", "{}"), None,
             (1, ["the plugin hooks (plugin/Hooks/hooks.json)"])),
            ("nested settings and config", lambda: (write("pkg/.claude/settings.json", "{}")(),
                                                    write("pkg/.claude/wavecoord.json", "{}")()), None,
             (1, ["the Claude Code config (pkg/.claude/settings.json)",
                  "the Claude Code config (pkg/.claude/wavecoord.json)"])),
            ("fence off, merge mode off", cfg_edit(lambda c: c.update(fence="off")), "off", (0, ["fence"])),
        ]
        for what, change, mode, want in cases:
            got = gate(change, mode)
            if got != want:
                return "%s: gate-check gave %s, want %s" % (what, got, want)
        # the config check does not depend on the Lease: line: none, or two of them (a worker summary line)
        bodies = [("no Lease: line", "Task: %s\nAttempt: 1\n" % wk.task, (1, ["fence"])),
                  ("two Lease: lines", "Task: %s\nAttempt: 1\nLease: {lease}\n\nLease: {lease}\n" % wk.task,
                   (3, ["fence"]))]
        for what, body, want in bodies:
            got = gate(cfg_edit(lambda c: c.update(fence="off")), body=body)
            if got != want:
                return "%s: gate-check gave %s, want %s" % (what, got, want)
        return None
    finally:
        git(wk.wt, "checkout", "-q", "-f", "-B", wk.branch, "origin/main")
        git(wk.wt, "clean", "-fdq")


with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "specs", "auth", "_node.md"),
          encoding="utf-8") as _f:
    NODE = _f.read()  # a capability record: a top-level _node.md of the spec tree


def spec_change_check(w):
    """A task PR must not change the spec tree (edit-fence.md, section 8.1), even when its lease covers it. Each
    case builds a base commit on origin/main (the spec tree config and files) and a head on top of it.
    -> None or the failure."""
    wk = w.workers[0]
    w.assign(wk, [("specs/**", "exclusive"), ("specsx/**", "exclusive"), ("requirements/**", "exclusive")])
    cfgp = os.path.join(wk.wt, ".claude", "wavecoord.json")

    def commit(start, cfg=None, files=(), mv=None):
        git(wk.wt, "checkout", "-q", "-f", "-B", wk.branch, start)
        git(wk.wt, "clean", "-fdq")  # no empty specs/ folder: SPECS/x must stay SPECS/x on a case-insensitive disk
        if cfg is not None:
            with open(cfgp) as f:
                c = json.load(f)
            c.update(cfg)
            with open(cfgp, "w") as f:
                json.dump(c, f)
        if mv:
            os.makedirs(os.path.dirname(os.path.join(wk.wt, mv[1])), exist_ok=True)
            git(wk.wt, "mv", mv[0], mv[1])
        for rel in files:
            p = os.path.join(wk.wt, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            new_node = rel.endswith("/_node.md") and not os.path.exists(p)
            with open(p, "a") as f:
                f.write(NODE if new_node else "---\nstatus: done\n---\n")  # a new _node.md is a spec record
        git(wk.wt, "add", "-A")
        git(wk.wt, "commit", "-q", "--no-verify", "--allow-empty", "-m", "c")
        return git(wk.wt, "rev-parse", "HEAD").strip()

    def gate(base, head, mode=None):
        out = io.StringIO()
        code = gatecheck.run(None, wk.task, 1, "%s..%s" % (base, head), None, "%s@%d" % (wk.task, wk.epoch), w.repo,
                             env=w.base_env("gate"), out=out, err=io.StringIO(), as_json=True, merge_mode=mode)
        d = json.loads(out.getvalue())
        return code, sorted(v["key"] for v in d.get("config_changes") or [] if v["kind"] == "spec_change")

    def key(p):
        return ["the spec tree (%s)" % p]

    try:
        tree = commit("origin/main", files=["specs/a/_node.md"])  # a spec tree, no plan key
        adapter = commit("origin/main", cfg={"plan": {"adapter": "specs"}})  # the spec tree, no files yet
        dotdir = commit("origin/main", cfg={"spec": {"dir": "./specs/."}}, files=["specs/a/_node.md"])
        moved = commit(tree, cfg={"spec": {"dir": "requirements"}}, mv=("specs", "requirements"))
        flat = commit("origin/main", cfg={"plan": {"adapter": "builtin"}}, files=["specs/openapi.yaml"])
        # no plan adapter and no spec record: the lead plans from the flat plan (lead/base.py, plan_store)
        stray = commit("origin/main", files=["specs/api.yaml"])
        cases = [
            ("a status line", tree, commit(tree, files=["specs/auth/s/r/tasks/t.md"]), None,
             (1, key("specs/auth/s/r/tasks/t.md"))),
            ("a new requirement", tree, commit(tree, files=["specs/auth/_node.md"]), None,
             (1, key("specs/auth/_node.md"))),
            ("a neighbour folder", tree, commit(tree, files=["specsx/notes.md"]), None, (0, [])),
            ("merge mode off", tree, commit(tree, files=["specs/auth/_node.md"]), "off",
             (0, key("specs/auth/_node.md"))),
            ("a rename out of the tree", tree, commit(tree, mv=("specs/a/_node.md", "specsx/a.md")), None,
             (1, key("specs/a/_node.md"))),
            ("plan adapter specs", adapter, commit(adapter, files=["specs/x/_node.md"]), None,
             (1, key("specs/x/_node.md"))),
            ("another case", adapter, commit(adapter, files=["SPECS/x/_node.md"]), None,
             (1, key("SPECS/x/_node.md"))),
            ("spec.dir ./specs/.", dotdir, commit(dotdir, files=["specs/a/b.md"]), None, (1, key("specs/a/b.md"))),
            ("the tree moved on the base side", moved, commit(tree, files=["specs/a/_node.md"]), None,
             (1, key("specs/a/_node.md"))),
            ("a flat plan repo", flat, commit(flat, files=["specs/openapi.yaml"]), None, (0, [])),
            ("a stray specs/ file, no plan adapter", stray, commit(stray, files=["specs/api.yaml"]), None, (0, [])),
            ("the first spec record in a task PR", stray, commit(stray, files=["specs/a/_node.md"]), None,
             (1, key("specs/a/_node.md"))),
        ]
        for what, base, head, mode, want in cases:
            got = gate(base, head, mode)
            if got != want:
                return "%s: gate-check gave %s, want %s" % (what, got, want)
        return None
    finally:
        git(wk.wt, "checkout", "-q", "-f", "-B", wk.branch, "origin/main")
        git(wk.wt, "clean", "-fdq")


def journal_retry_check(w):
    """The override journal: the first send is logged but its answer is lost, then a write is added before the
    retry (the same idempotency key). Every journaled write must reach the server. -> None or the failure."""
    from wavecoord.client import Client, Deadline
    env = w.base_env("agent")
    fence.write_override(env, 3600, "retry drill")
    try:
        ovr = fence.read_override(env)
        wk = w.workers[0]
        n1 = fence.journal_append(env, ovr, wk.agent, wk.task, wk.epoch, "a/x.py", "edit")["n"]
        w.proxy.fault = "truncated"  # the server logs the notice; the client gets half an answer
        try:
            left = fence.journal_flush(Client.from_env(agent=wk.agent, deadline=Deadline(5), env=env), env, wk.agent)
        finally:
            w.proxy.fault = None
        if left != 1:
            return "a flush with a lost answer removed entries (left %s, want 1)" % left
        n2 = fence.journal_append(env, ovr, wk.agent, wk.task, wk.epoch, "a/y.py", "edit")["n"]
        left = fence.journal_flush(Client.from_env(agent=wk.agent, deadline=Deadline(5), env=env), env, wk.agent)
        sent = set()
        for ev in w.coord.log.events:
            n = ev["payload"].get("notice") if ev["kind"] == "notify.post" else None
            if n and n.get("kind") == "fence_override":
                sent.update(e.get("n") for e in (n.get("detail") or {}).get("entries") or [])
        missing = sorted({n1, n2} - sent)
        if left or fence.journal_entries(env):
            return "the journal is not empty after one reachable flush (left %s)" % left
        if missing:
            return "%d journaled write(s) never reached the server (removed on a deduplicated ack)" % len(missing)
        return None
    finally:
        fence.remove_override(env)


def table_check(name):
    """Run one named check in a fresh world. -> None or the failure text."""
    ci = (name == "D08") and not CI_FS
    with fencedefects.ci_forced(ci):
        t = Table(ci=ci or None)
        try:
            return getattr(t, name)()
        finally:
            t.close()


def table_job(args):
    """Worker-process entry: (check name, defect to apply or None) -> (name, defect, failure or None)."""
    name, defect = args
    if defect:
        with fencedefects.patched(defect):
            return name, defect, table_check(name)
    return name, defect, table_check(name)


def pool():
    n = min(8, max(2, (os.cpu_count() or 2)))
    return ProcessPoolExecutor(max_workers=n, mp_context=multiprocessing.get_context("spawn"))


class DefectTest(unittest.TestCase):
    def test_table_checks_pass_without_defects(self):
        with pool() as ex:
            res = list(ex.map(table_job, [(n, None) for n in sorted(fencedefects.DEFECTS)]))
        for name, _d, failure in res:
            with self.subTest(defect=name):
                self.assertIsNone(failure)

    def test_every_defect_is_caught(self):
        from test_fence_invariant import FIXED_SEEDS
        k = int(os.environ.get("WAVECOORD_FENCE_DEFECT_SEEDS", "2"))
        jobs = [(s, name, None, (name == "D08") and not CI_FS) for name in sorted(fencedefects.DEFECTS)
                for s in FIXED_SEEDS[:k]]
        with pool() as ex:
            tables = ex.map(table_job, [(n, n) for n in sorted(fencedefects.DEFECTS)])
            futs = list(ex.map(fencedefects.run_seed, jobs))
            table_hits = {name: failure for name, _d, failure in tables}
        random_hits = {}
        for seed, name, failures, _st in futs:
            if failures:
                random_hits.setdefault(name, []).append((seed, failures[0]))
        rows, missed = [], []
        for name in sorted(fencedefects.DEFECTS):
            t = table_hits[name]
            r = random_hits.get(name)
            if t is None and not r:
                missed.append(name)
            rows.append("%s  table:%-6s random:%-14s %s" % (
                name, "caught" if t else "MISSED", ("caught (seed %s)" % r[0][0]) if r else "-",
                fencedefects.DEFECTS[name][0]))
        sys.stderr.write("\nseeded defects (%d seeds each in the simulation):\n  %s\n" % (k, "\n  ".join(rows)))
        self.assertEqual(missed, [], "defects that no test caught")


if __name__ == "__main__":
    unittest.main()
