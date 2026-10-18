"""Contract constants (docs/contract.md section 5) and small shared helpers.

A test or a deployment may override a constant with the environment variable
WAVECOORD_<NAME>, for example WAVECOORD_LEASE_TTL_S=5.
"""

import calendar
import hashlib
import json
import os
import time

DEFAULTS = {
    "HB_INTERVAL_S": 30,
    "HOOK_MIN_INTERVAL_S": 10,
    "AGENT_LIVE_S": 90,
    "LEASE_TTL_S": 600,
    "PARKED_DRIFT_S": 259200,
    "GAP_S": 60,
    "CLOCK_BACK_MS": 5000,
    "REAPER_S": 15,
    "COMMIT_WINDOW_MS": 200,
    "SNAPSHOT_EVERY": 1000,
    "PUSH_RETRY_S": 30,
    "MSG_MAX_BYTES": 4096,
    "REQ_MAX_BYTES": 65536,
    "INBOX_MAX": 20,
    "SSE_PING_S": 15,
    "CLIENT_TIMEOUT_S": 3,
    "WATCHDOG_S": 60,
    "WATCHDOG_NUDGE_S": 1200,
    "WATCHDOG_STALE_S": 600,
}

API_VERSION = 1
SCHEMA_VERSION = 1
DEFAULT_PORT = 18741
DEFAULT_LOG_BRANCH = "coord/log"


def get(name, env=None):
    """The value of constant `name`, with the WAVECOORD_ override applied."""
    env = os.environ if env is None else env
    default = DEFAULTS[name]
    raw = env.get("WAVECOORD_" + name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return type(default)(float(raw)) if isinstance(default, int) else float(raw)
    except ValueError:
        return default


def canonical(obj):
    """Canonical JSON bytes: sorted keys, no spaces, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def fmt_ts(t):
    """Seconds since the epoch -> YYYY-MM-DDTHH:MM:SSZ (UTC)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(t)))


def parse_ts(s):
    """YYYY-MM-DDTHH:MM:SS[.ffffff]Z -> seconds since the epoch (float)."""
    base, frac = s.rstrip("Z"), 0.0
    if "." in base:
        base, f = base.split(".", 1)
        frac = float("0." + f)
    return calendar.timegm(time.strptime(base, "%Y-%m-%dT%H:%M:%S")) + frac


def home_dir(project, env=None):
    """$WAVECOORD_HOME, else ~/.wavecoord/<project>."""
    env = os.environ if env is None else env
    h = env.get("WAVECOORD_HOME", "").strip()
    if h:
        return os.path.abspath(os.path.expanduser(h))
    return os.path.join(os.path.expanduser("~"), ".wavecoord", project)
