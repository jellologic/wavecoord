"""Coordination state: a pure, deterministic fold of the event log, cached in sqlite.

`State` holds the folded state as plain dicts. `State.apply(event)` is the
only way to change it, and it reads nothing but the event (and, for `revert`,
earlier events through a history object). No wall clock, no randomness, so
replay of the same events always gives the same state:

    fold(events) == fold(events)            # deterministic
    State.from_dump(s.dump()).dump() == s.dump()

Liveness (heartbeats, last_seen, lease renewals between transitions) is not in
the fold; the server keeps it in memory (`core.Liveness`).

`Store` persists a State in sqlite (WAL): one row per document, written in one
transaction per event.
"""

import copy
import json
import os
import sqlite3

from wavecoord import const
from wavecoord.globs import globs_overlap

COLLECTIONS = ("meta", "agents", "leases", "epochs", "reservations", "attempts", "messages", "acks",
               "notices", "proposals", "requests", "conflicts", "idem", "counters", "reverted")

# Kinds that `revert <seq>` may undo. Leases and reservations are undone by
# their own transitions, because fencing epochs only go up (rollback.md).
REVERTIBLE = ("message.new", "message.ack", "notify.post", "proposal.new", "dispatch.request", "conflict.open")

LEASE_KEYS = ("id", "task", "attempt", "holder", "epoch", "state", "acquired", "renewed", "expires_at",
              "released_at", "released_by", "reason")


def shift_ts(ts, seconds):
    return const.fmt_ts(const.parse_ts(ts) + seconds)


class ListHistory(object):
    """Earlier events for `revert`, from a plain list (events[i].seq == i + 1)."""

    def __init__(self, events):
        self.events = events

    def get(self, seq):
        return self.events[seq - 1] if 0 < seq <= len(self.events) else None

    def prefix_state(self, seq):
        return fold(self.events[:seq])


def fold(events, start=None, history=None):
    """The State after `events`, starting from `start` (a State or None)."""
    s = start if start is not None else State()
    h = history or ListHistory(events)
    for ev in events:
        s.apply(ev, h)
    return s


