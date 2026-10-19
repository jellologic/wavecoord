"""`wavecoord doctor`: check every prerequisite of a wavecoord setup, one line per check.

Each check is ok, warn or fail, and a check that is not ok says the command that fixes it.
The exit code is 1 when any check fails. Doctor only reads: it never writes a file, never
calls a model, and sends the user's token only to a coordinator the user configured
(`client.url_trusted`). `--quick` skips the host probes (the slowest check).
"""

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from wavecoord import __version__, const

OK, WARN, FAIL = "ok", "warn", "fail"
CONFIG_REL = ".claude/wavecoord.json"
PLUGIN_NAME = "wavecoord"
DEFAULT_PLUGIN_ID = "wavecoord@wavecoord"
INSTALL_PLUGIN = "claude plugin marketplace add jellologic/wavecoord && claude plugin install wavecoord@wavecoord"
HTTP_TIMEOUT_S = 3.0
CMD_TIMEOUT_S = 30
COLORS = {OK: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m"}
RESET = "\033[0m"


def check(name, status, detail, fix=None):
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def run(argv, cwd=None, timeout=CMD_TIMEOUT_S, env=None, stdin_text=None):
    """-> (rc, stdout, stderr); rc is None when the program is missing or timed out."""
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, input=stdin_text,
                           stdin=None if stdin_text is not None else subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "", str(e)
    return p.returncode, p.stdout, p.stderr


def git(args, cwd, timeout=CMD_TIMEOUT_S):
    rc, out, err = run(["git"] + list(args), cwd=cwd, timeout=timeout)
    return rc, out.strip(), err.strip()


def home_wc():
    return os.path.join(os.path.expanduser("~"), ".wavecoord")


def lead_token_path(env=None):
    env = os.environ if env is None else env
    return env.get("WAVECOORD_TOKEN_FILE") or os.path.join(home_wc(), "token")


def host_token_path(host):
    return os.path.join(home_wc(), "hosts", host + ".token")


def tilde(path):
    """`path` with the home folder as ~, for the fix lines."""
    h = os.path.expanduser("~")
    return "~" + path[len(h):] if path == h or path.startswith(h + os.sep) else path


def repo_top(cwd):
    rc, out, _ = git(["rev-parse", "--show-toplevel"], cwd)
    return out if rc == 0 and out else None


def default_branch(top, network=True):
    """-> (name or None, how). origin/HEAD, else `ls-remote --symref` (network), else origin/main|master."""
    rc, out, _ = git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], top)
    if rc == 0 and out.startswith("origin/"):
        return out[len("origin/"):], "origin/HEAD"
    if network:
        rc, out, _ = git(["ls-remote", "--symref", "origin", "HEAD"], top, timeout=15)
        m = re.search(r"^ref: refs/heads/(\S+)\s+HEAD", out or "", re.M) if rc == 0 else None
        if m:
            return m.group(1), "ls-remote"
    for cand in ("main", "master"):
        if git(["show-ref", "--verify", "--quiet", "refs/remotes/origin/" + cand], top)[0] == 0:
            return cand, "origin/" + cand
    return None, None


def pending_setup_branch(top, cfg, prefix="wavecoord/setup"):
    """The branch on origin (as last fetched) that `wavecoord init --pr` pushed with this same config, or None."""
    rc, out, _ = git(["for-each-ref", "--format=%(refname)", "refs/remotes/origin/" + prefix + "*"], top)
    for ref in (out.splitlines() if rc == 0 else []):
        rc2, text, _ = git(["show", "%s:%s" % (ref, CONFIG_REL)], top)
        try:
            if rc2 == 0 and json.loads(text) == cfg:
                return ref[len("refs/remotes/origin/"):]
        except ValueError:
            continue
    return None


def token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def read_tokens_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f) or {}).get("tokens") or {}
    except (OSError, ValueError, AttributeError):
        return None


def read_secret(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def http_get(url, token=None, timeout=HTTP_TIMEOUT_S):
    """-> (status or None, parsed body or None, error text)."""
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "null"), ""
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except ValueError:
            body = None
        return e.code, body, str(e)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, None, str(getattr(e, "reason", e))


def claude_exe(env=None):
    from wavecoord.launcher.hostside import claude_bin
    env = os.environ if env is None else env
    return shutil.which(claude_bin(env), path=env.get("PATH"))


