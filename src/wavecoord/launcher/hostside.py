"""Host side of the launcher: `wavecoord _remote <cmd>`.

The client runs `python3 -I ~/.wavecoord/bin/wavecoord-<hash> _remote <cmd> [args]`
on a host (over ssh, or as a local process) with a JSON payload on stdin, and
reads one JSON object from the last line of stdout. `logs` streams instead.

State on the host, under the wavecoord home (`~/.wavecoord`):

    bin/wavecoord-<hash>           the uploaded zipapp
    plugin-<hash>/                 the pinned plugin copy (--plugin-dir of every worker)
    attempts/<project>/<attempt>/  one attempt: status.json, task.json, prompt.md,
                                   stream.jsonl, stderr.log, wrapper.log, result.json, ...
    locks/                         slot and worktree-admin locks
    spool/                         coordinator writes that wait for a reachable server
    token                          the host's agent token (mode 600), if the client copied one
"""

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

from wavecoord import zipapp

from . import coord as coordmod
from . import providers, sessions
from .common import (ACTIVE, ATTEMPT_ID_RE, DEVNULL, FINAL, PIPE, Flock, LauncherError, ensure_dir, iso,
                     lock_held, now, pid_alive, read_json, tail_text, update_status, wc_home, write_json)
from .worktrees import (dirty_count, prepare_worktree, remove_worktree, unpushed_commits)

LAUNCH_WAIT_S = 30.0
RUN_LOCK = ".run.lock"


# ---- paths -------------------------------------------------------------------------------------

def attempts_root():
    return ensure_dir(os.path.join(wc_home(), "attempts"))


def project_dir(project):
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", project or ""):
        raise LauncherError("bad project name %r" % project)
    return ensure_dir(os.path.join(attempts_root(), project))


def attempt_dir(project, aid):
    if not ATTEMPT_ID_RE.match(aid or ""):
        raise LauncherError("bad attempt id %r" % aid)
    return os.path.join(project_dir(project), aid)


def plugin_copy_dir(h):
    return os.path.join(wc_home(), "plugin-" + h)


def slots_lock_path(host_name):
    return os.path.join(ensure_dir(os.path.join(wc_home(), "locks")), "slots-%s.lock" % host_name)


def expand_repo(path):
    return os.path.abspath(os.path.expanduser(path))


# ---- views -------------------------------------------------------------------------------------

def attempt_view(d):
    st = read_json(os.path.join(d, "status.json"))
    if st is None:
        return None
    task = read_json(os.path.join(d, "task.json"), {}) or {}
    state = st.get("state")
    held = lock_held(os.path.join(d, RUN_LOCK))  # the wrapper runs while it holds its run lock
    if state in ACTIVE:
        live = held
        if not live and state == "starting":
            live = pid_alive(st.get("pid"))
        if not live:
            state = "lost"
    started = st.get("started") or st.get("created")
    ended = st.get("ended") if state in FINAL else None
    return {
        "id": os.path.basename(d),
        "project": task.get("project") or os.path.basename(os.path.dirname(d)),
        "host": task.get("host") or st.get("host"),
        "task": task.get("task") or st.get("task"),
        "attempt": task.get("attempt") or st.get("attempt"),
        "branch": task.get("branch") or st.get("branch"),
        "agent": task.get("agent"),
        "state": state,
        "note": st.get("note"),
        "created": st.get("created"),
        "started": started,
        "ended": ended,
        "elapsed_s": ((ended or now()) - started) if started else None,
        "session_id": st.get("session_id") or task.get("session_id"),
        "turns": st.get("turns"),
        "cost_usd": st.get("cost_usd"),
        "pr_url": st.get("pr_url"),
        "report_status": st.get("report_status"),
        "worktree": task.get("worktree"),
        "provider": (task.get("spec") or {}).get("provider") or providers.ANTHROPIC,
        "throttled": st.get("throttled"),
        "running": bool(held),
    }


def iter_attempt_dirs(project=None):
    root = attempts_root()
    projects = [project] if project else sorted(os.listdir(root))
    for p in projects:
        pd = os.path.join(root, p)
        if not os.path.isdir(pd):
            continue
        for n in sorted(os.listdir(pd)):
            d = os.path.join(pd, n)
            if os.path.isfile(os.path.join(d, "status.json")):
                yield d