class State(object):
    def __init__(self):
        self.d = {c: {} for c in COLLECTIONS}
        self.dirty = set()

    @classmethod
    def from_dump(cls, data):
        s = cls()
        for c in COLLECTIONS:
            s.d[c] = copy.deepcopy((data or {}).get(c, {}))
        return s

    def dump(self):
        return {c: copy.deepcopy(self.d[c]) for c in COLLECTIONS}

    def copy(self):
        return State.from_dump(self.d)

    # ----- document access -----

    def get(self, coll, key):
        return self.d[coll].get(str(key))

    def put(self, coll, key, doc):
        self.d[coll][str(key)] = doc
        self.dirty.add((coll, str(key)))

    def delete(self, coll, key):
        if self.d[coll].pop(str(key), None) is not None:
            self.dirty.add((coll, str(key)))

    def values(self, coll):
        return [self.d[coll][k] for k in sorted(self.d[coll], key=_sort_key)]

    @property
    def last_seq(self):
        return self.d["meta"].get("last_seq", 0)

    def next_id(self, name):
        return self.d["counters"].get(name, 0) + 1

    def _bump(self, name, value):
        if value > self.d["counters"].get(name, 0):
            self.put("counters", name, value)

    # ----- queries -----

    def lease(self, lease_id):
        return self.get("leases", lease_id)

    def task_lease(self, task):
        """The non-released lease of `task`, or None."""
        for l in self.d["leases"].values():
            if l["task"] == task and l["state"] != "released":
                return l
        return None

    def agent_lease(self, agent):
        for l in self.d["leases"].values():
            if l["holder"] == agent and l["state"] != "released":
                return l
        return None

    def last_epoch(self, task):
        return self.d["epochs"].get(task)

    def live_reservations(self):
        return [r for r in self.values("reservations")
                if (self.lease(r["lease_id"]) or {}).get("state", "released") != "released"]

    def conflicts_for(self, items, exclude_lease=None):
        """Overlaps of [{glob, mode}] with reservations of other non-released leases."""
        out = []
        for it in items:
            for r in self.live_reservations():
                if r["lease_id"] == exclude_lease:
                    continue
                if "exclusive" not in (it["mode"], r["mode"]):
                    continue
                if globs_overlap(it["glob"], r["glob"]):
                    out.append({"glob": it["glob"], "lease_id": r["lease_id"], "task": r["task"],
                                "other_glob": r["glob"], "mode": r["mode"]})
        return out

    def path_conflicts(self):
        """Every overlapping pair of live reservations (each pair once)."""
        rs = self.live_reservations()
        out = []
        for i, a in enumerate(rs):
            for b in rs[i + 1:]:
                if a["lease_id"] != b["lease_id"] and "exclusive" in (a["mode"], b["mode"]) \
                        and globs_overlap(a["glob"], b["glob"]):
                    out.append({"kind": "path", "a": a, "b": b})
        return out

    def lost(self, agent):
        """Leases of `agent` that someone else ended, until the agent holds a newer lease."""
        mine = [l for l in self.d["leases"].values() if l["holder"] == agent]
        newest = max([l["id"] for l in mine if l["state"] != "released"] or [0])
        return [{"lease_id": l["id"], "task": l["task"], "epoch": l["epoch"], "reason": l["reason"],
                 "by": l["released_by"]}
                for l in sorted(mine, key=lambda x: x["id"])
                if l["state"] == "released" and l["released_by"] not in (agent, None) and l["id"] > newest]

    def inbox(self, agent, task=None):
        """Unacked messages for `agent` (and for `task`, its current task), oldest first."""
        out = []
        for m in self.values("messages"):
            to = m["to"]
            mine = (to["kind"] == "all" or (to["kind"] == "agent" and to.get("id") == agent)
                    or (to["kind"] == "lead" and agent == "lead")
                    or (to["kind"] == "task" and task is not None and to.get("id") == task))
            if mine and m["sender"] != agent and not self.get("acks", "%d/%s" % (m["id"], agent)):
                out.append(m)
        return out

    # ----- the fold -----

    def apply(self, ev, history=None):
        seq, kind, p = ev["seq"], ev["kind"], ev["payload"]
        if seq != self.last_seq + 1:
            raise ValueError("fold out of order: have %d, got %d" % (self.last_seq, seq))
        fn = getattr(self, "_on_" + kind.replace(".", "_"), None)
        if fn is not None:
            fn(ev, p, history)
        if ev.get("idem"):
            self.put("idem", ev["idem"], seq)
        self.d["meta"]["last_seq"] = seq
        self.d["meta"]["last_hash"] = const.sha256_hex(const.canonical(ev))
        self.dirty.update((("meta", "last_seq"), ("meta", "last_hash")))

    def _on_server_gap(self, ev, p, h):
        gap = p.get("gap_ms", 0) / 1000.0
        for l in self.values("leases"):
            if l["state"] != "released" and l["expires_at"]:
                l = dict(l, expires_at=shift_ts(l["expires_at"], gap))
                self.put("leases", l["id"], l)

    def _on_agent_register(self, ev, p, h):
        a = dict(p["agent"])
        old = self.get("agents", a["id"]) or {}
        a["started"] = old.get("started", a.get("started"))
        a["gone"] = False
        a["end_state"] = None
        self.put("agents", a["id"], a)

    def _on_agent_gone(self, ev, p, h):
        a = self.get("agents", p["agent_id"])
        if a:
            self.put("agents", a["id"], dict(a, gone=True, end_state=p.get("state")))

    def _grant(self, ev, lease, reservations):
        self.put("leases", lease["id"], dict(lease))
        self._bump("lease", lease["id"])
        t = lease["task"]
        if lease["epoch"] > self.d["epochs"].get(t, 0):
            self.put("epochs", t, lease["epoch"])
        for r in reservations or []:
            self.put("reservations", r["id"], dict(r))
            self._bump("reservation", r["id"])
        key = "%s/%d" % (t, lease["attempt"])
        att = self.get("attempts", key) or {"task": t, "n": lease["attempt"], "state": "running",
                                            "host": None, "branch": None, "started": ev["ts"], "ended": None,
                                            "report": None, "cost_usd": None, "turns": None}
        att = dict(att, holder=lease["holder"], lease_id=lease["id"])
        if att["state"] in ("dispatched",):
            att["state"] = "running"
        self.put("attempts", key, att)

    def _end_lease(self, lease_id, ts, reason, by, state="released"):
        l = self.lease(lease_id)
        if not l or l["state"] == "released":
            return
        l = dict(l, state=state, expires_at=None)
        if state == "released":
            l.update(released_at=ts, released_by=by, reason=reason)
            for r in list(self.d["reservations"].values()):
                if r["lease_id"] == lease_id:
                    self.delete("reservations", r["id"])
        self.put("leases", lease_id, l)

    def _on_lease_grant(self, ev, p, h):
        self._grant(ev, p["lease"], p.get("reservations"))

    def _on_lease_steal(self, ev, p, h):
        self._end_lease(p["old_lease_id"], ev["ts"], "steal", ev["actor"].get("agent") or ev["actor"].get("principal"))
        self._grant(ev, p["lease"], p.get("reservations"))

    def _on_lease_stale(self, ev, p, h):
        l = self.lease(p["lease_id"])
        if l and l["state"] == "active":
            self.put("leases", l["id"], dict(l, state="stale", stale_by=p.get("by")))

    def _on_lease_revive(self, ev, p, h):
        l = self.lease(p["lease_id"])
        if l and l["state"] == "stale":
            self.put("leases", l["id"], dict(l, state="active", renewed=ev["ts"], expires_at=p.get("expires_at"),
                                             stale_by=None))

    def _on_lease_park(self, ev, p, h):
        l = self.lease(p["lease_id"])
        if l and l["state"] in ("active", "stale"):
            self.put("leases", l["id"], dict(l, state="parked", renewed=ev["ts"], expires_at=None))

    def _on_lease_release(self, ev, p, h):
        self._end_lease(p["lease_id"], ev["ts"], p.get("reason"), p.get("by"))

    def _on_reservation_add(self, ev, p, h):
        for r in p.get("reservations", []):
            self.put("reservations", r["id"], dict(r))
            self._bump("reservation", r["id"])

    def _on_reservation_remove(self, ev, p, h):
        self.delete("reservations", p["reservation_id"])

    def _attempt(self, ev):
        key = "%s/%d" % (ev["task"], ev["attempt"])
        return key, self.get("attempts", key) or {
            "task": ev["task"], "n": ev["attempt"], "state": "dispatched", "holder": None, "lease_id": None,
            "host": None, "branch": None, "started": ev["ts"], "ended": None, "report": None,
            "cost_usd": None, "turns": None}

    def _on_attempt_start(self, ev, p, h):
        key, a = self._attempt(ev)
        a = dict(a, state=p.get("state", "running"))
        for k in ("host", "branch", "holder"):
            if p.get(k) is not None:
                a[k] = p[k]
        self.put("attempts", key, a)

    def _on_attempt_end(self, ev, p, h):
        key, a = self._attempt(ev)
        self.put("attempts", key, dict(a, state=p["state"], ended=ev["ts"]))

    def _on_report_post(self, ev, p, h):
        key, a = self._attempt(ev)
        rep = p["report"]
        a = dict(a, report=rep, cost_usd=p.get("cost_usd"), turns=p.get("turns"), ended=ev["ts"],
                 state="failed" if rep.get("status") == "failed" else "reported")
        for k in ("cost_basis", "usage", "model_usage", "provider", "throttled", "throttle", "next_provider",
                  "pushed", "pr_url"):
            if p.get(k) is not None:
                a[k] = p[k]
        self.put("attempts", key, a)

    def _on_message_new(self, ev, p, h):
        m = dict(p["message"])
        self.put("messages", m["id"], m)
        self._bump("message", m["id"])

    def _on_message_ack(self, ev, p, h):
        if self.get("messages", p["message_id"]):
            self.put("acks", "%d/%s" % (p["message_id"], p["agent"]),
                     {"message_id": p["message_id"], "agent": p["agent"], "ts": ev["ts"]})

    def _on_notify_post(self, ev, p, h):
        n = dict(p["notice"])
        self.put("notices", n["id"], n)
        self._bump("notice", n["id"])

    def _on_proposal_new(self, ev, p, h):
        pr = dict(p["proposal"])
        self.put("proposals", pr["id"], pr)
        self._bump("proposal", pr["id"])

    def _on_proposal_decide(self, ev, p, h):
        pr = self.get("proposals", p["proposal_id"])
        if pr:
            self.put("proposals", pr["id"], dict(pr, state=p["decision"], note=p.get("note"),
                                                   decided_by=p.get("by"), decided_ts=ev["ts"]))

    def _on_dispatch_request(self, ev, p, h):
        r = dict(p["request"])
        self.put("requests", r["id"], r)
        self._bump("request", r["id"])

    def _on_dispatch_decide(self, ev, p, h):
        rid = p.get("request_id")
        r = self.get("requests", rid) if rid is not None else None
        if r:
            self.put("requests", rid, dict(r, state="decided", decided_ts=ev["ts"]))

    def _on_conflict_open(self, ev, p, h):
        c = dict(p["conflict"])
        self.put("conflicts", c["id"], c)
        self._bump("conflict", c["id"])

    def _on_conflict_close(self, ev, p, h):
        c = self.get("conflicts", p["conflict_id"])
        if c:
            self.put("conflicts", c["id"], dict(c, state="closed", closed_ts=ev["ts"]))

    def _on_revert(self, ev, p, h):
        if "to_seq" in p:
            self._rollback(ev, p, h)
            return
        target = h.get(p["target_seq"]) if h else None
        if target is None:
            return
        tp = target["payload"]
        k = target["kind"]
        if k == "message.new":
            mid = tp["message"]["id"]
            self.delete("messages", mid)
            for key in [a for a in self.d["acks"] if a.startswith("%d/" % mid)]:
                self.delete("acks", key)
        elif k == "message.ack":
            self.delete("acks", "%d/%s" % (tp["message_id"], tp["agent"]))
        elif k == "notify.post":
            self.delete("notices", tp["notice"]["id"])
        elif k == "proposal.new":
            self.delete("proposals", tp["proposal"]["id"])
        elif k == "dispatch.request":
            self.delete("requests", tp["request"]["id"])
        elif k == "conflict.open":
            self.delete("conflicts", tp["conflict"]["id"])
        self.put("reverted", p["target_seq"], ev["seq"])

    def _rollback(self, ev, p, h):
        """State after `to_seq`, with the fencing exceptions of rollback.md."""
        prefix = h.prefix_state(p["to_seq"])
        new = prefix.copy()
        by = ev["actor"].get("agent") or ev["actor"].get("principal")
        # Epochs and ids never go back.
        for t, e in self.d["epochs"].items():
            if e > new.d["epochs"].get(t, 0):
                new.d["epochs"][t] = e
        for n, v in self.d["counters"].items():
            if v > new.d["counters"].get(n, 0):
                new.d["counters"][n] = v
        # A lease granted after to_seq ends with reason rollback; a lease that
        # ended after to_seq stays ended (its holder was told it lost the lease).
        for lid, l in self.d["leases"].items():
            old = new.d["leases"].get(lid)
            if old is None:
                l = dict(l)
                if l["state"] != "released":
                    l.update(state="released", expires_at=None, released_at=ev["ts"], released_by=by,
                             reason="rollback")
                new.d["leases"][lid] = l
            elif l["state"] == "released" and old["state"] != "released":
                new.d["leases"][lid] = dict(l)
        for rid, r in list(new.d["reservations"].items()):
            if new.d["leases"].get(str(r["lease_id"]), {}).get("state", "released") == "released":
                del new.d["reservations"][rid]
        # Host bindings are forever; idem keys stay used.
        for aid, a in self.d["agents"].items():
            new.d["agents"][aid] = dict(a)
        for k, v in self.d["idem"].items():
            new.d["idem"].setdefault(k, v)
        for k, v in self.d["reverted"].items():
            new.d["reverted"].setdefault(k, v)
        new.d["meta"] = dict(self.d["meta"])
        new.d["meta"]["rolled_back_to"] = p["to_seq"]
        for c in COLLECTIONS:
            for k in set(self.d[c]) | set(new.d[c]):
                if self.d[c].get(k) != new.d[c].get(k):
                    self.dirty.add((c, k))
            self.d[c] = new.d[c]