def plugin_entry(exe):
    """-> (entry or None, error or None) from `claude plugin list --json`: the enabled wavecoord entry first."""
    rc, out, err = run([exe, "plugin", "list", "--json"])
    if rc != 0:
        return None, (err or out).strip()[-200:] or "claude plugin list failed"
    try:
        rows = json.loads(out)
    except ValueError:
        return None, "claude plugin list printed no JSON"
    hits = [r for r in rows if isinstance(r, dict) and str(r.get("id", "")).split("@")[0] == PLUGIN_NAME]
    hits.sort(key=lambda r: not r.get("enabled"))
    return (hits[0] if hits else None), None


def plugin_options(exe, plugin_id):
    """-> the `claude plugin configure <id> --json` answer, or None."""
    rc, out, _ = run([exe, "plugin", "configure", plugin_id, "--json"])
    if rc != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def settings_enable(top):
    """Whether a Claude Code settings file enables a wavecoord plugin (when `claude plugin list` cannot tell)."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    paths = [os.path.join(cfg, "settings.json")]
    if top:
        paths += [os.path.join(top, ".claude", "settings.json"), os.path.join(top, ".claude", "settings.local.json")]
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                enabled = (json.load(f) or {}).get("enabledPlugins") or {}
        except (OSError, ValueError, AttributeError):
            continue
        for k, v in enabled.items():
            if k.split("@")[0] == PLUGIN_NAME and v:
                return p
    return None


class Doctor(object):
    def __init__(self, cwd=None, env=None, quick=False):
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.env = os.environ if env is None else env
        self.quick = quick
        self.top = None
        self.default = None
        self.cfg_raw = None
        self.cfg_ok = False
        self.url = None
        self.server_cfg = None
        self.coord_up = False
        self.plugin_id = None
        self.claude = None
        self.out = []

    def add(self, *a, **kw):
        self.out.append(check(*a, **kw))

    def run(self):
        for step in (self.c_python, self.c_git, self.c_gh, self.c_claude, self.c_plugin, self.c_config,
                     self.c_coord_url, self.c_coordinator, self.c_lead_token, self.c_host_tokens, self.c_hosts,
                     self.c_service, self.c_plan):
            try:
                step()
            except Exception as e:  # noqa: BLE001 - one broken check never hides the others
                self.add(step.__name__[2:].replace("_", " "), FAIL, "the check crashed: %s: %s"
                         % (type(e).__name__, e), "report this at https://github.com/jellologic/wavecoord/issues")
        return self.out

    # ----- tools -----

    def c_python(self):
        v = sys.version_info
        text = "Python %d.%d.%d (%s)" % (v[0], v[1], v[2], sys.executable or "python3")
        if v[:2] >= (3, 9):
            self.add("python", OK, text)
        else:
            self.add("python", FAIL, text + "; wavecoord needs 3.9 or later", "install Python 3.9 or later")

    def c_git(self):
        rc, out, _ = git(["--version"], self.cwd)
        if rc != 0:
            self.add("git", FAIL, "git is not on PATH", "install git (https://git-scm.com)")
            return
        self.add("git", OK, out)
        self.top = repo_top(self.cwd)
        if not self.top:
            self.add("repo", FAIL, "%s is not inside a git repository" % self.cwd,
                     "cd into your project's repo (or: git init)")
            return
        self.add("repo", OK, self.top)
        rc, url, _ = git(["remote", "get-url", "origin"], self.top)
        if rc != 0 or not url:
            self.add("origin", FAIL, "the repo has no origin remote", "git remote add origin <url> && git push -u origin HEAD")
            return
        self.add("origin", OK, re.sub(r"://[^/@\s]+@", "://***@", url))  # never print credentials in a URL
        name, how = default_branch(self.top)
        self.default = name
        if name:
            self.add("default branch", OK, "%s (from %s)" % (name, how))
        else:
            self.add("default branch", FAIL, "origin has no default branch (nothing pushed yet?)",
                     "git push -u origin HEAD && git remote set-head origin --auto")

    def _cfg_cmd(self, key):
        v = (self.cfg_raw or self._peek_config() or {}).get(key)
        return v

    def _peek_config(self):
        if not self.top:
            return None
        try:
            with open(os.path.join(self.top, CONFIG_REL), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def c_gh(self):
        def uses_gh(v):
            if not v:
                return True  # the default command is gh
            first = v[0] if isinstance(v, list) else (v.split() or [""])[0]
            return os.path.basename(first) == "gh"
        needs = [k for k in ("pr_cmd", "merge_cmd", "pr_state_cmd") if uses_gh(self._cfg_cmd(k))]
        if not needs:
            self.add("gh", OK, "not needed: pr_cmd, merge_cmd and pr_state_cmd are set")
            return
        exe = shutil.which("gh", path=self.env.get("PATH"))
        if not exe:
            self.add("gh", FAIL, "the GitHub CLI is not on PATH; %s use%s it" % (
                ", ".join(needs), "s" if len(needs) == 1 else ""),
                "install gh (https://cli.github.com), then: gh auth login"
                " (or set pr_cmd, merge_cmd and pr_state_cmd, docs/adapters.md)")
            return
        rc, out, err = run([exe, "auth", "status"])
        if rc != 0:
            self.add("gh", FAIL, "gh is not logged in (%s)" % ((err or out).strip().splitlines() or ["?"])[0][:120],
                     "gh auth login")
            return
        rc, ver, _ = run([exe, "--version"])
        self.add("gh", OK, "%s, logged in" % ((ver.strip().splitlines() or ["gh"])[0]))

    def c_claude(self):
        exe = claude_exe(self.env)
        self.claude = exe
        if not exe:
            self.add("claude", FAIL, "Claude Code (claude) is not on PATH",
                     "install Claude Code (https://code.claude.com/docs), then: claude auth login")
            return
        rc, out, err = run([exe, "--version"])
        ver = (out.strip() or err.strip()).splitlines()[0][:60] if rc == 0 and (out or err).strip() else "?"
        rc, out, _ = run([exe, "auth", "status", "--json"])
        status = None
        try:
            status = json.loads(out) if out.strip() else None
        except ValueError:
            status = None
        if isinstance(status, dict) and "loggedIn" in status:
            if status["loggedIn"]:
                how = status.get("authMethod") or "logged in"
                self.add("claude", OK, "%s, logged in (%s)" % (ver, how))
            else:
                self.add("claude", FAIL, "%s, not logged in" % ver, "claude auth login")
            return
        from wavecoord.launcher.hostside import claude_auth
        auth = claude_auth(self.env)
        if auth in ("env", "logged-in"):
            self.add("claude", OK, "%s, credentials found (%s)" % (ver, auth))
        elif auth == "keychain?":
            self.add("claude", WARN, "%s; cannot tell whether it is logged in (macOS keychain)" % ver,
                     "claude auth status (and claude auth login if it says so)")
        else:
            self.add("claude", FAIL, "%s, no login found" % ver, "claude auth login")

    def c_plugin(self):
        exe = self.claude
        if not exe:
            return
        entry, err = plugin_entry(exe)
        if entry is None and err:
            where = settings_enable(self.top)
            if where:
                self.add("plugin", OK, "enabled in %s (version unknown: %s)" % (tilde(where), err))
            else:
                self.add("plugin", WARN, "cannot tell whether the plugin is installed (%s)" % err,
                         "claude plugin list")
            return
        if entry is None:
            self.add("plugin", FAIL, "the wavecoord plugin is not installed in Claude Code", INSTALL_PLUGIN)
            return
        pid = entry.get("id") or DEFAULT_PLUGIN_ID
        self.plugin_id = pid
        ver = entry.get("version") or "?"
        if not entry.get("enabled", True):
            self.add("plugin", FAIL, "%s %s is installed but disabled" % (pid, ver), "claude plugin enable %s" % pid)
        elif ver != __version__:
            self.add("plugin", WARN, "%s %s is installed; this CLI is %s" % (pid, ver, __version__),
                     "claude plugin update %s (then restart Claude Code)" % pid)
        else:
            self.add("plugin", OK, "%s %s, enabled (%s scope)" % (pid, ver, entry.get("scope") or "?"))
        opts = plugin_options(exe, pid)
        if opts is None:
            self.add("plugin options", WARN, "cannot read the plugin options", "claude plugin configure %s" % pid)
            return
        done = set(opts.get("configured") or [])
        inputs = opts.get("inputs") or {}
        missing = [k for k in ("coord_url", "token") if k not in done]
        lead = str(inputs.get("lead", "")).lower() == "true"
        fix = "wavecoord init --configure-plugin (or /plugin configure %s in Claude Code)" % pid
        if missing:
            self.add("plugin options", FAIL, "not set: %s" % ", ".join(missing), fix)
            return
        want = self._resolved_url()
        have = str(inputs.get("coord_url") or "").rstrip("/")
        if want and have and have != want:
            self.add("plugin options", WARN, "coord_url is %s but the repo uses %s" % (have, want), fix)
        elif not lead:
            self.add("plugin options", WARN, "coord_url and token are set; lead is off",
                     "set lead to true on the machine you plan from (wavecoord init --configure-plugin),"
                     " or start the lead with: WAVECOORD_ROLE=lead claude")
        else:
            self.add("plugin options", OK, "coord_url=%s, token set, lead=true" % (have or "?"))

    # ----- the repo config -----

    def c_config(self):
        if not self.top:
            return
        path = os.path.join(self.top, CONFIG_REL)
        if not os.path.isfile(path):
            self.add("config", FAIL, "no %s" % CONFIG_REL, "wavecoord init")
            return
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            self.add("config", FAIL, "%s is not valid JSON: %s" % (CONFIG_REL, e), "fix the JSON in %s" % CONFIG_REL)
            return
        from wavecoord.launcher.common import LauncherError
        from wavecoord.launcher.config import load_config
        try:
            load_config(path)
        except LauncherError as e:
            self.add("config", FAIL, str(e).replace(path, CONFIG_REL)[:300],
                     "fix %s (keys: docs/adapters.md, schema: schemas/config.schema.json)" % CONFIG_REL)
            return
        self.cfg_raw = raw
        self.cfg_ok = True
        if not self.default:
            self.add("config", WARN, "%s is valid; origin has no default branch to commit it to" % CONFIG_REL,
                     "git push -u origin HEAD")
            return
        rc, out, _ = git(["show", "refs/remotes/origin/%s:%s" % (self.default, CONFIG_REL)], self.top)
        pending = pending_setup_branch(self.top, raw)
        if rc != 0:
            self.add("config", FAIL, "%s is valid but not on origin/%s; the launcher and the refinery read it there"
                     % (CONFIG_REL, self.default),
                     ("merge the PR of branch %s, then: git pull" % pending) if pending else
                     "wavecoord init --pr (or: git add %s && git commit -m 'wavecoord config' && git push)" % CONFIG_REL)
            return
        try:
            committed = json.loads(out)
        except ValueError:
            committed = None
        if committed != raw:
            self.add("config", WARN, "%s differs from origin/%s (as of the last fetch); the launcher and the"
                     " refinery read origin's" % (CONFIG_REL, self.default),
                     ("merge the PR of branch %s, then: git pull" % pending) if pending else
                     "commit and merge it: wavecoord init --pr")
        else:
            self.add("config", OK, "%s, valid, committed on origin/%s (project %s)"
                     % (CONFIG_REL, self.default, raw.get("project")))

    def _resolved_url(self):
        from wavecoord.cli import resolve_coord_url
        return resolve_coord_url(env=self.env, cwd=self.top or self.cwd)

    def c_coord_url(self):
        url = self._resolved_url()
        if not url:
            self.add("coord_url", FAIL, "no coordinator URL is configured",
                     "wavecoord init (adds coord_url to %s), or: export WAVECOORD_URL=http://127.0.0.1:%d"
                     % (CONFIG_REL, const.DEFAULT_PORT))
            return
        src = "WAVECOORD_URL" if self.env.get("WAVECOORD_URL", "").strip() else (
            "the plugin option" if self.env.get("CLAUDE_PLUGIN_OPTION_COORD_URL", "").strip() else CONFIG_REL)
        try:
            parts = urllib.parse.urlsplit(url)
            host, port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            host, port, parts = None, None, None
        if not parts or parts.scheme not in ("http", "https") or not host:
            self.add("coord_url", FAIL, "%s (from %s) is not an http(s) URL" % (url, src),
                     "set coord_url to http://127.0.0.1:%d" % const.DEFAULT_PORT)
            return
        try:
            socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        except (socket.gaierror, OSError) as e:
            self.add("coord_url", FAIL, "%s (from %s): cannot resolve %s (%s)" % (url, src, host, e),
                     "fix the host name in coord_url")
            return
        self.url = url
        from wavecoord.client import url_trusted
        if not url_trusted(url, self.env):
            self.add("coord_url", WARN, "%s (from %s): a non-loopback URL that only the repo names gets no token"
                     % (url, src), "export WAVECOORD_URL=%s (and set the plugin option coord_url)" % url)
        else:
            self.add("coord_url", OK, "%s (from %s)" % (url, src))

    # ----- the coordinator -----

    def c_coordinator(self):
        if not self.url:
            return
        st, body, err = http_get(self.url + "/api/health")
        if st != 200 or not isinstance(body, dict):
            fix = "wavecoord install-server (or run: wavecoord serve)"
            unit = self._unit()
            if unit and os.path.isfile(unit[0]):
                fix = "the service is installed; read its log: %s" % tilde(unit[1])
            self.add("coordinator", FAIL, "no coordinator answers at %s (%s)" % (self.url, err or st), fix)
            return
        self.coord_up = True
        ver = str(body.get("version") or "")
        ver = ver[len("wavecoord "):] if ver.startswith("wavecoord ") else ver
        if ver != __version__:
            self.add("coordinator", WARN, "%s answers, version %s; this CLI is %s" % (self.url, ver, __version__),
                     "wavecoord install-server (moves the service to this build)")
        elif body.get("ok") is False:
            self.add("coordinator", WARN, "%s answers but reports ok: false (a failed push or a forked log)" % self.url,
                     "read the server log and docs/rollback.md")
        else:
            self.add("coordinator", OK, "%s, version %s, api %s" % (self.url, ver, body.get("api")))
        from wavecoord.client import url_trusted
        from wavecoord.corecli import client_token
        if not url_trusted(self.url, self.env):
            self.add("token", WARN, "not checked: the token is never sent to %s (only the repo names it)" % self.url,
                     "export WAVECOORD_URL=%s" % self.url)
            return
        tok = client_token(self.env)
        if not tok:
            self.add("token", FAIL, "no token (plugin option, WAVECOORD_TOKEN, WAVECOORD_TOKEN_FILE or %s)"
                     % tilde(lead_token_path(self.env)),
                     "wavecoord tokens provision --role lead --out ~/.wavecoord/token")
            return
        st, body, err = http_get(self.url + "/api/config", token=tok)
        if st == 200 and isinstance(body, dict):
            self.server_cfg = body.get("config")
            self.add("token", OK, "the coordinator accepts the token")
        elif st in (401, 403):
            self.add("token", FAIL, "the coordinator refuses the token (%s)" % st,
                     "wavecoord tokens provision --role lead --out ~/.wavecoord/token, then set the plugin option"
                     " token (wavecoord init --configure-plugin)")
        else:
            self.add("token", WARN, "cannot check the token (%s)" % (err or st), "wavecoord status")

    # ----- tokens -----

    def _token_check(self, name, path, role, principal, missing_status, missing_fix, missing_note=""):
        tok = read_secret(path)
        if tok is None:
            self.add(name, missing_status, "%s is missing%s" % (tilde(path), missing_note), missing_fix)
            return
        mode = os.stat(path).st_mode & 0o777
        if mode & 0o077:
            self.add(name, FAIL, "%s has mode %o; others can read it" % (tilde(path), mode), "chmod 600 %s" % tilde(path))
            return
        if not tok:
            self.add(name, FAIL, "%s is empty" % tilde(path), missing_fix)
            return
        from wavecoord.corecli import default_tokens_file
        tf = default_tokens_file(self.env)
        known = read_tokens_file(tf)
        if known is None:
            self.add(name, WARN, "%s (mode 600); no tokens file at %s to check it against (a server on another"
                     " machine?)" % (tilde(path), tilde(tf)), None)
            return
        rec = known.get(token_digest(tok))
        if rec is None:
            self.add(name, FAIL, "%s is not in %s (made for another server?)" % (tilde(path), tilde(tf)), missing_fix)
        elif rec.get("role") != role or (principal and rec.get("principal") != principal):
            self.add(name, FAIL, "%s is a %s token for %s, not a %s token%s" % (
                tilde(path), rec.get("role"), rec.get("principal"), role, " for " + principal if principal else ""),
                missing_fix)
        else:
            self.add(name, OK, "%s, mode 600, %s %s token" % (tilde(path), "an" if role[0] in "aeiou" else "a", role))

    def c_lead_token(self):
        path = lead_token_path(self.env)
        self._token_check("lead token", path, "lead", None, FAIL,
                          "wavecoord tokens provision --role lead --out %s (or: wavecoord init)" % tilde(path))

    def _hosts(self):
        if self.cfg_ok:
            from wavecoord.launcher.config import DEFAULT_HOSTS
            return self.cfg_raw.get("hosts") or DEFAULT_HOSTS
        return []

    def c_host_tokens(self):
        for h in self._hosts():
            n = h["name"]
            path = host_token_path(n)
            fix = "wavecoord tokens provision --role agent --host %s --out %s" % (n, tilde(path))
            if h.get("ssh"):
                self._token_check("host token %s" % n, path, "agent", "host:" + n, FAIL, fix)
            else:
                self._token_check("host token %s" % n, path, "agent", "host:" + n, WARN, fix + " (or: wavecoord init)",
                                  "; the launcher makes one at the first launch")

    def c_hosts(self):
        if not self.cfg_ok:
            return
        if self.quick:
            self.add("hosts", WARN, "not probed (--quick)", "wavecoord doctor (without --quick), or: wavecoord hosts")
            return
        from wavecoord.launcher.client import Launcher, attempt_auth, why_ineligible
        from wavecoord.launcher.common import LauncherError
        from wavecoord.launcher.config import load_config
        try:
            cfg = load_config(os.path.join(self.top, CONFIG_REL))
            L = Launcher(cfg, coord_url=self.url)
            probes = L.probe()
        except LauncherError as e:
            self.add("hosts", FAIL, str(e)[:300], "wavecoord hosts")
            return
        prov = cfg.default_provider
        for n in sorted(probes):
            p = probes[n] or {}
            h = cfg.hosts.get(n) or {}
            if not p.get("ok"):
                fix = ("check that this works: ssh %s true" % h["ssh"]) if h.get("ssh") else "wavecoord hosts --host %s" % n
                self.add("host %s" % n, FAIL, "probe failed: %s" % str(p.get("error") or "?")[:200], fix)
                continue
            why = why_ineligible(p, prov) if prov in cfg.host_providers(n) else "provider %s is not on this host" % prov
            c = p.get("claude") or {}
            text = "%s/%s slots, claude %s, auth %s" % (p.get("active", "?"), p.get("slots", "?"),
                                                       (c.get("version") or "?").split(" ")[0],
                                                       attempt_auth(p, prov)[0] or c.get("auth") or "?")
            if why and not why.startswith("full"):
                fix = "on %s: claude auth login" % n if "logged in" in why else "wavecoord hosts --host %s" % n
                self.add("host %s" % n, FAIL, "%s: %s" % (text, why), fix)
            else:
                self.add("host %s" % n, OK, text + (" (all slots busy)" if why else ""))

    # ----- the server service -----

    def _unit(self):
        """-> (unit path, log path) of the service for this repo's project, or None."""
        from wavecoord import installsrv
        if sys.platform not in ("darwin",) and not sys.platform.startswith("linux"):
            return None
        project = (self.cfg_raw or self._peek_config() or {}).get("project") or os.path.basename(self.top or self.cwd)
        plat = "darwin" if sys.platform == "darwin" else "linux"
        return installsrv.unit_location(project, plat), os.path.join(const.home_dir(project, env={}), "serve.log")

    def c_service(self):
        if not self.top:
            return
        unit = self._unit()
        if unit is None:
            self.add("service", WARN, "no user service on %s; run the server yourself" % sys.platform,
                     "wavecoord serve")
            return
        path = unit[0]
        if not os.path.isfile(path):
            self.add("service", WARN, "no server service installed (%s)" % tilde(path),
                     "wavecoord install-server (or: wavecoord init --server)")
            return
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            self.add("service", WARN, "cannot read %s: %s" % (tilde(path), e), None)
            return
        m = re.search(r"wavecoord-([0-9a-f]{16})\b", text)
        from wavecoord import zipapp
        mine = zipapp.self_hash()
        if m and m.group(1) != mine:
            self.add("service", WARN, "%s runs build %s; this CLI is build %s" % (tilde(path), m.group(1), mine),
                     "wavecoord install-server (moves the pin)")
        else:
            self.add("service", OK, "%s (build %s)" % (tilde(path), m.group(1) if m else "?"))

    # ----- the plan store -----

    def c_plan(self):
        if not self.cfg_ok:
            return
        from wavecoord.specs.source import norm_dir
        spec_dir = norm_dir((self.cfg_raw.get("spec") or {}).get("dir"))
        named = (self.cfg_raw.get("plan") or {}).get("adapter")
        records = self._spec_records(spec_dir)
        if self.server_cfg and isinstance(self.server_cfg.get("plan"), dict):
            plan = self.server_cfg["plan"]
            adapter, why = plan.get("adapter"), plan.get("selected_by") or "?"
            src = "the coordinator"
        else:
            adapter, why = self._local_store(named, spec_dir, records)
            src = "the local origin/%s" % (self.default or "?")
        label = {"specs": "spec tree %s/" % spec_dir, "builtin": "flat tasks %s" % (
            (self.cfg_raw.get("plan") or {}).get("dir") or "plan/tasks"), "commands": "command adapter"}.get(
            adapter, adapter)
        if adapter == "unavailable":
            self.add("plan", FAIL, "%s (from %s)" % (why, src), "git fetch origin; or set plan.adapter")
        elif named == "builtin" and records:
            self.add("plan", WARN, "plan.adapter builtin pins %s, but %s/ has spec records: they are never dispatched"
                     % (label, spec_dir), 'remove "plan" from %s in a config PR' % CONFIG_REL)
        elif adapter == "builtin" and not records:
            self.add("plan", OK, "%s: %s (from %s); no spec tree yet: start one with /wavecoord:spec" % (label, why, src))
        else:
            self.add("plan", OK, "%s: %s (from %s)" % (label, why, src))

    def _spec_records(self, spec_dir):
        if not self.default:
            return False
        from wavecoord.specs import source
        from wavecoord.specs.tree import Tree
        try:
            files = source.read_ref(self.top, "refs/remotes/origin/%s" % self.default, spec_dir)
        except Exception:  # noqa: BLE001 - unreadable: reported by the store check
            return None
        return any(n.rec is not None for n in Tree(files).nodes.values()) if files else False

    def _local_store(self, named, spec_dir, records):
        """plan_store of lead/base.py, on the local origin ref (no fetch)."""
        if named in ("commands", "specs", "builtin"):
            return named, "plan.adapter"
        if not self.default:
            return "builtin", "no default branch of origin: no spec tree"
        if records is None:
            return "unavailable", "cannot read %s/ on origin/%s" % (spec_dir, self.default)
        if records:
            return "specs", "%s/ has spec records on the default branch" % spec_dir
        return "builtin", "no spec records in %s/ on the default branch" % spec_dir


