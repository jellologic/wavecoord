"""Client side: run `wavecoord _remote <cmd>` on a host, locally or over ssh.

Remote hosts run the zipapp at `~/.wavecoord/bin/wavecoord-<hash>`. A remote
command exits 86 when that file is missing; the client then uploads it over
the same ssh connection (ControlMaster) and runs the command again.
"""

import json
import os
import shlex
import subprocess
import sys
import threading

from wavecoord import zipapp

from .common import EXIT_NEED_UPLOAD, PIPE, LauncherError, Unreachable, ensure_dir, wc_home

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ControlMaster=auto",
            "-o", "ControlPath=~/.ssh/wavecoord-%C", "-o", "ControlPersist=600",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4", "-o", "LogLevel=ERROR"]
REMOTE_HOME = "${WAVECOORD_HOME:-$HOME/.wavecoord}"


def ssh_exe():
    return os.environ.get("WAVECOORD_SSH") or "ssh"


def local_zipapp():
    """This build's zipapp as ~/.wavecoord/bin/wavecoord-<hash> on this machine (written once)."""
    d = ensure_dir(os.path.join(wc_home(), "bin"))
    p = os.path.join(d, "wavecoord-" + zipapp.self_hash())
    if not os.path.isfile(p):
        tmp = "%s.tmp.%d" % (p, os.getpid())
        with open(tmp, "wb") as f:
            f.write(zipapp.self_bytes())
        os.chmod(tmp, 0o755)
        os.replace(tmp, p)
    return p


class Host(object):
    _lock = threading.Lock()

    def __init__(self, cfg):
        self.cfg = cfg
        self.name = cfg["name"]
        self.local = not cfg.get("ssh")

    def ssh_argv(self):
        return [ssh_exe()] + SSH_OPTS + [self.cfg["ssh"]]

    def argv(self, cmd, args=()):
        if self.local:
            return [sys.executable or "python3", "-I", local_zipapp(), "_remote", cmd] + list(args)
        script = ('f="%s/bin/wavecoord-%s"; [ -f "$f" ] || exit %d; exec python3 -I "$f" _remote %s'
                  % (REMOTE_HOME, zipapp.self_hash(), EXIT_NEED_UPLOAD,
                     " ".join(shlex.quote(x) for x in [cmd] + list(args))))
        return self.ssh_argv() + [script]

    def upload(self):
        script = ('d="%s/bin" && mkdir -p "$d" && f="$d/wavecoord-%s" && cat > "$f.tmp.$$" && '
                  'chmod 755 "$f.tmp.$$" && mv "$f.tmp.$$" "$f" && '
                  'find "$d" -name "wavecoord-*" -mtime +30 -delete 2>/dev/null; true'
                  % (REMOTE_HOME, zipapp.self_hash()))
        try:
            p = subprocess.run(self.ssh_argv() + [script], input=zipapp.self_bytes(), stdout=PIPE, stderr=PIPE,
                               timeout=300)
        except subprocess.TimeoutExpired:
            raise Unreachable("%s: upload timed out" % self.name)
        if p.returncode != 0:
            raise Unreachable("%s: upload failed: %s" % (self.name, p.stderr.decode("utf-8", "replace")[-300:]))

    def _run(self, cmd, data, args, timeout):
        for attempt in (0, 1):
            try:
                p = subprocess.run(self.argv(cmd, args), input=data, stdout=PIPE, stderr=PIPE, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise Unreachable("%s: %s timed out after %ss" % (self.name, cmd, timeout))
            except OSError as e:
                raise Unreachable("%s: cannot run %s: %s" % (self.name, cmd, e))
            if p.returncode == EXIT_NEED_UPLOAD and not self.local and attempt == 0:
                self.upload()
                continue
            return p
        return p

    def call(self, cmd, payload=None, args=(), timeout=300):
        """Run a JSON command. -> the parsed last stdout line."""
        d = {"host": self.cfg}
        d.update(payload or {})
        return self._parse(cmd, self._run(cmd, json.dumps(d).encode("utf-8"), args, timeout))

    def call_raw(self, cmd, data, args=(), timeout=300):
        """Run a command that reads raw bytes on stdin (plugin-install, token-install)."""
        return self._parse(cmd, self._run(cmd, data, args, timeout))

    def _parse(self, cmd, p):
        err = p.stderr.decode("utf-8", "replace").strip()
        if p.returncode == 255 and not self.local:
            raise Unreachable("%s unreachable: %s" % (self.name, err[-300:] or "ssh failed"))
        lines = [l for l in p.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
        try:
            out = json.loads(lines[-1])
        except (IndexError, ValueError):
            raise LauncherError("%s: `%s` failed (exit %s): %s" % (self.name, cmd, p.returncode,
                                                                   (err or "no output")[-600:]))
        return out

    def stream(self, cmd, payload=None, args=()):
        """Run a streaming command; its output passes through. -> exit code."""
        d = {"host": self.cfg}
        d.update(payload or {})
        data = json.dumps(d).encode("utf-8")
        for attempt in (0, 1):
            p = subprocess.Popen(self.argv(cmd, args), stdin=PIPE)
            try:
                p.stdin.write(data)
                p.stdin.close()
            except BrokenPipeError:
                pass
            try:
                rc = p.wait()
            except KeyboardInterrupt:
                p.terminate()
                rc = p.wait()
            if rc == EXIT_NEED_UPLOAD and not self.local and attempt == 0:
                self.upload()
                continue
            return rc
        return rc

    def safe_call(self, cmd, payload=None, args=(), timeout=120):
        try:
            return self.call(cmd, payload, args, timeout)
        except LauncherError as e:
            return {"ok": False, "error": str(e), "unreachable": isinstance(e, Unreachable)}