def _sort_key(k):
    return (0, int(k), "") if k.isdigit() else (1, 0, k)


class Store(object):
    """A State persisted in sqlite (WAL mode)."""

    def __init__(self, path):
        self.path = path
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("CREATE TABLE IF NOT EXISTS docs (coll TEXT NOT NULL, k TEXT NOT NULL, v TEXT NOT NULL,"
                          " PRIMARY KEY (coll, k))")

    def load(self):
        data = {c: {} for c in COLLECTIONS}
        for coll, k, v in self.conn.execute("SELECT coll, k, v FROM docs"):
            if coll in data:
                data[coll][k] = json.loads(v)
        return State.from_dump(data)

    def save(self, state, full=False):
        """Write the dirty documents of `state` (all of them when `full`) in one transaction."""
        c = self.conn
        c.execute("BEGIN")
        try:
            if full:
                c.execute("DELETE FROM docs")
                keys = [(coll, k) for coll in COLLECTIONS for k in state.d[coll]]
            else:
                keys = sorted(state.dirty)
            for coll, k in keys:
                v = state.d[coll].get(k)
                if v is None:
                    c.execute("DELETE FROM docs WHERE coll = ? AND k = ?", (coll, k))
                else:
                    c.execute("INSERT OR REPLACE INTO docs (coll, k, v) VALUES (?, ?, ?)",
                              (coll, k, json.dumps(v, sort_keys=True)))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        state.dirty.clear()

    def close(self):
        self.conn.close()