def count_active(host_name):
    """Attempts that take a slot: an active state, or a wrapper that still runs (whatever its status says)."""
    n = 0
    for d in iter_attempt_dirs():
        v = attempt_view(d)
        if v and (v["state"] in ACTIVE or v["running"]) and v["host"] == host_name:
            n += 1
    return n


def project_attempt_ids(project):
    """The attempt ids of `project` on this host (the client counts them when it numbers an attempt)."""
    try:
        return sorted(os.path.basename(d) for d in iter_attempt_dirs(project))
    except LauncherError:
        return []


# ---- probe -------------------------------------------------------------------------------------

def mem_avail_gib():
    try:
        with open("/proc/meminfo") as f:
            m = {}
            for line in f:
                k, v = line.split(":", 1)
                m[k] = int(v.split()[0]) * 1024
        return m.get("MemAvailable", m.get("MemFree", 0)) / 2.0 ** 30
    except (OSError, ValueError, IndexError):
        pass
    try:  # macOS
        vm = subprocess.run(["vm_stat"], stdout=PIPE, stderr=DEVNULL, universal_newlines=True, timeout=10).stdout
        page = int(re.search(r"page size of (\d+)", vm).group(1))
        pages = 0
        for key in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"):
            mm = re.search(key + r":\s+(\d+)", vm)
            if mm:
                pages += int(mm.group(1))
        return pages * page / 2.0 ** 30
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        return None


def disk_free_gib(path):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize / 2.0 ** 30
    except OSError:
        return None


def claude_bin(env=None):
    return (env or os.environ).get("WAVECOORD_CLAUDE") or "claude"


def claude_info(env):
    exe = shutil.which(claude_bin(env), path=env.get("PATH"))
    info = {"installed": bool(exe), "path": exe, "version": None, "auth": "no"}
    if not exe:
        return info
    try:
        p = subprocess.run([exe, "--version"], stdin=DEVNULL, stdout=PIPE, stderr=PIPE,
                           universal_newlines=True, timeout=60, env=env)
        info["version"] = (p.stdout.strip() or p.stderr.strip()).splitlines()[0][:80] if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, IndexError):
        pass
    info["auth"] = claude_auth(env)
    return info


def claude_auth(env):
    """Whether claude has credentials. Presence only: it never reads a secret."""
    if env.get("ANTHROPIC_API_KEY") or env.get("CLAUDE_CODE_OAUTH_TOKEN") or env.get("ANTHROPIC_AUTH_TOKEN"):
        return "env"
    cfg = env.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    if os.path.isfile(os.path.join(cfg, ".credentials.json")):
        return "logged-in"
    if sys.platform == "darwin":
        return "keychain?"  # macOS keeps the login in the keychain; not checked
    return "no"


def file_sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def r_probe(payload, args):
    from .common import host_env
    cfg = payload["host"]
    env = host_env(cfg)
    repo = expand_repo(cfg["repo_path"])
    slots = int(cfg.get("slots", 1))
    active = count_active(cfg["name"])
    out = {
        "ok": True, "host": cfg["name"], "hostname": socket.gethostname(),
        "python": sys.version.split()[0], "wavecoord": zipapp.self_hash(),
        "slots": slots, "active": active, "free": max(0, slots - active),
        "mem_avail_gib": mem_avail_gib(), "disk_free_gib": disk_free_gib(repo if os.path.isdir(repo) else "/"),
        "repo_ok": os.path.exists(os.path.join(repo, ".git")),
        "claude": claude_info(env),
        "plugin_present": bool(payload.get("plugin_hash")) and
        os.path.isfile(os.path.join(plugin_copy_dir(payload["plugin_hash"]), ".claude-plugin", "plugin.json")),
        "token_sha": file_sha(coordmod.token_path()),
        "providers": providers.probe(payload.get("providers") or {}),
    }
    if payload.get("project"):
        out["attempt_ids"] = project_attempt_ids(payload["project"])
    if payload.get("flush_spool", True):
        out["spool"] = coordmod.flush_spool()
    url = payload.get("coord_url")
    if url:
        try:
            # no token to /api/health; on a remote host the server must prove it is the coordinator,
            # since any local user can bind the loopback port first (security.md, "Remote hosts")
            token = coordmod.read_token() if cfg.get("ssh") else None
            reachable, verified = coordmod.verify_coordinator(url, cfg["name"], token)
            out["coord_reachable"] = reachable and verified is not False
            if cfg.get("ssh"):
                out["coord_verified"] = verified
        except LauncherError:
            out["coord_reachable"] = False
    return out


