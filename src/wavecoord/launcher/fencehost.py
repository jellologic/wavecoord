"""The launcher's part of the edit fence (docs/design/edit-fence.md), host side.

All fence logic of the wrapper is here, so that wrapper.py only calls it:

- `setup`: the fence modes from the default branch's .claude/wavecoord.json
  (never the worktree copy, which the agent can write), into WAVECOORD_FENCE.
- `preflight`: in enforce mode, refuse a run with no lease; in edit enforce
  mode, refuse a claude older than 2.1.295 (no `onFailure`) and a user mod
  that handles `tool.check` (it can approve a call that the hook denied).
- `settings_pins`: worker --settings keys that pin the fence (disableAllHooks
  false, and the fence env), above the worktree's local and project settings.
- `prepush`: E3, the check of the net diff before the launcher pushes, with
  the attempt's agent, task and epoch (never the wrapper's env).
- `report_fields`: E4, changed_paths and lease_epoch for the report.
"""

import json
import os
import re
import subprocess

from wavecoord import fence, fencecore

MIN_CLAUDE = (2, 1, 295)
MOD_SCAN_MAX_FILES = 5000


CREDENTIAL_ENV = ("CLAUDE_PLUGIN_OPTION_TOKEN", "WAVECOORD_TOKEN_FILE", "WAVECOORD_TOKEN", "WAVECOORD_TOKENS_FILE")


def _token_role(token, env):
    """The role of `token` in the server's tokens file when this machine has it, else None."""
    import hashlib
    path = os.path.expanduser(env.get("WAVECOORD_TOKENS_FILE") or os.path.join("~", ".wavecoord", "tokens.json"))
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    ent = (data.get("tokens") or {}).get(hashlib.sha256(token.encode("utf-8")).hexdigest())
    return ent.get("role") if isinstance(ent, dict) else None


