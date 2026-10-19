"""CLI commands of the core: serve, log, revert, rebuild, events, tokens.

cli.py registers these with `register(subparsers)`.
"""

import errno
import hashlib
import http.client
import json
import os
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from wavecoord import const

ROLES = ("agent", "lead", "ui", "gate")


def default_tokens_file(env=None):
    env = os.environ if env is None else env
    return env.get("WAVECOORD_TOKENS_FILE") or os.path.join(os.path.expanduser("~"), ".wavecoord", "tokens.json")


def repo_root(path=None):
    p = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path or os.getcwd(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if p.returncode != 0:
        raise SystemExit("wavecoord: not inside a git repository (use --repo)")
    return p.stdout.strip()


def client_token(env=None):
    """The bearer token: plugin option, WAVECOORD_TOKEN, WAVECOORD_TOKEN_FILE, ~/.wavecoord/token."""
    env = os.environ if env is None else env
    for k in ("CLAUDE_PLUGIN_OPTION_TOKEN", "WAVECOORD_TOKEN"):
        if env.get(k, "").strip():
            return env[k].strip()
    path = env.get("WAVECOORD_TOKEN_FILE") or os.path.join(os.path.expanduser("~"), ".wavecoord", "token")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def api(url, method, path, body=None, timeout=None, token=None):
    """Call the server. Return (status, parsed JSON body). Raise OSError when unreachable."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    from wavecoord.client import url_trusted
    tok = token or (client_token() if url_trusted(url) else None)  # never to a repo-named server
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=timeout or const.get("CLIENT_TIMEOUT_S")) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except ValueError:
            return e.code, None


def _url():
    from wavecoord.cli import resolve_coord_url
    url = resolve_coord_url()
    if not url:
        raise SystemExit("wavecoord: no coordinator configured (set WAVECOORD_URL or coord_url)")
    return url


def _print_result(status, body):
    print(json.dumps(body, indent=2, sort_keys=True))
    return 0 if status == 200 else 1


# ----- serve -----

def _lock(home):
    import fcntl
    os.makedirs(home, exist_ok=True)
    f = open(os.path.join(home, "serve.lock"), "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            f.close()
            return None
        raise
    return f


def _server_config(args, home):
    path = args.config or os.path.join(home, "server.json")
    cfg = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    elif args.config:
        raise SystemExit("wavecoord serve: no server config at %s" % path)
    for k in ("bind", "port", "tokens"):
        v = getattr(args, k)
        if v is not None:
            cfg[k if k != "tokens" else "tokens_file"] = v
    return cfg


def cmd_serve(args):
    from wavecoord.core import Coord
    from wavecoord.server import Server
    repo = os.path.abspath(args.repo) if args.repo else repo_root()
    coord = Coord(repo, home=args.home, push=not args.no_push)
    cfg = _server_config(args, coord.home)
    tokens = cfg.get("tokens_file") or default_tokens_file()
    if not os.path.isfile(tokens):
        sys.stderr.write("wavecoord serve: no tokens file at %s; run: wavecoord tokens provision --role lead\n"
                         % tokens)
        return 1
    lock = _lock(coord.home)
    if lock is None:
        sys.stderr.write("wavecoord serve: another server owns %s\n" % coord.home)
        return 1
    try:
        srv = Server(coord, bind=cfg.get("bind", "127.0.0.1"), port=int(cfg.get("port", const.DEFAULT_PORT)),
                     tokens=tokens, allow_cidrs=cfg.get("allow_cidrs"), allow_hosts=cfg.get("allow_hosts"),
                     allow_origins=cfg.get("allow_origins"), verbose=args.verbose)
    except (ValueError, OSError) as e:
        sys.stderr.write("wavecoord serve: %s\n" % e)
        return 1
    coord.open()
    from wavecoord.watchdog import Watchdog
    wd = Watchdog.from_config(coord, cfg.get("watchdog"))
    if wd is not None:
        wd.start()
    from wavecoord.lead import lead_for
    from wavecoord.specs.status import StatusLoop
    every = os.environ.get("WAVECOORD_STATUS_FLUSH_S")  # else spec.status_flush_s, read at each tick
    status_loop = StatusLoop(lead_for(coord), float(every) if every else None)
    status_loop.start()  # status commits of the spec tree; a no-op without one
    sys.stdout.write("wavecoord serve: %s project=%s log=%s\n" % (srv.url, coord.project, coord.log.log_dir))
    sys.stdout.flush()

    def stop(_sig, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stopping.set()
        if wd is not None:
            wd.stop()
        status_loop.stop()
        srv.httpd.server_close()
        coord.close()
        lock.close()
    return 0


# ----- log -----

def cmd_log(args):
    from wavecoord.core import load_consumer_config
    from wavecoord.log import LogError, read_branch
    repo = os.path.abspath(args.repo) if args.repo else repo_root()
    branch = args.branch or load_consumer_config(repo).get("log_branch") or const.DEFAULT_LOG_BRANCH
    try:
        events = read_branch(repo, branch, args.rev)
    except LogError as e:
        sys.stderr.write("wavecoord log: %s\n" % e)
        return 1
    if args.kind:
        events = [e for e in events if e["kind"].startswith(args.kind)]
    if args.n:
        events = events[-args.n:]
    for e in events:
        if args.json:
            print(json.dumps(e, sort_keys=True))
        else:
            who = e["actor"].get("agent") or e["actor"].get("principal") or "-"
            print("%6d %s %-18s %-24s %s" % (e["seq"], e["ts"], e["kind"], e["task"] or "-", who))
    if args.verify:
        sys.stderr.write("ok: %d events, chain verified\n" % len(read_branch(repo, branch, args.rev)))
    return 0


# ----- revert and rebuild -----

def cmd_revert(args):
    status, body = api(_url(), "POST", "/api/revert", {"seq": args.seq, "reason": args.reason or ""},
                       timeout=30)
    return _print_result(status, body)


def offline_rebuild(repo, home=None):
    """Rebuild the sqlite cache with no server running. Logs nothing."""
    from wavecoord.core import Coord
    coord = Coord(repo, home=home, push=False, threads=False)
    lock = _lock(coord.home)
    if lock is None:
        raise SystemExit("wavecoord rebuild: a server is running; it was not reachable at the configured URL")
    try:
        coord.log.open(start_threads=False)
        from wavecoord.store import Store
        coord.store = Store(os.path.join(coord.home, "cache.sqlite"))
        st = coord.state_at(coord.log.last_seq)
        coord.store.save(st, full=True)
        coord.store.close()
        return coord.log.last_seq
    finally:
        lock.close()


def cmd_rebuild(args):
    url = None
    from wavecoord.cli import resolve_coord_url
    url = resolve_coord_url()
    if args.at:
        if not url:
            raise SystemExit("wavecoord rebuild --at: needs a running coordinator (it appends a revert event)")
        status, body = api(url, "POST", "/api/revert", {"at_sha": args.at, "reason": args.reason or ""},
                           timeout=60)
        return _print_result(status, body)
    if url:
        try:
            status, body = api(url, "POST", "/api/rebuild", {}, timeout=300)
            return _print_result(status, body)
        except OSError:
            pass
    repo = os.path.abspath(args.repo) if args.repo else repo_root()
    seq = offline_rebuild(repo, args.home)
    print("rebuilt the cache from %d events (offline)" % seq)
    return 0


# ----- events -----

def follow_events(url, since=None, follow=True, out=None, token=None, max_events=None):
    """Print each SSE event as one JSON line. Reconnect with Last-Event-ID when following."""
    out = out or sys.stdout
    u = urllib.parse.urlsplit(url)
    last = since
    count = 0
    while True:
        conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(u.hostname, u.port or (443 if u.scheme == "https" else 80),
                        timeout=const.get("SSE_PING_S") * 3)
        path = (u.path.rstrip("/") + "/api/events") + ("" if follow else "?once=1")
        headers = {"Accept": "text/event-stream"}
        tok = token or client_token()
        if tok:
            headers["Authorization"] = "Bearer " + tok
        if last is not None:
            headers["Last-Event-ID"] = str(last)
        try:
            conn.request("GET", path, headers=headers)
            r = conn.getresponse()
            if r.status != 200:
                sys.stderr.write("wavecoord events: HTTP %d %s\n" % (r.status, r.read().decode("utf-8", "replace")))
                return 1
            ev_id, data = None, []
            while True:
                line = r.fp.readline()
                if not line:
                    break
                line = line.decode("utf-8").rstrip("\r\n")
                if line.startswith("id:"):
                    ev_id = line[3:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif line == "":
                    if data:
                        out.write("\n".join(data) + "\n")
                        out.flush()
                        count += 1
                        if ev_id is not None:
                            last = int(ev_id)
                        if max_events and count >= max_events:
                            return 0
                    ev_id, data = None, []
        except (OSError, http.client.HTTPException) as e:
            if not follow:
                sys.stderr.write("wavecoord events: %s\n" % e)
                return 1
        finally:
            conn.close()
        if not follow:
            return 0
        time.sleep(1.0)


def follow_lead(url, since=None, follow=True, out=None, token=None, state=None, cursor=True,
                as_json=False, max_lines=None, env=None):
    """`events --for lead`: one concise line per lead-relevant event (docs/feedback.md).

    Starts after `since`, else after the shared lead cursor in `state`, else at
    the next event. Each printed line moves the cursor, so the hooks do not
    inject the same event again.
    """
    from wavecoord import feedback
    from wavecoord.client import state_dir
    from wavecoord.hooks import coord_state_dir
    out = out or sys.stdout
    # the cursor of this coordinator, as the hooks keep it: <state dir>/coord-<sha(url)>/lead-cursor.json
    state = coord_state_dir(state if state is not None else state_dir(env), url)
    if since is None and cursor:
        since = feedback.read_cursor(state)
    count = 0
    try:
        from wavecoord.client import url_trusted
        tok = token or (client_token(env) if url_trusted(url, env) else None)
        for ev in feedback.sse_events(url, tok, since=since, follow=follow, agent="lead"):
            line = feedback.lead_line(ev)
            if line is not None:
                out.write((json.dumps(ev, sort_keys=True) if as_json else line) + "\n")
                out.flush()
                count += 1
            if cursor and isinstance(ev.get("seq"), int):
                feedback.write_cursor(state, ev["seq"])
            if max_lines and count >= max_lines:
                return 0
    except feedback.SseError as e:
        sys.stderr.write("wavecoord events: %s\n" % e)
        return 1
    except OSError as e:
        sys.stderr.write("wavecoord events: %s\n" % e)
        return 1
    return 0


def cmd_events(args):
    url = (args.url or "").strip().rstrip("/") or _url()
    try:
        if args.for_ == "lead":
            return follow_lead(url, since=args.since, follow=args.follow, cursor=not args.no_cursor,
                               as_json=args.json, state=(args.state_dir or "").strip() or None)
        return follow_events(url, since=args.since, follow=args.follow)
    except KeyboardInterrupt:
        return 0


# ----- tokens -----

def write_secret(path, text):
    """Write `text` to `path` with mode 600, also over an existing file with a looser mode:
    a new file is written and renamed over the old one, never the old file reopened."""
    d = os.path.dirname(os.path.abspath(path))
    tmp = os.path.join(d, ".%s.%d.tmp" % (os.path.basename(path), os.getpid()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def provision_token(path, role, principal):
    """Add a new token to the tokens file (digest only). Return the token."""
    data = {"version": 1, "tokens": {}}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    data["tokens"][digest] = {"principal": principal, "role": role, "created": const.fmt_ts(time.time())}
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return token


def cmd_tokens(args):
    if args.role == "agent":
        if not args.host:
            raise SystemExit("wavecoord tokens provision: --role agent needs --host <name>")
        principal = "host:" + args.host
    else:
        principal = args.role
    if args.out and os.path.dirname(os.path.abspath(args.out)):
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), mode=0o700, exist_ok=True)  # ~/.wavecoord/hosts/
    token = provision_token(args.tokens or default_tokens_file(), args.role, principal)
    if args.out:
        write_secret(args.out, token + "\n")
        print("wrote a %s token for %s to %s" % (args.role, principal, args.out))
    else:
        print(token)
    return 0


# ----- registration -----

def register(sub):
    sp = sub.add_parser("serve", help="run the coordinator server")
    sp.add_argument("--repo", help="the consumer repo (default: the git repo of the cwd)")
    sp.add_argument("--home", help="state dir (default: $WAVECOORD_HOME or ~/.wavecoord/<project>)")
    sp.add_argument("--bind")
    sp.add_argument("--port", type=int)
    sp.add_argument("--config", help="server config JSON (default: <home>/server.json)")
    sp.add_argument("--tokens", help="tokens file (default: ~/.wavecoord/tokens.json)")
    sp.add_argument("--no-push", action="store_true", help="do not push coord/log to origin")
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("log", help="print the coord/log events (reads git; no server needed)")
    sp.add_argument("--repo")
    sp.add_argument("--branch")
    sp.add_argument("--rev", help="read the log at this commit")
    sp.add_argument("-n", type=int, help="only the last N events")
    sp.add_argument("--kind", help="only kinds with this prefix, for example lease")
    sp.add_argument("--json", action="store_true", help="one JSON event per line")
    sp.add_argument("--verify", action="store_true", help="verify the hash chain")
    sp.set_defaults(func=cmd_log)

    sp = sub.add_parser("revert", help="revert one coordination event (appends a revert event)")
    sp.add_argument("seq", type=int)
    sp.add_argument("--reason")
    sp.set_defaults(func=cmd_revert)

    sp = sub.add_parser("rebuild", help="rebuild the state cache from the log, or roll back with --at")
    sp.add_argument("--at", metavar="SHA", help="roll state back to this coord/log commit")
    sp.add_argument("--reason")
    sp.add_argument("--repo")
    sp.add_argument("--home")
    sp.set_defaults(func=cmd_rebuild)

    sp = sub.add_parser("events", help="print coordination events, one JSON per line")
    sp.add_argument("--follow", "-f", action="store_true", help="keep streaming (for a Monitor)")
    sp.add_argument("--since", type=int, help="start after this seq")
    sp.add_argument("--for", dest="for_", choices=("lead",),
                    help="only the events the lead acts on, one concise line each (docs/feedback.md)")
    sp.add_argument("--json", action="store_true", help="with --for: print the filtered events as JSON")
    sp.add_argument("--url", help="coordinator URL; empty means the usual lookup (WAVECOORD_URL, config)")
    sp.add_argument("--state-dir", help="with --for: the hook state dir that holds the lead cursor")
    sp.add_argument("--no-cursor", action="store_true",
                    help="with --for: do not read or move the shared lead cursor")
    sp.set_defaults(func=cmd_events)

    sp = sub.add_parser("tokens", help="manage bearer tokens")
    tsub = sp.add_subparsers(dest="tokens_cmd", metavar="<action>")
    tsub.required = True
    for name in ("provision", "issue"):
        tp = tsub.add_parser(name, help="add a token and print it once")
        tp.add_argument("--role", choices=ROLES, default="lead")
        tp.add_argument("--host", help="the host name, for --role agent")
        tp.add_argument("--tokens", help="tokens file (default: ~/.wavecoord/tokens.json)")
        tp.add_argument("--out", help="write the token to this file (mode 600) instead of stdout")
        tp.set_defaults(func=cmd_tokens)