def run_doctor(cwd=None, env=None, quick=False):
    return Doctor(cwd=cwd, env=env, quick=quick).run()


def summary(checks):
    n = {s: sum(1 for c in checks if c["status"] == s) for s in (OK, WARN, FAIL)}
    return n


def render(checks, color=False, out=None):
    out = out or sys.stdout
    width = max([len(c["name"]) for c in checks] + [8])
    for c in checks:
        st = c["status"]
        word = "%-4s" % st
        if color:
            word = COLORS[st] + word + RESET
        out.write("%s  %s  %s\n" % (word, c["name"].ljust(width), c["detail"]))
        if c.get("fix") and st != OK:
            out.write("%s  %s  fix: %s\n" % (" " * 4, " " * width, c["fix"]))
    n = summary(checks)
    line = "%d failed, %d warnings, %d ok" % (n[FAIL], n[WARN], n[OK])
    if color:
        line = (COLORS[FAIL] if n[FAIL] else COLORS[WARN] if n[WARN] else COLORS[OK]) + line + RESET
    out.write(line + "\n")


def use_color(mode, stream=None):
    stream = stream or sys.stdout
    if mode == "always":
        return True
    if mode == "never" or os.environ.get("NO_COLOR"):
        return False
    return hasattr(stream, "isatty") and stream.isatty() and os.environ.get("TERM") != "dumb"


def cmd_doctor(args):
    checks = run_doctor(quick=args.quick)
    failed = any(c["status"] == FAIL for c in checks)
    if args.json:
        print(json.dumps({"version": __version__, "ok": not failed, "counts": summary(checks), "checks": checks},
                         indent=1, sort_keys=True))
    else:
        print("wavecoord doctor %s" % __version__)
        render(checks, color=use_color(args.color))
    return 1 if failed else 0


def register(sub):
    sp = sub.add_parser("doctor", help="check every prerequisite of the setup; print the fix for each problem")
    sp.add_argument("--json", action="store_true", help="print the checks as JSON")
    sp.add_argument("--quick", action="store_true", help="do not probe the hosts")
    sp.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    sp.set_defaults(func=cmd_doctor)
