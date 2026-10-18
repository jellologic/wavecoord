"""`wavecoord install-server`: run `wavecoord serve` as a user service.

It copies the running zipapp (pinned by its content hash) to
`~/.wavecoord/bin/wavecoord-<hash>` and writes a user unit that runs
`serve` for one consumer repo from that copy:

- macOS: a launchd agent, `~/Library/LaunchAgents/io.github.wavecoord.<project>.plist`
- Linux: a systemd user unit, `~/.config/systemd/user/wavecoord-<project>.service`

An update of the plugin does not change a running server: install again to
move the pin. `--dry-run` prints the plan and changes nothing.
"""

import json
import os
import re
import shlex
import subprocess
import sys
from xml.sax.saxutils import escape

from wavecoord import const

LABEL_PREFIX = "io.github.wavecoord."
NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# WAVECOORD_* variables of a worker or a session, never a server's: not carried into the unit
NOT_CARRIED = ("WAVECOORD_TASK", "WAVECOORD_ATTEMPT", "WAVECOORD_AGENT_ID", "WAVECOORD_WORKTREE",
               "WAVECOORD_BRANCH", "WAVECOORD_HOST", "WAVECOORD_ROLE", "WAVECOORD_LEASE_ID",
               "WAVECOORD_LEASE_EPOCH", "WAVECOORD_SESSION_ID")
SECRET_RE = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|KEY", re.I)


def _home():
    return os.path.expanduser("~")


def wc_root():
    return os.path.join(_home(), ".wavecoord")


def safe_name(project):
    return NAME_RE.sub("-", project).strip("-.") or "default"


def carry_env(environ):
    """The WAVECOORD_* variables of `environ` that the unit keeps (WAVECOORD_SSH, WAVECOORD_HOME, ...).

    Worker/session variables and any name that looks like a secret (TOKEN, KEY, ...;
    a `*_FILE` path is fine) are left out: the unit file is not a secret store."""
    out = {}
    for k, v in environ.items():
        if not k.startswith("WAVECOORD_") or k in NOT_CARRIED:
            continue
        if SECRET_RE.search(k[len("WAVECOORD_"):]) and not k.endswith("_FILE"):
            continue
        out[k] = v
    return out


def parse_env_args(items):
    """`--env KEY=VALUE` words -> dict. Raises ValueError."""
    out = {}
    for it in items or ():
        k, sep, v = it.partition("=")
        if not sep or not ENV_KEY_RE.match(k):
            raise ValueError("--env needs KEY=VALUE, not %r" % it)
        if k in ("HOME",):
            raise ValueError("--env cannot set HOME")
        out[k] = v
    return out


def plan(repo, platform=None, port=None, bind=None, tokens=None, home=None, no_push=False, python=None,
         path_env=None, zipapp_bytes=None, extra_env=None):
    """-> the install plan: {zipapp, zipapp_hash, unit_path, unit, argv, env, start, stop, log}. Pure.

    `extra_env` ({KEY: VALUE}) is added to the unit's environment (PATH too; never HOME)."""
    from wavecoord import zipapp
    from wavecoord.core import load_consumer_config
    platform = platform or ("darwin" if sys.platform == "darwin" else "linux")
    if platform not in ("darwin", "linux"):
        raise ValueError("install-server supports macOS (launchd) and Linux (systemd --user), not %s" % platform)
    repo = os.path.abspath(repo)
    project = load_consumer_config(repo).get("project") or os.path.basename(repo)
    data = zipapp_bytes if zipapp_bytes is not None else zipapp.self_bytes()
    h = zipapp.content_hash(data)
    target = os.path.join(wc_root(), "bin", "wavecoord-" + h)
    argv = [python or sys.executable or "python3", "-I", target, "serve", "--repo", repo]
    if home:
        argv += ["--home", os.path.abspath(home)]
    if bind:
        argv += ["--bind", bind]
    if port:
        argv += ["--port", str(int(port))]
    if tokens:
        argv += ["--tokens", os.path.abspath(tokens)]
    if no_push:
        argv.append("--no-push")
    log_dir = os.path.abspath(home) if home else const.home_dir(project, env={})
    log = os.path.join(log_dir, "serve.log")
    env = {"PATH": path_env if path_env is not None else os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": _home()}
    for k, v in (extra_env or {}).items():
        if k != "HOME":
            env[k] = v
    name = safe_name(project)
    if platform == "darwin":
        label = LABEL_PREFIX + name
        unit_path = os.path.join(_home(), "Library", "LaunchAgents", label + ".plist")
        unit = launchd_plist(label, argv, repo, env, log)
        domain = "gui/%d" % os.getuid()
        start = [["launchctl", "bootout", "%s/%s" % (domain, label)], ["launchctl", "bootstrap", domain, unit_path]]
        stop = [["launchctl", "bootout", "%s/%s" % (domain, label)]]
    else:
        label = "wavecoord-%s.service" % name
        unit_path = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(_home(), ".config"),
                                 "systemd", "user", label)
        unit = systemd_unit(project, argv, repo, env, log)
        start = [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", label],
                 ["systemctl", "--user", "restart", label]]
        stop = [["systemctl", "--user", "disable", "--now", label]]
    return {"platform": platform, "project": project, "repo": repo, "zipapp": target, "zipapp_hash": h,
            "label": label, "unit_path": unit_path, "unit": unit, "argv": argv, "env": env, "log": log, "start": start,
            "stop": stop}


