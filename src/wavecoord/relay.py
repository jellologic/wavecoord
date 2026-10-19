"""`wavecoord inbox-relay`: push coordination messages into a worker session that wavecoord did not launch.

A worker that the launcher runs gets its messages through its stdin
(launcher/steer.py). A worker session that a person started (WAVECOORD_TASK set,
no WAVECOORD_ATTEMPT_DIR) has no such parent, so its SessionStart hook starts
this relay. The relay polls the session's inbox every INBOX_POLL_S and writes
new messages into the session's Claude Code inbox socket
(CLAUDE_CODE_MESSAGING_SOCKET): an auth line with the session's token, then one
user line. Claude Code reads it between tool calls, or starts a turn when the
session is idle. The relay never acks: a write to the socket is not proof that
the session got the message (Claude Code sends no reply). The relay records
what it pushed (pushed-<agent>.json), and the hooks ack a pushed message once
the session's transcript shows it. A message that the transcript does not show
after RELAY_CONFIRM_S goes through UserPromptSubmit, PostToolBatch and Stop.

The socket's line format is not documented by Claude Code; it is what Claude
Code 2.1.302 prints in its own debug log (docs/hooks.md, "Claude Code facts").

    {"type":"auth","token":"<CLAUDE_CODE_MESSAGING_TOKEN>"}
    {"type":"user","message":{"role":"user","content":"..."}}

One relay per agent (a lock in the state dir). It ends when the socket is
gone, the Claude Code process is gone, or after RELAY_MAX_S.
"""

import fcntl
import hashlib
import json
import os
import socket
import subprocess
import sys
import time

from wavecoord.client import Client, Deadline, Unreachable, const, state_dir

RELAY_MAX_S = 12 * 3600
PUSH_TIMEOUT_S = 2.0
PUSHED_MAX = 500


def _key(agent):
    return hashlib.sha256(agent.encode("utf-8")).hexdigest()[:16]


def relay_dir(env, url):
    from wavecoord.hooks import coord_state_dir
    return coord_state_dir(state_dir(env), url)


def pushed_path(env, agent, url=None):
    return os.path.join(relay_dir(env, url), "pushed-%s.json" % _key(agent))


def read_pushed(env, agent, url=None):
    """{message id: the time the relay pushed it}."""
    try:
        with open(pushed_path(env, agent, url), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    out = {}
    for k, v in d.items():
        try:
            out[int(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def write_pushed(env, agent, pushed, url=None):
    path = pushed_path(env, agent, url)
    keep = sorted(pushed.items(), key=lambda kv: kv[1])[-PUSHED_MAX:]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".%d.tmp" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in keep}, f)
        os.replace(tmp, path)
    except OSError:
        pass


def push(sock_path, token, text, timeout=PUSH_TIMEOUT_S):
    """Write one user message into a Claude Code inbox socket. -> True when the write went through
    (not proof of delivery: the hooks confirm it in the transcript)."""
    lines = []
    if token:
        lines.append(json.dumps({"type": "auth", "token": token}))
    lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False))
    data = ("\n".join(lines) + "\n").encode("utf-8")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        s.sendall(data)
        try:
            s.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        return True
    except OSError:
        return False
    finally:
        s.close()


def _alive(pid):
    if not pid:
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def run_once(env, agent, sock_path, token):
    """One poll: fetch the inbox and push what is new; the hooks ack it. -> the number of messages pushed."""
    from urllib.parse import quote
    from wavecoord.hooks import frame
    from wavecoord.launcher.steer import HEADER
    c = Client.from_env(agent=agent, deadline=Deadline(const("CLIENT_TIMEOUT_S", env) + 2), env=env)
    if c is None:
        return 0
    try:
        status, resp = c.get("/api/inbox?agent=" + quote(agent, safe=""))
    except Unreachable:
        return 0
    msgs = resp.get("messages") if status == 200 and isinstance(resp, dict) else None
    pushed = read_pushed(env, agent, c.url)
    fresh = [m for m in msgs or [] if isinstance(m, dict) and m.get("id") is not None
             and m["id"] not in pushed and not m.get("acked")]
    if not fresh:
        return 0
    text = "\n".join([HEADER] + [frame(m) for m in fresh])
    if not push(sock_path, token, text):
        return 0
    at = time.time()
    pushed.update({m["id"]: at for m in fresh})
    write_pushed(env, agent, pushed, c.url)
    return len(fresh)


def run(env, agent, sock_path, token, ppid, max_s=RELAY_MAX_S):
    lock_path = os.path.join(relay_dir(env, env.get("WAVECOORD_URL")), "relay-%s.lock" % _key(agent))
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # another relay serves this agent
        end = time.time() + max_s
        every = const("INBOX_POLL_S", env)
        while time.time() < end and os.path.exists(sock_path) and _alive(ppid):
            try:
                run_once(env, agent, sock_path, token)
            except Exception:  # noqa: BLE001 - the relay is best effort; the hooks deliver anyway
                pass
            time.sleep(every)
    return 0


def main(stdin=None, env=None):
    """`wavecoord inbox-relay`: {"agent", "socket", "token", "ppid"} on stdin (never on the command line)."""
    env = dict(os.environ if env is None else env)
    try:
        cfg = json.loads((stdin or sys.stdin).read())
    except ValueError:
        return 2
    if not isinstance(cfg, dict) or not cfg.get("agent") or not cfg.get("socket"):
        return 2
    if cfg.get("url"):
        env["WAVECOORD_URL"] = cfg["url"]  # the coordinator of the session that started the relay
    return run(env, cfg["agent"], cfg["socket"], cfg.get("token"), cfg.get("ppid"))


def start(env, agent, sock_path, token, ppid, url=None):
    """Start a detached relay for this session (SessionStart). It exits at once when one already runs."""
    from wavecoord import zipapp
    p = zipapp.self_path()
    argv = [sys.executable or "python3"] + (["-I", p] if p else ["-m", "wavecoord"]) + ["inbox-relay"]
    cfg = json.dumps({"agent": agent, "socket": sock_path, "token": token, "ppid": ppid, "url": url})
    child_env = {k: v for k, v in env.items() if k not in ("CLAUDE_CODE_MESSAGING_TOKEN",)}
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=child_env, start_new_session=True, close_fds=True)
        proc.stdin.write(cfg.encode("utf-8"))
        proc.stdin.close()
        return True
    except OSError:
        return False