def _read(p):
    try:
        with open(p, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def worker_token_file(host_cfg, host_name, env=None):
    """The token file a worker may use: only an agent token of its host, never a lead or server token.

    Candidates, in order: <wavecoord home>/hosts/<host>.token (the agent token that the launcher
    keeps per host), then, on a remote host only, the host's installed token. On a local host the
    installed token is the lead's own (~/.wavecoord/token), so it is never a candidate. A token that
    the tokens file on this machine lists with another role than agent is refused.
    """
    from .common import wc_home
    from . import coord as coordmod
    env = os.environ if env is None else env
    cands = [os.path.join(wc_home(), "hosts", host_name + ".token")]
    if host_cfg.get("ssh"):
        cands.append(coordmod.token_path())
    lead = _read(os.path.join(wc_home(), "token")) if not host_cfg.get("ssh") else None
    lead_opt = (env.get("CLAUDE_PLUGIN_OPTION_TOKEN") or "").strip() or None
    for p in cands:
        t = _read(p)
        if not t or t in (lead, lead_opt):
            continue
        if _token_role(t, env) not in (None, "agent"):
            continue
        return p
    return None


def worker_credentials(attempt):
    """Strip lead and server credentials from the worker env and give it only its host agent token."""
    for k in CREDENTIAL_ENV:
        attempt.env.pop(k, None)
    t = attempt.task
    p = worker_token_file(t.get("hostcfg") or {}, t.get("host") or "local")
    if p:
        attempt.env["WAVECOORD_TOKEN_FILE"] = p
    return p


def default_branch_modes(wt, default, remote="origin"):
    """fence modes from <remote>/<default>:.claude/wavecoord.json; the defaults when absent."""
    try:
        p = subprocess.run(["git", "show", "%s/%s:.claude/wavecoord.json" % (remote, default)], cwd=wt,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           universal_newlines=True, timeout=60)
        cfg = json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        cfg = {}
    return fencecore.parse_modes(cfg.get("fence") if isinstance(cfg, dict) else None)


def setup(attempt):
    """Set the attempt's fence modes and WAVECOORD_FENCE in its env. -> modes."""
    try:
        m = default_branch_modes(attempt.wt, attempt.default)
    except Exception:  # noqa: BLE001
        m = dict(fencecore.DEFAULT_MODES)
    over = os.environ.get("WAVECOORD_FENCE_FORCE")  # tests and operators: a stricter mode for this host
    if over:
        m = fencecore.stricter(m, fencecore.parse_modes(json.loads(over) if over.startswith("{") else over))
    attempt.fence_modes = m
    attempt.task["fence"] = m
    attempt.env["WAVECOORD_FENCE"] = json.dumps(m, sort_keys=True)
    return m


def parse_version(text):
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def claude_version(argv0, env):
    try:
        p = subprocess.run([argv0, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_version(p.stdout) if p.returncode == 0 else None


def _mod_roots(env):
    cfg = env.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    roots = [os.path.join(cfg, d) for d in ("plugins", "dev-mods", "skills")]
    for d in (env.get("CLAUDE_CODE_PLUGIN_DIRS") or "").split(os.pathsep):
        if d.strip():
            roots.append(os.path.expanduser(d.strip()))
    return roots


def tool_check_mods(env, skip=()):
    """hooks.json files of mods whose modules handle `tool.check` (best effort; see docs/hooks.md)."""
    found, seen = [], 0
    skip = [os.path.realpath(s) for s in skip if s]
    for root in _mod_roots(env):
        if not os.path.isdir(root):
            continue
        for d, dirs, files in os.walk(root):
            seen += len(files)
            if seen > MOD_SCAN_MAX_FILES:
                return found
            if any(os.path.realpath(d).startswith(s) for s in skip):
                dirs[:] = []
                continue
            dirs[:] = [x for x in dirs if x not in ("node_modules", ".git", "types")]
            if "hooks.json" not in files:
                continue
            p = os.path.join(d, "hooks.json")
            try:
                with open(p, encoding="utf-8") as f:
                    mods = json.load(f).get("modules")
            except (OSError, ValueError, AttributeError):
                continue
            for m in mods if isinstance(mods, list) else []:
                try:
                    with open(os.path.join(d, str(m)), encoding="utf-8", errors="replace") as f:
                        if "tool.check" in f.read():
                            found.append(p)
                            break
                except OSError:
                    continue
    return found


def preflight(attempt, claude_argv0):
    """None when the run may start, else the reason the fence refuses it."""
    m = getattr(attempt, "fence_modes", None) or fencecore.DEFAULT_MODES
    if not fencecore.any_enforce(m):
        return None
    if not attempt.spec.get("lease_epoch"):
        return "fence: refused: the fence is in enforce mode and the attempt has no lease (the claim failed)"
    if m["edit"] == "enforce":
        v = claude_version(claude_argv0, attempt.env)
        if v is None or v < MIN_CLAUDE:
            return ("fence: refused: edit enforce mode needs Claude Code %s or later (onFailure: block); this host"
                    " has %s" % (".".join(map(str, MIN_CLAUDE)), ".".join(map(str, v)) if v else "an unknown version"))
        mods = tool_check_mods(attempt.env, skip=[attempt.task.get("plugin_dir")])
        if mods:
            return ("fence: refused: a user mod handles tool.check and can approve an edit that the fence denied:"
                    " %s" % ", ".join(mods[:3]))
    return None


def settings_pins(task):
    """Keys for the worker --settings: they win over the worktree's local and project settings."""
    pins = {"disableAllHooks": False}
    env = {"WAVECOORD_ROLE": "worker", "WAVECOORD_TASK": str(task.get("task") or ""),
           "WAVECOORD_AGENT_ID": str(task.get("agent") or "")}
    spec = task.get("spec") or {}
    if spec.get("lease_epoch"):
        env["WAVECOORD_LEASE_EPOCH"] = str(spec["lease_epoch"])
    if task.get("worktree"):
        env["WAVECOORD_WORKTREE"] = task["worktree"]
    if task.get("attempt_dir"):
        env["WAVECOORD_ATTEMPT_DIR"] = task["attempt_dir"]
    if task.get("fence"):
        env["WAVECOORD_FENCE"] = json.dumps(task["fence"], sort_keys=True)
    pins["env"] = env
    return pins


def prepush(attempt, res, base):
    """E3. -> True when the push may go on. On a refusal, res["push_error"] says why."""
    m = getattr(attempt, "fence_modes", None) or fencecore.DEFAULT_MODES
    if m["push"] == "off":
        return True
    t = attempt.task
    out = fence.push_check(attempt.wt, base, t["agent"], t["task"], attempt.spec.get("lease_epoch"), attempt.env)
    res["changed_paths"] = out["paths"]
    res["fence_push"] = {k: out[k] for k in ("ok", "kind", "detail", "seq")}
    res["fence_push"]["violations"] = out["violations"][:20]
    if out["ok"]:
        attempt.log("fence push check: ok (%d paths)" % len(out["paths"]))
        return True
    why = "fence: %s: %s" % (out["kind"], out["detail"] or "")
    if m["push"] != "enforce":
        attempt.log("fence push check (advisory): would refuse: %s" % why)
        return True
    attempt.log("fence push check: refused: %s" % why)
    res["push_error"] = why[:600]
    return False


def report_fields(attempt, res):
    out = {}
    if isinstance(res.get("changed_paths"), list):
        out["changed_paths"] = res["changed_paths"][:5000]
    try:
        e = int(attempt.spec.get("lease_epoch") or 0)
    except (TypeError, ValueError):
        e = 0
    if e > 0:
        out["lease_epoch"] = e
    return out
