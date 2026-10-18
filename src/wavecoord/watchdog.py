"""The watchdog: nudge, then mark stale, an idle live lease (docs/feedback.md).

The TTL reaper (contract 3.1) only sees heartbeats. A launcher heartbeats
while its `claude -p` process lives, also when the worker hangs: on a long
retry loop, a prompt it cannot answer, or a dead tool. The watchdog looks at
real activity instead:

- a hook or MCP heartbeat, or a launcher heartbeat with a new `last_tool`
  (core.Coord.hb records it as `seen[agent]["act"]`);
- a new commit on the attempt's branch (local ref or `origin`).

For each `active` lease with no activity for WATCHDOG_NUDGE_S, the watchdog
sends one nudge message to the holder's inbox (logged as `message.new`, from
`watchdog`). With still no activity WATCHDOG_STALE_S after the nudge, it logs
`lease.stale` with `by: "watchdog"`. A launcher heartbeat alone does not
revive that lease; real activity does, and the lead can steal or release it.

It never touches a lease with `user_set` set, or a lease held by a `lead` or
`other` agent (an interactive session the user drives). All idle state is in
memory; a server restart starts every count again.

Configuration: the constants WATCHDOG_S (check interval, 0 disables),
WATCHDOG_NUDGE_S and WATCHDOG_STALE_S, overridable by WAVECOORD_<NAME>, and
the `watchdog` object of the server config (`interval_s`, `nudge_s`,
`stale_s`, `enabled`).
"""

import subprocess
import threading

from wavecoord import const
from wavecoord.core import ApiError, Ctx

SENDER = "watchdog"
EXEMPT_KINDS = ("lead", "other")
GIT_TIMEOUT_S = 10
NUDGE = ("wavecoord watchdog: no tool activity and no new commit on task %s for %d min. If you are stuck, "
         "commit and push what you have, then send a report or a message to the lead that says what blocks "
         "you. If you are still working, continue; any tool call resets this timer. Without activity, the "
         "lease becomes stale in %d min and the lead may give the task to another agent.")


def git_head(repo, branch):
    """A signature of the branch head: (local sha, origin sha). None parts when absent."""
    if not branch:
        return None
    out = []
    for args in (["rev-parse", "-q", "--verify", "refs/heads/" + branch],
                 ["ls-remote", "origin", "refs/heads/" + branch]):
        try:
            p = subprocess.run(["git"] + args, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               universal_newlines=True, timeout=GIT_TIMEOUT_S)
            out.append(p.stdout.split()[0] if p.returncode == 0 and p.stdout.strip() else None)
        except (OSError, subprocess.SubprocessError, IndexError):
            out.append(None)
    return tuple(out)


class Watchdog(object):
    def __init__(self, coord, interval_s=None, nudge_s=None, stale_s=None, head=None):
        self.coord = coord
        self.interval_s = const.get("WATCHDOG_S") if interval_s is None else interval_s
        self.nudge_s = const.get("WATCHDOG_NUDGE_S") if nudge_s is None else nudge_s
        self.stale_s = const.get("WATCHDOG_STALE_S") if stale_s is None else stale_s
        self.head = head or (lambda branch: git_head(coord.repo, branch))
        self.track = {}  # lease id -> {"since", "head", "nudged"}
        self.started = coord.now()
        self._stop = threading.Event()
        self._thread = None

    @classmethod
    def from_config(cls, coord, cfg=None):
        """A watchdog from the server config `watchdog` object, or None when disabled."""
        cfg = cfg if isinstance(cfg, dict) else {}
        if cfg.get("enabled") is False:
            return None
        wd = cls(coord, cfg.get("interval_s"), cfg.get("nudge_s"), cfg.get("stale_s"))
        return wd if wd.interval_s and wd.interval_s > 0 else None

    # -- the check

    def _candidates(self):
        """(lease, branch, last activity) for each lease the watchdog may act on."""
        c = self.coord
        out = []
        with c.wlock:
            for l in c.state.values("leases"):
                if l["state"] != "active" or l.get("user_set"):
                    continue
                a = c.state.get("agents", l["holder"]) or {}
                if a.get("kind") in EXEMPT_KINDS or l["holder"] in ("lead", "ui"):
                    continue
                att = c.state.get("attempts", "%s/%d" % (l["task"], l["attempt"])) or {}
                act = (c.seen.get(l["holder"]) or {}).get("act")
                out.append((dict(l), att.get("branch") or a.get("branch"), act))
        return out

    def check(self):
        """One pass. Returns {"nudged": [lease ids], "stale": [lease ids]}."""
        now = self.coord.now()
        done = {"nudged": [], "stale": []}
        seen_ids = set()
        for l, branch, act in self._candidates():
            lid = l["id"]
            seen_ids.add(lid)
            rec = self.track.get(lid)
            if rec is None:
                start = max(const.parse_ts(l["acquired"]), act or 0, self.started)
                rec = self.track[lid] = {"since": start, "head": self.head(branch), "nudged": None}
            if act is not None and act > rec["since"]:
                rec["since"], rec["nudged"] = act, None
            if now - rec["since"] < self.nudge_s:
                continue
            h = self.head(branch)
            if h != rec["head"]:  # a new commit counts as activity
                rec["head"], rec["since"], rec["nudged"] = h, now, None
                continue
            if rec["nudged"] is None:
                if self.nudge(l, now, rec):
                    rec["nudged"] = now
                    done["nudged"].append(lid)
            elif now - rec["nudged"] >= self.stale_s:
                if self.mark_stale(l, now, rec):
                    done["stale"].append(lid)
                self.track.pop(lid, None)
        for lid in list(self.track):
            if lid not in seen_ids:
                self.track.pop(lid, None)
        return done

    def _ctx(self):
        return Ctx("lead", SENDER, agent=SENDER)

    def nudge(self, l, now, rec):
        text = NUDGE % (l["task"], int((now - rec["since"]) // 60), max(1, int(self.stale_s // 60)))
        try:
            self.coord.send(self._ctx(), {"to": {"kind": "agent", "id": l["holder"]}, "body": text},
                            idem="watchdog:nudge:%d:%d" % (l["id"], int(rec["since"])))
            return True
        except ApiError:
            return False

    def mark_stale(self, l, now, rec):
        c = self.coord
        idle_min = int((now - rec["since"]) // 60)

        def build():
            cur = c.state.lease(l["id"])
            if cur is None or cur["state"] != "active" or cur.get("user_set"):
                return []
            act = (c.seen.get(cur["holder"]) or {}).get("act")
            if act is not None and act > rec["since"]:
                return []  # activity arrived after the check
            c.renew.pop(cur["id"], None)
            return [c._ev("lease.stale", {"lease_id": cur["id"], "by": SENDER,
                                          "reason": "no activity for %d min" % idle_min},
                          cur["task"], cur["attempt"])]

        try:
            logged, _, _ = c.write(self._ctx(), "watchdog:stale:%d:%d" % (l["id"], int(rec["since"])), build,
                                   actor_agent=None, expect=("lease.stale",))
        except ApiError:
            return False
        return bool(logged)

    # -- the thread

    def _loop(self):
        while not self._stop.wait(self.interval_s):
            try:
                self.check()
            except Exception:  # the watchdog must never die
                pass

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="wavecoord-watchdog", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(5)