def launchd_plist(label, argv, cwd, env, log):
    def s(v):
        return "<string>%s</string>" % escape(v)
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
             '<plist version="1.0">', "<dict>",
             "  <key>Label</key>", "  " + s(label),
             "  <key>ProgramArguments</key>", "  <array>"] + ["    " + s(a) for a in argv] + [
             "  </array>",
             "  <key>WorkingDirectory</key>", "  " + s(cwd),
             "  <key>EnvironmentVariables</key>", "  <dict>"]
    for k in sorted(env):
        lines += ["    <key>%s</key>" % escape(k), "    " + s(env[k])]
    lines += ["  </dict>",
              "  <key>RunAtLoad</key>", "  <true/>",
              "  <key>KeepAlive</key>", "  <dict>", "    <key>SuccessfulExit</key>", "    <false/>", "  </dict>",
              "  <key>ThrottleInterval</key>", "  <integer>10</integer>",
              "  <key>StandardOutPath</key>", "  " + s(log),
              "  <key>StandardErrorPath</key>", "  " + s(log),
              "</dict>", "</plist>", ""]
    return "\n".join(lines)


def _sd_quote(v):
    """A systemd command-line word: double quotes, with backslash, quote and % escaped."""
    return '"%s"' % v.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


def systemd_unit(project, argv, cwd, env, log):
    lines = ["[Unit]", "Description=wavecoord coordinator for %s" % project,
             "After=network.target", "", "[Service]", "Type=simple",
             "WorkingDirectory=%s" % _sd_quote(cwd),
             "ExecStart=%s" % " ".join(_sd_quote(a) for a in argv)]
    for k in sorted(env):
        lines.append("Environment=%s" % _sd_quote("%s=%s" % (k, env[k])))
    lines += ["Restart=on-failure", "RestartSec=5",
              "StandardOutput=append:%s" % log, "StandardError=append:%s" % log,
              "", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def _write(path, data, mode):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def install(p, start=True, run=None):
    """Copy the zipapp, write the unit, (re)start it. -> list of commands run."""
    from wavecoord import zipapp
    run = run or (lambda argv: subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                              universal_newlines=True))
    if not os.path.isfile(p["zipapp"]):
        _write(p["zipapp"], zipapp.self_bytes(), 0o755)
    os.makedirs(os.path.dirname(p["log"]), exist_ok=True)
    _write(p["unit_path"], p["unit"].encode("utf-8"), 0o644)
    done = []
    if start:
        for i, argv in enumerate(p["start"]):
            r = run(argv)
            done.append(argv)
            # `launchctl bootout` of an agent that is not loaded fails; that is fine
            if r.returncode != 0 and not (p["platform"] == "darwin" and i == 0):
                raise RuntimeError("%s failed (exit %s): %s" % (" ".join(argv), r.returncode,
                                                                 (r.stderr or r.stdout or "").strip()[-300:]))
    return done


