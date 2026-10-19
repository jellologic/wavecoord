"""Small helpers shared by the client side and the host side of the launcher."""

import fcntl
import json
import os
import random
import re
import signal
import subprocess
import threading
import time

DEVNULL = subprocess.DEVNULL
PIPE = subprocess.PIPE

EXIT_NEED_UPLOAD = 86
ACTIVE = ("starting", "running", "finishing")
FINAL = ("reported", "failed", "timed_out", "stopped", "lost")
PROTECTED_BRANCHES = ("main", "master", "trunk", "develop", "HEAD")
ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}-[0-9]{1,6}$")
TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HB_INTERVAL_S = 30.0


class LauncherError(Exception):
    pass


class Unreachable(LauncherError):
    pass


def const(name, default):
    """A contract constant, overridable in tests by WAVECOORD_<NAME> (contract.md section 5)."""
    v = os.environ.get("WAVECOORD_" + name)
    if v is None:
        return default
    try:
        return type(default)(v)
    except ValueError:
        return default


def now():
    return time.time()


def iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t if t is not None else now()))


def wc_home():
    """The wavecoord state root on this machine: $WAVECOORD_HOME, else ~/.wavecoord."""
    h = os.environ.get("WAVECOORD_HOME") or os.path.join(os.path.expanduser("~"), ".wavecoord")
    return os.path.abspath(os.path.expanduser(h))


def ensure_dir(p, mode=0o755):
    os.makedirs(p, mode=mode, exist_ok=True)
    return p


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    tmp = "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def tail_text(path, nbytes):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "replace")
    if size > nbytes:
        text = "...\n" + text.split("\n", 1)[-1]
    return text


def rand4():
    return "%04x" % random.SystemRandom().randrange(65536)


class Flock(object):
    """An exclusive flock on a file; blocking unless blocking=False."""

    def __init__(self, path, blocking=True):
        self.path = path
        self.blocking = blocking
        self.f = None

    def __enter__(self):
        self.f = open(self.path, "a+")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB))
        except OSError:
            self.f.close()
            self.f = None
            raise
        return self

    def __exit__(self, *a):
        if self.f is None:
            return
        try:
            fcntl.flock(self.f, fcntl.LOCK_UN)
        finally:
            self.f.close()
            self.f = None


def lock_held(path):
    """Whether another process holds the flock on path. A missing file is never held."""
    if not os.path.exists(path):
        return False
    lk = Flock(path, blocking=False)
    try:
        lk.__enter__()
    except OSError:
        return True
    lk.__exit__()
    return False


def update_status(d, **kw):
    p = os.path.join(d, "status.json")
    with Flock(os.path.join(d, ".status.lock")):
        st = read_json(p, {}) or {}
        st.update(kw)
        write_json(p, st)
    return st


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True


def pgroup_alive(pgid):
    if not pgid:
        return False
    try:
        os.killpg(int(pgid), 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def signal_group(pgid, sig):
    try:
        os.killpg(int(pgid), sig)
    except (OSError, ValueError, TypeError):
        pass


def wait_group_gone(pgid, timeout):
    end = now() + timeout
    while now() < end and pgroup_alive(pgid):
        time.sleep(0.2)
    return not pgroup_alive(pgid)


def kill_group(pgid, grace=5.0):
    """SIGTERM a process group, then SIGKILL after `grace` seconds."""
    if not pgroup_alive(pgid):
        return
    signal_group(pgid, signal.SIGTERM)
    if not wait_group_gone(pgid, grace):
        signal_group(pgid, signal.SIGKILL)


def run_cmd(cmd, cwd, env=None, timeout=600, stdin=None):
    """Run a config command: an array without a shell, a string with `sh -c`.

    Returns (rc, stdout, stderr). A missing executable gives rc 127."""
    argv = ["sh", "-c", cmd] if isinstance(cmd, str) else list(cmd)
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, input=stdin, stdout=PIPE, stderr=PIPE,
                           universal_newlines=True, timeout=timeout,
                           stdin=None if stdin is not None else DEVNULL)
    except FileNotFoundError as e:
        return 127, "", str(e)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ss" % timeout
    return p.returncode, p.stdout, p.stderr


def git_run(args, cwd, timeout=600, env=None):
    try:
        p = subprocess.run(["git"] + list(args), cwd=cwd, stdin=DEVNULL, stdout=PIPE, stderr=PIPE,
                           universal_newlines=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, "", "git %s timed out" % " ".join(args)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def git(args, cwd, timeout=600, env=None):
    rc, out, err = git_run(args, cwd, timeout, env)
    if rc != 0:
        raise LauncherError("git %s: %s" % (" ".join(args), (err or out)[-800:]))
    return out


def expand_env_value(v):
    """Expand ~ and $VARS in a host env value (so "PATH": "$HOME/bin:$PATH" works)."""
    return os.path.expanduser(os.path.expandvars(str(v)))


# Environment variables of a parent Claude Code session that must not leak into a worker. The
# messaging pair is the parent session's own inbox (a worker binds its own).
STRIP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_MESSAGING_SOCKET",
             "CLAUDE_CODE_MESSAGING_TOKEN")


def host_env(host_cfg, extra=None):
    env = dict(os.environ)
    for k in STRIP_ENV:
        env.pop(k, None)
    for k, v in (host_cfg.get("env") or {}).items():
        env[k] = expand_env_value(v)
    env["WAVECOORD_HOST"] = host_cfg["name"]
    if extra:
        env.update({k: str(v) for k, v in extra.items() if v is not None})
    return env
