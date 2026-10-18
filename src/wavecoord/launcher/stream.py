"""Read the `claude -p --output-format stream-json` stream of a worker.

`StreamStats.feed(line)` takes one line at a time, so the wrapper can tail the
stream while the worker runs. It records the session, turns, cost, API retries,
rate-limit events, the last tool and the final result with its
`structured_output`. Unknown line types are counted and otherwise ignored.
"""

import json
import re

# api_retry error categories that mean the account or the API is throttled.
THROTTLE_ERRORS = ("rate_limit", "overloaded")
THROTTLE_RE = re.compile(r"rate.?limit|overloaded|\b(429|529)\b", re.I)


class StreamStats(object):
    def __init__(self):
        self.session_id = None
        self.model = None
        self.version = None
        self.plugins = []
        self.plugin_errors = []
        self.turns = 0  # assistant messages of the main conversation (live count)
        self.tokens = 0  # input + output + cache-write tokens of the main conversation (live count)
        self._usage_ids = {}
        self.cost_usd = None
        self.num_turns = None
        self.result = None  # the final `result` line (dict)
        self.api_retries = []
        self.rate_limits = []
        self.hook_events = 0
        self.last_tool = None
        self.stop_failures = []
        self.lines = 0
        self.bad_lines = 0
        self.other = {}

    def feed(self, line):
        """Parse one line. -> a list of notable events: ('api_retry'|'rate_limit', dict)."""
        line = line.strip()
        if not line:
            return []
        self.lines += 1
        try:
            e = json.loads(line)
        except ValueError:
            self.bad_lines += 1
            return []
        if not isinstance(e, dict):
            self.bad_lines += 1
            return []
        t = e.get("type")
        st = e.get("subtype")
        notable = []
        if t == "system" and st == "init":
            self.session_id = e.get("session_id") or self.session_id
            self.model = e.get("model")
            self.version = e.get("claude_code_version")
            self.plugins = [p.get("name") for p in (e.get("plugins") or []) if isinstance(p, dict)]
            self.plugin_errors = list(e.get("plugin_errors") or [])
        elif t == "system" and st == "api_retry":
            ev = {k: e.get(k) for k in ("attempt", "max_retries", "retry_delay_ms", "error_status", "error")}
            self.api_retries.append(ev)
            notable.append(("api_retry", ev))
        elif t == "system" and st in ("hook_started", "hook_progress", "hook_response"):
            self.hook_events += 1
            if e.get("hook_event") == "StopFailure" and st == "hook_started":
                ev = {"hook": e.get("hook_name")}
                self.stop_failures.append(ev)
                notable.append(("stop_failure", ev))
        elif t == "rate_limit_event":
            info = e.get("rate_limit_info") or {}
            ev = {k: info.get(k) for k in ("status", "resetsAt", "rateLimitType", "utilization",
                                            "overageStatus") if k in info}
            self.rate_limits.append(ev)
            if (info.get("status") or "allowed") != "allowed":
                notable.append(("rate_limit", ev))
        elif t == "assistant":
            if e.get("parent_tool_use_id") is None:
                self.turns += 1
                self._count_tokens(e.get("message") or {})
            for block in ((e.get("message") or {}).get("content") or []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self.last_tool = _tool_summary(block)
        elif t == "result":
            self.result = e
            self.session_id = e.get("session_id") or self.session_id
            self.cost_usd = e.get("total_cost_usd")
            self.num_turns = e.get("num_turns")
        else:
            key = "%s/%s" % (t, st) if st else str(t)
            self.other[key] = self.other.get(key, 0) + 1
        return notable

    def _count_tokens(self, msg):
        """One message id can come in several lines (one per content block): count its latest usage once."""
        u = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(u, dict):
            return
        n = 0
        for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens"):
            v = u.get(k)
            if isinstance(v, int) and v > 0:
                n += v
        mid = msg.get("id") or "line-%d" % self.lines
        self.tokens += n - self._usage_ids.get(mid, 0)
        self._usage_ids[mid] = n

    @property
    def structured_output(self):
        return (self.result or {}).get("structured_output")

    @property
    def is_error(self):
        if self.result is None:
            return True
        return bool(self.result.get("is_error")) or self.result.get("subtype") not in (None, "success")

    @property
    def usage(self):
        return (self.result or {}).get("usage")

    @property
    def model_usage(self):
        return (self.result or {}).get("modelUsage")

    def throttle(self):
        """Why this run looks rate limited or overloaded ([] = not). The dispatcher switches providers on it."""
        why = []
        n = len([r for r in self.api_retries if r.get("error") in THROTTLE_ERRORS])
        if n:
            why.append("api_retry:%d" % n)
        if [r for r in self.rate_limits if (r.get("status") or "allowed") != "allowed"]:
            why.append("rate_limit_event")
        if self.stop_failures:
            why.append("stop_failure")
        r = self.result or {}
        if r.get("is_error") and THROTTLE_RE.search(str(r.get("result") or "")):
            why.append("result")
        return why

    def summary(self):
        return {
            "session_id": self.session_id,
            "model": self.model,
            "claude_version": self.version,
            "turns": self.num_turns if self.num_turns is not None else self.turns,
            "cost_usd": self.cost_usd,
            "tokens": self.tokens,
            "api_retries": len(self.api_retries),
            "throttled_retries": len([r for r in self.api_retries if r.get("error") in THROTTLE_ERRORS]),
            "rate_limits": self.rate_limits[-5:],
            "hook_events": self.hook_events,
            "last_tool": self.last_tool,
            "result_subtype": (self.result or {}).get("subtype"),
            "is_error": self.is_error,
            "usage": self.usage,
            "model_usage": self.model_usage,
            "throttle": self.throttle(),
            "plugins": self.plugins,
            "plugin_errors": self.plugin_errors,
            "lines": self.lines,
            "bad_lines": self.bad_lines,
        }


def _tool_summary(block):
    name = block.get("name") or "?"
    inp = block.get("input") or {}
    detail = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
    s = "%s: %s" % (name, detail) if detail else name
    return s.replace("\n", " ")[:200]


def parse_file(path):
    st = StreamStats()
    try:
        with open(path, "rb") as f:
            for raw in f:
                st.feed(raw.decode("utf-8", "replace"))
    except OSError:
        pass
    return st
