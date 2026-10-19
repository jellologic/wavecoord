"""Remove secrets from text before it is logged (docs/security.md, "Secrets in the log").

The lead's records carry the tail of command output (check_cmd, Verify, merge_cmd, pr_cmd). That
output runs with the lead's environment, and coord/log is pushed and streamed to every agent, so
the text is redacted first:

- the values of environment variables whose names look secret (TOKEN, SECRET, PASSWORD, KEY, ...);
- the coordinator tokens on this machine (~/.wavecoord/token, ~/.wavecoord/hosts/*.token);
- common token formats (GitHub, GitLab, Slack, Anthropic, OpenAI, AWS, Google, bearer headers,
  private key blocks).
"""

import glob
import os
import re

MARK = "[redacted]"
MIN_LEN = 8  # shorter values are too common to replace
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSW|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL|_AUTH|AUTH_", re.I)
PATTERNS = [re.compile(p) for p in (
    r"\bgh[pousr]_[A-Za-z0-9]{20,}",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}",
    r"\bglpat-[A-Za-z0-9_-]{20,}",
    r"\bxox[abposr]-[A-Za-z0-9-]{10,}",
    r"\bsk-[A-Za-z0-9_-]{20,}",
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    r"\bAIza[0-9A-Za-z_-]{35}",
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
)]


def _homes():
    out = [os.path.join(os.path.expanduser("~"), ".wavecoord")]
    if os.environ.get("WAVECOORD_HOME"):
        out.append(os.path.abspath(os.path.expanduser(os.environ["WAVECOORD_HOME"])))
    return out


def secret_values(env=None):
    """The secret strings of this process: secret-looking env values and the token files."""
    env = os.environ if env is None else env
    vals = set()
    for k, v in env.items():
        if SECRET_NAME.search(k) and isinstance(v, str) and len(v.strip()) >= MIN_LEN:
            vals.add(v.strip())
    paths = []
    for home in _homes():
        paths += [os.path.join(home, "token")] + glob.glob(os.path.join(home, "hosts", "*.token"))
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                t = f.read().strip()
        except (OSError, UnicodeDecodeError):
            continue
        if len(t) >= MIN_LEN:
            vals.add(t)
    return sorted(vals, key=len, reverse=True)


def redact(text, values=None):
    """`text` with every secret replaced by [redacted]."""
    if not isinstance(text, str) or not text:
        return text
    for v in secret_values() if values is None else values:
        if v in text:
            text = text.replace(v, MARK)
    for rx in PATTERNS:
        text = rx.sub(MARK, text)
    return text


def redact_obj(obj, values=None):
    """A copy of a JSON value with every string redacted."""
    values = secret_values() if values is None else values
    if isinstance(obj, str):
        return redact(obj, values)
    if isinstance(obj, list):
        return [redact_obj(x, values) for x in obj]
    if isinstance(obj, dict):
        return {k: redact_obj(v, values) for k, v in obj.items()}
    return obj
