"""`wavecoord init`: set up wavecoord in the current repo, idempotently.

It writes `.claude/wavecoord.json` (or adds missing keys to an existing one, never
changing a key that is there), makes the lead token and the local hosts' agent tokens
when their files are missing (never over an existing file), and, on request, installs the
server service (`--server`), creates the spec folder (`--specs`), sets the plugin options in
Claude Code (`--configure-plugin`) and opens a config PR (`--pr`). Then it prints what is
left to do and runs `wavecoord doctor`. `--dry-run` prints the same plan and writes nothing.
"""

import ipaddress
import json
import os
import re
import shlex
import sys
import tempfile
import urllib.parse

from wavecoord import __version__, const, doctor

CONFIG_REL = doctor.CONFIG_REL
SETUP_BRANCH = "wavecoord/setup"
DEFAULT_SLOTS = 2
# git merge: a resolver (a --continue run after a conflict) merges the default branch into its branch
WORKER_TOOLS = "Bash(git add *),Bash(git commit *),Bash(git status *),Bash(git diff *),Bash(git log *),Bash(git merge *)"
SPEC_README = """# Specs

This folder holds the wavecoord spec tree: capabilities, features, requirements and tasks as
Markdown records (see the wavecoord docs, `docs/specs.md`). This README is not a record, so
the lead keeps planning from `plan/tasks` until the first spec record is merged here.

Start one in a Claude Code session in this repo:

    /wavecoord:spec <the capability or feature you want>
"""


class InitError(Exception):
    pass


def project_name(top, existing=None):
    """The project: the config's, else the origin repo's name, else the folder name; as the schema allows."""
    if existing:
        return existing
    rc, url, _ = doctor.git(["remote", "get-url", "origin"], top)
    base = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1] if rc == 0 and url else os.path.basename(top)
    if base.endswith(".git"):
        base = base[:-4]
    name = re.sub(r"[^a-z0-9._-]+", "-", base.lower()).strip("-._")[:64]
    return name if name and re.match(r"^[a-z0-9]", name) else "project"


def defaults(project, coord_url, slots, fence, check_cmd):
    cfg = {
        "coord_url": coord_url,
        "project": project,
        "hosts": [{"name": "local", "slots": slots, "repo_path": ".",
                   "claude_flags": ["--permission-mode", "acceptEdits", "--allowedTools", WORKER_TOOLS]}],
        "wip_cap": slots,
        "fence": fence,
    }
    if check_cmd:
        cfg["check_cmd"] = check_cmd
    return cfg


def merge(existing, wanted):
    """-> (merged, added keys, kept [(key, existing value, wanted value)]). Existing keys always win."""
    out = dict(existing)
    added, kept = [], []
    for k, v in wanted.items():
        if k not in out:
            out[k] = v
            added.append(k)
        elif out[k] != v:
            kept.append((k, out[k], v))
    return out, added, kept


def serve_address(coord_url):
    """-> (port or None, note or None): what the server service must listen on so that `coord_url` reaches it.

    The port of the URL goes to `serve --port` when it is not the default. The service binds 127.0.0.1;
    a URL with another host gets a note instead of a wider bind (security.md)."""
    try:
        parts = urllib.parse.urlsplit(coord_url or "")
        host, port = parts.hostname, parts.port
    except ValueError:
        return None, "coord_url %s is not a URL; the service listens on 127.0.0.1:%d" % (coord_url, const.DEFAULT_PORT)
    if parts.scheme != "http":
        # https: a proxy in front of the plain-http server (security.md); its port is not the server's
        return None, ("coord_url %s is not plain http; the service listens on http://127.0.0.1:%d, behind your"
                      " proxy" % (coord_url, const.DEFAULT_PORT))
    note = None
    loopback = host in ("localhost",)
    try:
        loopback = loopback or ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        pass
    if not loopback:
        note = ("coord_url names %s; the service listens on 127.0.0.1 only. To serve other machines, read"
                " docs/security.md, then: wavecoord install-server --bind <address>%s"
                % (host, " --port %d" % port if port else ""))
    return (port if port and port != const.DEFAULT_PORT else None), note


def dump(cfg):
    return json.dumps(cfg, indent=2) + "\n"


