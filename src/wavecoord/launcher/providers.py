"""Provider profiles: run workers against an Anthropic-compatible provider (docs/providers.md).

A profile is a name in `providers` of `.claude/wavecoord.json`. Its secrets live
in `env_file` ON THE HOST: a KEY=VALUE file (shell-quoted values), owned by the
host user, mode 600. The launcher never uploads, prints or logs that file. The
wrapper loads it into the environment of the `claude` child only.

`anthropic` is built in: the host's default environment, no file.
"""

import os
import re
import shlex
import stat

ANTHROPIC = "anthropic"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MODEL_ENV = {
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "fable": "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "subagent": "CLAUDE_CODE_SUBAGENT_MODEL",
}
# Parent variables that would send a worker to another account or provider. A
# non-anthropic profile removes them before it loads its own file.
STRIP_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")
STRIP_KEYS = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_SUBAGENT_MODEL", "API_TIMEOUT_MS", "AWS_BEARER_TOKEN_BEDROCK")


class ProviderError(Exception):
    pass


def is_builtin(name, profile):
    return name == ANTHROPIC and not profile


def check_env_file(path):
    """-> None when the file is usable, else why not. Uses stat only; never reads the file."""
    p = os.path.expanduser(path)
    try:
        st = os.stat(p)
    except FileNotFoundError:
        return "env file %s is missing" % path
    except OSError as e:
        return "env file %s: %s" % (path, e.strerror or e)
    if not stat.S_ISREG(st.st_mode):
        return "env file %s is not a regular file" % path
    if st.st_mode & 0o077:
        return "env file %s has mode %04o; it must be 600 (chmod 600 %s)" % (path, st.st_mode & 0o777, path)
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        return "env file %s is not owned by this user" % path
    return None


def parse_env_file(path):
    """KEY=VALUE lines -> dict. `#` comments, blank lines and `export ` are allowed.

    Values are shell-quoted words (`KEY='a b'`, `KEY=""`). An error names the line
    number, never the value."""
    out = {}
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, value = line.partition("=")
            key = key.strip()
            if not sep or not KEY_RE.match(key):
                raise ProviderError("%s line %d: expected KEY=VALUE" % (path, n))
            try:
                words = shlex.split(value, comments=False, posix=True)
            except ValueError:
                raise ProviderError("%s line %d (%s): bad quoting" % (path, n, key))
            if len(words) > 1:
                raise ProviderError("%s line %d (%s): quote a value that has spaces" % (path, n, key))
            out[key] = words[0] if words else ""
    return out


def profile_env(name, profile):
    """The variables a profile adds to the claude child env. Raises ProviderError."""
    if is_builtin(name, profile):
        return {}
    why = check_env_file(profile["env_file"])
    if why:
        raise ProviderError("provider %s: %s" % (name, why))
    env = {"ANTHROPIC_API_KEY": ""}  # never fall back to a key of the parent
    env.update(parse_env_file(profile["env_file"]))
    for alias, var in MODEL_ENV.items():
        m = (profile.get("models") or {}).get(alias)
        if m:
            env[var] = m
    return env


def apply(env, name, profile):
    """A copy of `env` for the claude child: parent provider vars removed, the profile loaded.

    -> (env, the names of the variables that were set; never their values)."""
    if is_builtin(name, profile):
        return dict(env), []
    add = profile_env(name, profile)
    out = {k: v for k, v in env.items()
           if not k.startswith(STRIP_PREFIXES) and k not in STRIP_KEYS}
    out.update(add)
    return out, sorted(add)


def probe(profiles):
    """{name: {"present": bool, "why": str|None}} for the given {name: profile}. stat only."""
    res = {}
    for name, prof in sorted((profiles or {}).items()):
        if is_builtin(name, prof):
            res[name] = {"present": True, "why": None, "builtin": True}
            continue
        why = check_env_file(prof.get("env_file") or "")
        res[name] = {"present": why is None, "why": why}
    return res


def cost_basis(name, profile):
    """What total_cost_usd means for a profile: anthropic, anthropic_estimate, provider or none."""
    if is_builtin(name, profile):
        return "anthropic"
    return (profile or {}).get("cost_basis") or "anthropic_estimate"