# ---- uploads -----------------------------------------------------------------------------------

def r_plugin_install(payload, args, data):
    h = args[0]
    if not re.match(r"^[0-9a-f]{8,64}$", h):
        raise LauncherError("bad plugin hash %r" % h)
    if zipapp.content_hash(data) != h:
        raise LauncherError("plugin archive hash mismatch")
    dest = plugin_copy_dir(h)
    if os.path.isfile(os.path.join(dest, ".claude-plugin", "plugin.json")):
        return {"ok": True, "already": True, "path": dest}
    tmp = "%s.tmp.%d" % (dest, os.getpid())
    shutil.rmtree(tmp, ignore_errors=True)
    zipapp.safe_extract(data, tmp)
    try:
        os.rename(tmp, dest)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)  # a concurrent install won
    return {"ok": True, "path": dest}


def r_token_install(payload, args, data):
    token = data.decode("utf-8").strip()
    if not token or "\n" in token:
        raise LauncherError("bad token")
    p = coordmod.token_path()
    ensure_dir(os.path.dirname(p), 0o700)
    tmp = "%s.tmp.%d" % (p, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return {"ok": True, "token_sha": file_sha(p)}


# ---- launch ------------------------------------------------------------------------------------

def self_argv():
    """argv prefix that runs this same wavecoord build."""
    p = zipapp.self_path()
    if p:
        return [sys.executable or "python3", "-I", p]
    return [sys.executable or "python3", "-m", "wavecoord"]


def r_launch(payload, args):
    cfg = payload["host"]
    spec = payload["spec"]
    project = payload["project"]
    aid = spec["attempt_id"]
    d = attempt_dir(project, aid)
    prov = spec.get("provider") or providers.ANTHROPIC
    if not providers.is_builtin(prov, spec.get("provider_profile")):
        why = providers.check_env_file((spec.get("provider_profile") or {}).get("env_file") or "")
        if why:
            return {"ok": False, "error": "refusing to start %s on %s: provider %s: %s"
                    % (aid, cfg["name"], prov, why)}
    plugin_dir = plugin_copy_dir(payload["plugin_hash"]) if payload.get("plugin_hash") else None
    if plugin_dir and not os.path.isdir(plugin_dir):
        return {"ok": False, "need_plugin": True, "error": "plugin copy %s missing" % plugin_dir}
    prev = None
    if os.path.exists(os.path.join(d, "status.json")):
        v = attempt_view(d)
        if not spec.get("continue"):
            return {"ok": False, "exists": True, "id": aid, "state": v["state"],
                    "error": "attempt %s already exists on %s (state %s)" % (aid, cfg["name"], v["state"])}
        if v["state"] not in FINAL or v["running"]:
            return {"ok": False, "error": "attempt %s is still %s" % (aid, "running" if v["running"] else v["state"])}
        prev = True
    prev_task, prev_result = {}, {}
    with Flock(slots_lock_path(cfg["name"])):
        active = count_active(cfg["name"])
        slots = int(cfg.get("slots", 1))
        if active >= slots and not payload.get("force"):
            return {"ok": False, "full": True, "active": active, "slots": slots,
                    "error": "%s is full (%d/%d slots)" % (cfg["name"], active, slots)}
        if prev:
            # --continue: keep the earlier run's files in prev-<time>/
            archive = os.path.join(d, "prev-%d" % int(now() * 1000))
            os.makedirs(archive)
            for n in os.listdir(d):
                if not n.startswith("prev-") and not n.startswith("."):
                    os.replace(os.path.join(d, n), os.path.join(archive, n))
            prev_task = read_json(os.path.join(archive, "task.json"), {}) or {}
            prev_result = read_json(os.path.join(archive, "result.json"), {}) or {}
        ensure_dir(d)
        write_json(os.path.join(d, "status.json"),
                   {"state": "starting", "created": now(), "pid": os.getpid(), "host": cfg["name"],
                    "task": spec["task"], "attempt": spec["attempt"], "branch": spec["branch"]})
    wt, repo, made = None, None, False
    try:
        repo = expand_repo(cfg["repo_path"])
        wt, base_sha, default = prepare_worktree(repo, spec["branch"], aid, cont=bool(spec.get("continue")),
                                                 log_branch=payload.get("log_branch"), project=project)
        made = not spec.get("continue")
        # --continue: the sessions of the earlier runs, the first run's (the lineage root) first. The
        # wrapper resumes the first one whose transcript exists, else it starts a fresh session.
        candidates, spec_prompt = [], spec.get("prompt") or ""
        if spec.get("continue") and prev:
            if prev_task.get("worktree") == wt:
                candidates = sessions.lineage(d, prev_task, prev_result)
            spec_prompt = sessions.first_prompt(d, prev_task) or spec_prompt
        resume = candidates[0] if candidates else None
        task = {
            "id": aid, "project": project, "host": cfg["name"], "task": spec["task"],
            "attempt": spec["attempt"], "branch": spec["branch"], "agent": spec["agent"],
            "session_id": spec["session_id"], "resume": resume, "resume_candidates": candidates,
            "session_root": prev_task.get("session_root") if candidates else None,
            "session_lineage": list(prev_task.get("session_lineage") or []) if prev else [],
            "spec_prompt": spec_prompt, "worktree": wt, "repo": repo,
            "base_sha": base_sha, "default_branch": default, "spec": spec, "hostcfg": cfg,
            "coord_url": payload.get("coord_url"), "pr_cmd": payload.get("pr_cmd"),
            "plugin_dir": plugin_dir, "continue": bool(spec.get("continue")),
            "prev_pr_url": prev_result.get("pr_url"),
            "created": now(),
        }
        with open(os.path.join(d, "prompt.md"), "w", encoding="utf-8") as f:
            f.write(spec.get("prompt") or "")
        write_json(os.path.join(d, "task.json"), task)
        logf = open(os.path.join(d, "wrapper.log"), "ab")
        p = subprocess.Popen(self_argv() + ["_remote", "wrap", project, aid], stdin=DEVNULL, stdout=logf,
                             stderr=subprocess.STDOUT, cwd=wt, start_new_session=True, close_fds=True,
                             env=dict(os.environ, WAVECOORD_HOME=wc_home()))
        logf.close()
        end = now() + LAUNCH_WAIT_S
        while now() < end:
            st = read_json(os.path.join(d, "status.json"), {}) or {}
            if st.get("state") != "starting" or p.poll() is not None:
                break
            time.sleep(0.1)
        st = read_json(os.path.join(d, "status.json"), {}) or {}
        if st.get("state") == "starting":
            if p.poll() is None:
                return {"ok": False, "error": "wrapper did not start within %ss" % LAUNCH_WAIT_S, "id": aid}
            why = "wrapper exited at start: %s" % tail_text(os.path.join(d, "wrapper.log"), 600)
            update_status(d, state="failed", ended=now(), note="wrapper exited at start (see wrapper.log)")
            out = {"ok": False, "error": why, "id": aid}
            if not prev:
                out["moved_to"] = discard_failed(d, repo, wt if made else None, spec["branch"])
            return out
    except Exception as e:  # noqa: BLE001 - the launch failure is recorded on the attempt
        update_status(d, state="failed", ended=now(), note="launch failed: %s" % e)
        out = {"ok": False, "error": "launch on %s failed: %s" % (cfg["name"], e), "id": aid}
        if not prev:
            out["moved_to"] = discard_failed(d, repo, wt if made else None, spec["branch"])
        return out
    return {"ok": True, "id": aid, "host": cfg["name"], "worktree": wt, "base_sha": base_sha,
            "default_branch": default, "pid": p.pid, "state_dir": d}


def discard_failed(d, repo, wt, branch):
    """A new attempt that failed before its wrapper ran: move its dir to <project>/.failed/<id>-<ms>
    (kept for diagnosis, out of the attempt list) and remove the worktree and branch it made, so the
    next dispatch can use the same number. -> the new path of the dir."""
    if wt and repo:
        try:
            remove_worktree(repo, wt, branch)
        except LauncherError:
            pass
    dest = os.path.join(ensure_dir(os.path.join(os.path.dirname(d), ".failed")),
                        "%s-%d" % (os.path.basename(d), int(now() * 1000)))
    try:
        os.replace(d, dest)
    except OSError:
        return None
    return dest


# ---- the other commands ------------------------------------------------------------------------

def r_ls(payload, args):
    rows = []
    for d in iter_attempt_dirs(payload.get("project")):
        v = attempt_view(d)
        if v and (not payload.get("ids") or v["id"] in payload["ids"]):
            rows.append(v)
    return {"ok": True, "attempts": rows}


def r_result(payload, args):
    d = attempt_dir(payload["project"], args[0])
    v = attempt_view(d) if os.path.isdir(d) else None
    if v is None:
        return {"ok": False, "error": "no attempt %s on %s" % (args[0], payload["host"]["name"])}
    out = dict(read_json(os.path.join(d, "result.json"), {}) or {})
    out.update(v)
    out["ok"] = True
    out["state_dir"] = d
    return out


def r_stop(payload, args):
    import signal
    d = attempt_dir(payload["project"], args[0])
    v = attempt_view(d) if os.path.isdir(d) else None
    if v is None:
        return {"ok": False, "error": "no attempt %s" % args[0]}
    if v["state"] in FINAL and v["state"] != "lost":
        return {"ok": True, "already": True, "state": v["state"]}
    st = read_json(os.path.join(d, "status.json"), {}) or {}
    if st.get("pid") and v["state"] != "lost":
        try:
            os.kill(int(st["pid"]), signal.SIGTERM)
        except OSError:
            pass
    end = now() + float(payload.get("wait_s", 90))
    while now() < end:
        v = attempt_view(d)
        if v["state"] in FINAL:
            break
        time.sleep(0.3)
    v = attempt_view(d)
    if v["running"] and v["state"] not in FINAL:
        # The wrapper still holds its run lock (a long prepare_cmd, fetch or push): it records the final
        # state itself. Writing `stopped` now would free its slot and let gc remove its worktree.
        from .common import kill_group
        kill_group((read_json(os.path.join(d, "status.json"), {}) or {}).get("claude_pid"), grace=3.0)
        return {"ok": True, "state": v["state"], "stopping": True,
                "note": "the wrapper still runs; it ends the attempt when its current step returns"}
    if v["state"] not in FINAL or v["state"] == "lost":
        from .common import kill_group
        kill_group(st.get("claude_pid"), grace=3.0)
        update_status(d, state="stopped", ended=st.get("ended") or now(), note="stopped (wrapper not responding)")
        v = attempt_view(d)
    return {"ok": True, "state": v["state"]}


def r_logs(payload, args):
    d = attempt_dir(payload["project"], args[0])
    if not os.path.isdir(d):
        sys.stderr.write("wavecoord: no attempt %s\n" % args[0])
        return 1
    files = [("wrapper.log", "# "), ("stream.jsonl", ""), ("stderr.log", "! ")]
    raw = bool(payload.get("raw"))
    w = sys.stdout

    def emit(name, prefix, line):
        if name == "stream.jsonl" and not raw:
            r = render_stream_line(line)
            if r:
                w.write(r + "\n")
        else:
            w.write(prefix + line + "\n")

    if not payload.get("follow"):
        n = payload.get("tail")
        for name, prefix in files:
            p = os.path.join(d, name)
            if not os.path.exists(p):
                continue
            with open(p, "rb") as f:
                lines = f.read().decode("utf-8", "replace").splitlines()
            for line in (lines[-n:] if n else lines):
                emit(name, prefix, line)
        w.flush()
        return 0
    pos, buf, quiet = {}, {}, 0
    while True:
        got = False
        for name, prefix in files:
            try:
                with open(os.path.join(d, name), "rb") as f:
                    f.seek(pos.get(name, 0))
                    data = f.read()
                    pos[name] = f.tell()
            except OSError:
                continue
            if not data:
                continue
            got = True
            lines = (buf.get(name, "") + data.decode("utf-8", "replace")).split("\n")
            buf[name] = lines.pop()
            for line in lines:
                emit(name, prefix, line)
        w.flush()
        if attempt_view(d)["state"] in FINAL and not got:
            quiet += 1
            if quiet >= 2:
                return 0
        else:
            quiet = 0
        time.sleep(0.5)


def render_stream_line(line):
    try:
        e = json.loads(line)
    except ValueError:
        return line
    t, st = e.get("type"), e.get("subtype")
    if t == "assistant":
        parts = []
        for b in (e.get("message") or {}).get("content") or []:
            if b.get("type") == "text":
                parts.append(b.get("text") or "")
            elif b.get("type") == "tool_use":
                parts.append("[tool] %s %s" % (b.get("name"), json.dumps(b.get("input"))[:200]))
        return "\n".join(parts) or None
    if t == "result":
        return "[result %s, %s turns, $%s]" % (e.get("subtype"), e.get("num_turns"), e.get("total_cost_usd"))
    if t == "system" and st == "api_retry":
        return "[api_retry %s/%s %s]" % (e.get("attempt"), e.get("max_retries"), e.get("error"))
    if t == "system" and st == "init":
        return "[init model=%s session=%s]" % (e.get("model"), e.get("session_id"))
    if t == "rate_limit_event":
        return "[rate_limit %s]" % json.dumps(e.get("rate_limit_info"))[:200]
    return None


def r_gc(payload, args):
    """Remove worktrees of finished attempts that have no unpushed commits and no changes."""
    removed, kept = [], []
    for d in iter_attempt_dirs(payload.get("project")):
        v = attempt_view(d)
        task = read_json(os.path.join(d, "task.json"), {}) or {}
        wt = task.get("worktree")
        if not v or v["state"] not in FINAL or v["state"] == "lost" or v["running"] or not wt or not os.path.isdir(wt):
            continue
        if v["host"] != payload["host"]["name"]:
            continue
        unpushed = unpushed_commits(wt)
        dirty = dirty_count(wt)
        if unpushed is None or dirty is None or unpushed or dirty:
            kept.append({"id": v["id"], "worktree": wt,
                         "why": "unknown git state" if unpushed is None or dirty is None else
                         "%d unpushed commits, %d changed files" % (len(unpushed), dirty)})
            continue
        if payload.get("dry_run"):
            removed.append({"id": v["id"], "worktree": wt, "dry_run": True})
            continue
        try:
            remove_worktree(task["repo"], wt, task.get("branch"))
            update_status(d, gc=iso())
            removed.append({"id": v["id"], "worktree": wt})
        except LauncherError as e:
            kept.append({"id": v["id"], "worktree": wt, "why": str(e)})
    return {"ok": True, "removed": removed, "kept": kept}


# ---- entry point -------------------------------------------------------------------------------

JSON_CMDS = {"probe": r_probe, "launch": r_launch, "ls": r_ls, "result": r_result, "stop": r_stop, "gc": r_gc}
RAW_CMDS = {"plugin-install": r_plugin_install, "token-install": r_token_install}


def main(argv):
    if not argv:
        sys.stderr.write("wavecoord _remote: missing command\n")
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "wrap":
        from .wrapper import wrap
        return wrap(args[0], args[1])
    try:
        if cmd in RAW_CMDS:
            data = sys.stdin.buffer.read()
            out = RAW_CMDS[cmd]({}, args, data)
        else:
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
            if cmd == "logs":
                return r_logs(payload, args)
            if cmd not in JSON_CMDS:
                sys.stderr.write("wavecoord _remote: unknown command %s\n" % cmd)
                return 2
            out = JSON_CMDS[cmd](payload, args)
    except (LauncherError, ValueError, KeyError, OSError) as e:
        out = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
    return 0