class Init(object):
    def __init__(self, args, out=None):
        self.a = args
        self.out = out or sys.stdout
        self.dry = args.dry_run
        self.created = []  # repo files this run wrote (for --pr)

    def say(self, text):
        self.out.write(text + "\n")

    def act(self, text):
        self.say(("would " if self.dry else "") + text)

    # ----- interactive -----

    def ask(self, question, default):
        shown = default if not isinstance(default, bool) else ("Y/n" if default else "y/N")
        try:
            ans = input("%s [%s]: " % (question, shown)).strip()
        except EOFError:
            ans = ""
        if isinstance(default, bool):
            return default if not ans else ans.lower().startswith("y")
        return ans or default

    def interactive(self, project, existing):
        if not sys.stdin.isatty():
            raise InitError("--interactive needs a terminal; pass the flags instead")
        a = self.a
        if "project" not in existing:
            a.project = self.ask("Project name", a.project or project)
        if "coord_url" not in existing:
            a.coord_url = self.ask("Coordinator URL", a.coord_url)
        if "hosts" not in existing:
            ans = self.ask("Worker slots on this machine", str(a.slots))
            if not ans.isdigit() or int(ans) < 1:
                raise InitError("slots must be a number, 1 or more (not %r)" % ans)
            a.slots = int(ans)
        if "fence" not in existing:
            a.fence = self.ask("Edit fence (off, advisory, enforce)", a.fence)
        if "check_cmd" not in existing:
            a.check_cmd = self.ask("Test command the refinery runs before a merge (empty: none)", a.check_cmd or "") \
                or None
        a.server = self.ask("Install the coordinator as a user service", a.server)
        a.specs = self.ask("Create the spec folder with a README", a.specs)
        a.configure_plugin = self.ask("Set the plugin options in Claude Code now", a.configure_plugin)
        a.pr = self.ask("Commit the config on a branch and open a PR", a.pr)

    # ----- steps -----

    def run(self):
        a = self.a
        top = doctor.repo_top(os.getcwd())
        if not top:
            raise InitError("not inside a git repository: cd into your project's repo first")
        self.top = top
        path = os.path.join(top, CONFIG_REL)
        existing = {}
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    existing = json.load(f)
            except ValueError as e:
                raise InitError("%s is not valid JSON (%s); fix it first, init never overwrites it" % (CONFIG_REL, e))
            if not isinstance(existing, dict):
                raise InitError("%s is not a JSON object; fix it first" % CONFIG_REL)
        project = project_name(top, existing.get("project"))
        if a.interactive:
            self.interactive(project, existing)
        if a.project and not re.match(r"^[a-z0-9][a-z0-9._-]{0,63}$", a.project):
            raise InitError("--project must be lowercase letters, digits, '.', '_' or '-' (at most 64)")
        default, _ = doctor.default_branch(top)
        self.default = default
        self.say("wavecoord init %s in %s" % (__version__, top))
        self.say("project %s, default branch %s" % (a.project or project,
                                                    default or "(none on origin yet: git push -u origin HEAD)"))
        cfg = self.step_config(path, existing, a.project or project)
        self.step_tokens(cfg)
        if a.server:
            self.step_server(cfg)
        if a.specs:
            self.step_specs(cfg)
        self.step_plugin(cfg)
        self.step_commit(cfg)
        self.next_steps(cfg)
        return 0

    def step_config(self, path, existing, project):
        a = self.a
        wanted = defaults(project, a.coord_url, a.slots, a.fence, a.check_cmd)
        if "hosts" in existing:
            wanted.pop("wip_cap")  # its default is the sum of the slots of the hosts there
        merged, added, kept = merge(existing, wanted)
        from wavecoord import jsonschema_lite
        from wavecoord.launcher.config import config_schema
        errs = jsonschema_lite.validate(merged, config_schema())
        if errs:
            raise InitError("the config would be invalid: %s" % "; ".join(errs[:5]))
        rel = CONFIG_REL
        if not existing:
            self.act("write %s (%s)" % (rel, ", ".join(added)))
        elif added:
            self.act("add %s to %s (the keys there stay as they are)" % (", ".join(added), rel))
        else:
            self.say("%s: up to date" % rel)
        for k, old, new in kept:
            if k in ("project", "coord_url", "fence", "wip_cap", "check_cmd") and self._flag_given(k):
                self.say("  kept %s = %s (you asked for %s; edit the file to change it)"
                         % (k, json.dumps(old), json.dumps(new)))
        if added and not self.dry:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(dump(merged))
            os.replace(tmp, path)
        if added:
            self.created.append(rel)
        return merged

    def _flag_given(self, key):
        return key in getattr(self.a, "given", ())

    def step_tokens(self, cfg):
        from wavecoord.corecli import default_tokens_file, provision_token, write_secret
        tokens = default_tokens_file()
        wanted = [("lead", None, doctor.lead_token_path())]
        for h in cfg.get("hosts") or []:
            if not h.get("ssh"):
                wanted.append(("agent", h["name"], doctor.host_token_path(h["name"])))
        for role, host, path in wanted:
            what = "the lead token" if role == "lead" else "the agent token of host %s" % host
            if os.path.lexists(path):
                self.say("%s: kept %s" % (what, doctor.tilde(path)))
                continue
            self.act("make %s: %s (mode 600)" % (what, doctor.tilde(path)))
            if self.dry:
                continue
            d = os.path.dirname(os.path.abspath(path))
            os.makedirs(d, mode=0o700, exist_ok=True)
            tok = provision_token(tokens, role, "host:" + host if host else role)
            write_secret(path, tok + "\n")
        remote = [h["name"] for h in cfg.get("hosts") or [] if h.get("ssh")]
        for n in remote:
            p = doctor.host_token_path(n)
            if not os.path.lexists(p):
                self.say("ssh host %s has no token yet: wavecoord tokens provision --role agent --host %s --out %s"
                         % (n, n, doctor.tilde(p)))

    def step_server(self, cfg):
        from wavecoord import installsrv
        port, note = serve_address(cfg.get("coord_url"))
        if note:
            self.say("server: " + note)
        try:
            p = installsrv.plan(self.top, port=port, extra_env=installsrv.carry_env(os.environ))
        except ValueError as e:
            self.say("server: %s; run `wavecoord serve` yourself" % e)
            return
        current = None
        try:
            with open(p["unit_path"], encoding="utf-8") as f:
                current = f.read()
        except OSError:
            pass
        if current == p["unit"] and os.path.isfile(p["zipapp"]):
            self.say("server service: up to date (%s)" % doctor.tilde(p["unit_path"]))
            return
        self.act("install the server service %s (wavecoord install-server%s)"
                 % (doctor.tilde(p["unit_path"]), " --port %d" % port if port else ""))
        if self.dry:
            return
        try:
            installsrv.install(p)
        except (OSError, RuntimeError) as e:
            raise InitError("install-server failed: %s" % e)
        self.say("  started %s; log %s" % (p["label"], doctor.tilde(p["log"])))

    def step_specs(self, cfg):
        from wavecoord.specs.source import norm_dir
        d = norm_dir((cfg.get("spec") or {}).get("dir"))
        readme = os.path.join(self.top, d, "README.md")
        if os.path.isdir(os.path.join(self.top, d)):
            self.say("%s/: exists" % d)
            return
        self.act("create %s/README.md" % d)
        self.created.append("%s/README.md" % d)
        if self.dry:
            return
        os.makedirs(os.path.dirname(readme), exist_ok=True)
        with open(readme, "w", encoding="utf-8", newline="\n") as f:
            f.write(SPEC_README)

    def plugin_id(self):
        exe = doctor.claude_exe()
        return exe, doctor.plugin_id_for(exe, explicit=getattr(self.a, "plugin_id", None))

    def step_plugin(self, cfg):
        a = self.a
        url = os.environ.get("WAVECOORD_URL", "").strip().rstrip("/") or cfg.get("coord_url") or \
            "http://127.0.0.1:%d" % const.DEFAULT_PORT
        tok_path = doctor.lead_token_path()
        exe, pid = self.plugin_id()
        self.say("")
        self.say("Plugin options for %s (Claude Code: /plugin configure %s):" % (pid, pid))
        self.say("  coord_url  %s" % url)
        self.say("  lead       true")
        if a.show_token and not self.dry:
            self.say("  token      %s" % (doctor.read_secret(tok_path) or "(no token file)"))
        else:
            self.say("  token      the contents of %s (--show-token prints it)" % doctor.tilde(tok_path))
        cmd = ("printf '{\"coord_url\": \"%%s\", \"lead\": \"true\", \"token\": \"%%s\"}' %s \"$(cat %s)\""
               " | claude plugin configure %s --values-stdin" % (shlex.quote(url), doctor.tilde(tok_path), pid))
        if not a.configure_plugin:
            self.say("  or in a terminal: %s" % cmd)
            self.say("  (or run: wavecoord init --configure-plugin)")
            return
        if not exe:
            self.say("plugin options: claude is not on PATH; set them in Claude Code with /plugin configure %s" % pid)
            return
        self.act("set the plugin options of %s (claude plugin configure %s --values-stdin)" % (pid, pid))
        if self.dry:
            return
        tok = doctor.read_secret(tok_path)
        if not tok:
            self.say("  skipped: no lead token at %s" % doctor.tilde(tok_path))
            return
        values = json.dumps({"coord_url": url, "lead": "true", "token": tok})
        rc, out, err = doctor.run([exe, "plugin", "configure", pid, "--values-stdin"], stdin_text=values)
        if rc != 0:
            msg = (err or out).replace(tok, "<token>").strip()[-300:]
            self.say("  failed (%s): %s; set them with /plugin configure %s" % (rc, msg, pid))
        else:
            self.say("  done; restart Claude Code sessions to pick them up")

    def step_commit(self, cfg):
        a = self.a
        rc, out, _ = doctor.git(["status", "--porcelain", "--", CONFIG_REL], self.top)
        dirty = bool(out.strip())
        on_default = False
        if self.default:
            rc, text, _ = doctor.git(["show", "refs/remotes/origin/%s:%s" % (self.default, CONFIG_REL)], self.top)
            try:
                on_default = rc == 0 and json.loads(text) == cfg
            except ValueError:
                on_default = False
        self.say("")
        if on_default and not self.created:
            self.say("%s is committed on origin/%s" % (CONFIG_REL, self.default))
            return
        files = [CONFIG_REL] + [f for f in self.created if f != CONFIG_REL]
        if not a.pr:
            self.say("Commit the config to the default branch (the launcher and the refinery read it there):")
            self.say("  git add %s && git commit -m 'wavecoord config' && git push" % " ".join(files))
            self.say("  (or open a PR for it: wavecoord init --pr)")
            return
        if not self.default:
            self.say("--pr: origin has no default branch yet; push one first: git push -u origin HEAD")
            return
        pending = doctor.pending_setup_branch(self.top, cfg)
        if pending and files == [CONFIG_REL]:
            self.say("branch %s on origin has this config already: merge its PR, then: git pull" % pending)
            return
        branch = self.free_branch()
        self.act("commit %s on %s and open a PR (pr_cmd)" % (", ".join(files), branch))
        if self.dry:
            return
        self.open_pr(branch, files, dirty)

    def free_branch(self):
        rc, out, _ = doctor.git(["ls-remote", "--heads", "origin", "refs/heads/%s*" % SETUP_BRANCH], self.top, timeout=60)
        taken = set(l.split("refs/heads/", 1)[-1] for l in out.splitlines()) if rc == 0 else set()
        if SETUP_BRANCH not in taken:
            return SETUP_BRANCH
        n = 2
        while "%s-%d" % (SETUP_BRANCH, n) in taken:
            n += 1
        return "%s-%d" % (SETUP_BRANCH, n)

    def open_pr(self, branch, files, dirty):
        from wavecoord.launcher.client import run_pr_cmd
        from wavecoord.launcher.config import load_config
        from wavecoord.plan.edit import EditError, commit_files
        rc, base, err = doctor.git(["rev-parse", "refs/remotes/origin/%s" % self.default], self.top)
        if rc != 0:
            raise InitError("cannot read origin/%s: %s" % (self.default, err))
        contents = {}
        for rel in files:
            with open(os.path.join(self.top, rel), encoding="utf-8") as f:
                contents[rel] = f.read()
        body = ("Set up wavecoord: %s.\n\nMade by `wavecoord init --pr`. Merge it so the launcher and the refinery"
                " read the config from the default branch." % ", ".join("`%s`" % f for f in files))
        try:
            commit_files(self.top, base, branch, contents, "wavecoord: set up %s" % CONFIG_REL, body)
        except EditError as e:
            raise InitError(str(e))
        self.say("  pushed %s" % branch)
        # stage the same content here: an untracked copy would make `git pull` refuse to overwrite it after the
        # merge; a staged copy that matches the merged file lets the pull fast-forward over it
        rc, _, err = doctor.git(["add", "--"] + list(files), self.top)
        if rc != 0:
            self.say("  could not stage %s (%s); after the merge, remove the local copy before git pull"
                     % (", ".join(files), err.strip()[-200:]))
        cfg = load_config(os.path.join(self.top, CONFIG_REL))
        fd, body_file = tempfile.mkstemp(prefix="wavecoord-init-", suffix=".md")
        os.close(fd)
        try:
            url, err = run_pr_cmd(cfg, branch, self.default, "wavecoord: set up the config", body, body_file)
        finally:
            os.remove(body_file)
        if err:
            self.say("  the PR did not open (%s); open it for branch %s by hand" % (err, branch))
        else:
            self.say("  PR: %s (merge it, then: git pull)" % url)

    def next_steps(self, cfg):
        self.say("")
        self.say("Next:")
        steps = []
        if not self.a.server:
            steps.append("start the coordinator: wavecoord init --server (or: wavecoord install-server)")
        if not self.a.configure_plugin:
            steps.append("set the plugin options above, then restart Claude Code")
        steps.append("in Claude Code in this repo: /wavecoord:setup checks everything again")
        steps.append("then /wavecoord:spec <feature> for a new spec tree"
                     " (or /wavecoord:spec-import to import existing spec files)")
        for i, s in enumerate(steps, 1):
            self.say("  %d. %s" % (i, s))