def cmd_install_server(args):
    from wavecoord.corecli import default_tokens_file, repo_root
    repo = os.path.abspath(args.repo) if args.repo else repo_root()
    tokens = args.tokens or default_tokens_file()
    try:
        extra = {} if args.no_carry_env else carry_env(os.environ)
        extra.update(parse_env_args(args.env))
        p = plan(repo, platform=args.platform, port=args.port, bind=args.bind, tokens=args.tokens,
                 home=args.home, no_push=args.no_push, extra_env=extra)
    except ValueError as e:
        sys.stderr.write("wavecoord install-server: %s\n" % e)
        return 1
    if args.uninstall:
        if args.dry_run:
            out = {"dry_run": True, "run": p["stop"], "remove": [p["unit_path"]]}
            print(json.dumps(out, indent=1) if args.json else "\n".join(
                ["would run: " + " ".join(shlex.quote(a) for a in c) for c in p["stop"]] +
                ["would remove: " + p["unit_path"]]))
            return 0
        for argv in p["stop"]:
            subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            os.remove(p["unit_path"])
        except OSError:
            pass
        print("removed %s (the zipapp copy %s stays)" % (p["unit_path"], p["zipapp"]))
        return 0
    if not os.path.isfile(tokens):
        sys.stderr.write("wavecoord install-server: warning: no tokens file at %s; the server will not start until"
                         " you run: wavecoord tokens provision --role lead --out ~/.wavecoord/token\n" % tokens)
    if args.dry_run:
        if args.json:
            print(json.dumps(dict(p, dry_run=True), indent=1, sort_keys=True))
        else:
            print("would copy the zipapp to %s" % p["zipapp"])
            print("would write %s:" % p["unit_path"])
            print(p["unit"])
            for argv in ([] if args.no_start else p["start"]):
                print("would run: " + " ".join(shlex.quote(a) for a in argv))
        return 0
    try:
        install(p, start=not args.no_start)
    except (OSError, RuntimeError) as e:
        sys.stderr.write("wavecoord install-server: %s\n" % e)
        return 1
    print("installed %s\n  zipapp %s\n  log %s%s" % (p["unit_path"], p["zipapp"], p["log"],
                                                     "" if args.no_start else "\n  started " + p["label"]))
    return 0


def register(sub):
    sp = sub.add_parser("install-server", help="run `wavecoord serve` as a user service (launchd or systemd --user)")
    sp.add_argument("--repo", help="the consumer repo (default: the git repo of the cwd)")
    sp.add_argument("--port", type=int, help="port (default: the server config, else %d)" % const.DEFAULT_PORT)
    sp.add_argument("--bind", help="bind address (default 127.0.0.1)")
    sp.add_argument("--tokens", help="tokens file (default: ~/.wavecoord/tokens.json)")
    sp.add_argument("--home", help="server state dir (default: ~/.wavecoord/<project>)")
    sp.add_argument("--no-push", action="store_true", help="do not push coord/log to origin")
    sp.add_argument("--env", action="append", metavar="KEY=VALUE",
                    help="add a variable to the unit's environment (repeatable; overrides a carried one)")
    sp.add_argument("--no-carry-env", action="store_true",
                    help="do not carry this shell's WAVECOORD_* variables (WAVECOORD_SSH, ...) into the unit")
    sp.add_argument("--platform", choices=("darwin", "linux"), help="unit type (default: this OS)")
    sp.add_argument("--no-start", action="store_true", help="write the files, do not start the service")
    sp.add_argument("--uninstall", action="store_true", help="stop the service and remove its unit")
    sp.add_argument("--dry-run", action="store_true", help="print what would happen; change nothing")
    sp.add_argument("--json", action="store_true", help="with --dry-run: print the plan as JSON")
    sp.set_defaults(func=cmd_install_server)