def fill_defaults(args):
    """Note which settings the user passed, then fill in the defaults."""
    args.given = set(k for k, a in (("project", "project"), ("coord_url", "coord_url"), ("wip_cap", "slots"),
                                    ("fence", "fence"), ("check_cmd", "check_cmd")) if getattr(args, a) is not None)
    if args.coord_url is None:
        args.coord_url = "http://127.0.0.1:%d" % const.DEFAULT_PORT
    if args.slots is None:
        args.slots = DEFAULT_SLOTS
    if args.slots < 1:
        raise InitError("--slots must be 1 or more")
    if args.fence is None:
        args.fence = "advisory"


def cmd_init(args):
    try:
        fill_defaults(args)
        rc = Init(args).run()
    except InitError as e:
        sys.stderr.write("wavecoord init: %s\n" % e)
        return 1
    if args.dry_run:
        print("\n(dry run: nothing was written; run without --dry-run to apply)")
        return rc
    if not args.no_doctor:
        print("\nwavecoord doctor:")
        sys.stdout.flush()
        checks = doctor.run_doctor(quick=True, plugin_id=args.plugin_id)
        doctor.render(checks, color=doctor.use_color("auto"))
    return rc


def register(sub):
    sp = sub.add_parser("init", help="set up wavecoord in this repo: config, tokens, and optionally the server,"
                                     " the spec folder, the plugin options and a config PR")
    sp.add_argument("--project", help="project name (default: the origin repo's name)")
    sp.add_argument("--coord-url", help="coordinator URL (default http://127.0.0.1:%d)" % const.DEFAULT_PORT)
    sp.add_argument("--slots", type=int, help="worker slots of the local host and wip_cap (default %d)" % DEFAULT_SLOTS)
    sp.add_argument("--fence", choices=("off", "advisory", "enforce"), help="edit fence mode (default advisory)")
    sp.add_argument("--check-cmd", help="command the refinery runs before each merge, for example your tests")
    sp.add_argument("--server", action="store_true", help="install the coordinator as a user service")
    sp.add_argument("--specs", action="store_true", help="create the spec folder with a README")
    sp.add_argument("--configure-plugin", action="store_true",
                    help="set the plugin options coord_url, lead and token with `claude plugin configure`")
    sp.add_argument("--plugin-id", help="the plugin id that --configure-plugin sets, for example %s for a claude"
                                        " --plugin-dir session (default: the id claude plugin list shows, else %s)"
                                        % (doctor.INLINE_PLUGIN_ID, doctor.DEFAULT_PLUGIN_ID))
    sp.add_argument("--pr", action="store_true", help="commit the config on a branch and open a PR (pr_cmd)")
    sp.add_argument("--show-token", action="store_true", help="print the lead token with the plugin options")
    sp.add_argument("--interactive", action="store_true", help="ask the questions on a terminal")
    sp.add_argument("--dry-run", action="store_true", help="print what would happen; write nothing")
    sp.add_argument("--no-doctor", action="store_true", help="do not run doctor at the end")
    sp.set_defaults(func=cmd_init)
