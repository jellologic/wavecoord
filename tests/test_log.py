import json
import os
import threading
import time
import unittest

import _coord
from wavecoord import const
from wavecoord.log import EventLog, LogError, git, read_branch


def ev(kind="notify.post", payload=None, idem=None, ts="2026-10-18T12:00:00Z"):
    return {"ts": ts, "kind": kind, "actor": {"agent": None, "host": "local", "session_id": None},
            "task": None, "attempt": None, "payload": payload or {}, "idem": idem}


class LogTest(unittest.TestCase):
    def setUp(self):
        self.tr = _coord.TempRepo()
        self.dir = os.path.join(self.tr.home, "log")

    def tearDown(self):
        self.tr.cleanup()

    def open(self, **kw):
        kw.setdefault("commit_window_ms", 0)
        kw.setdefault("push_retry_s", 0.2)
        log = EventLog(self.tr.repo, self.dir, project="demo", **kw)
        log.open()
        self.addCleanup(log.close)
        return log

    def test_chain_and_layout(self):
        log = self.open()
        for i in range(5):
            e, created = log.append(ev(payload={"i": i}))
            self.assertTrue(created)
        log.wait(5)
        evs = read_branch(self.tr.repo)
        self.assertEqual([e["seq"] for e in evs], [1, 2, 3, 4, 5])
        self.assertIsNone(evs[0]["prev"])
        for a, b in zip(evs, evs[1:]):
            self.assertEqual(b["prev"], const.sha256_hex(const.canonical(a)))
        names = git(self.dir, "ls-tree", "-r", "--name-only", "HEAD").split()
        self.assertIn("meta.json", names)
        self.assertIn("events/2026/10/18/%012d-%s.json" % (1, log.hashes[0][:12]), names)
        # The branch is an orphan: no common history with main.
        self.assertIsNone(git(self.tr.repo, "merge-base", "main", "coord/log", check=False))

    def test_idempotency(self):
        log = self.open()
        a, c1 = log.append(ev(idem="report:t:1"))
        b, c2 = log.append(ev(payload={"other": 1}, idem="report:t:1"))
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(a, b)
        self.assertEqual(log.last_seq, 1)
        log.close()
        log2 = EventLog(self.tr.repo, self.dir).open(start_threads=False)
        self.assertEqual(log2.find_idem("report:t:1")["seq"], 1)
        self.assertFalse(log2.append(ev(idem="report:t:1"))[1])

    def test_invalid_event_rejected(self):
        log = self.open()
        with self.assertRaises(ValueError):
            log.append(ev(kind="lease.bogus"))
        self.assertEqual(log.last_seq, 0)

    def test_group_commit_acks_after_commit(self):
        log = self.open(commit_window_ms=300)
        seqs, errs = [], []

        def writer(i):
            e, _ = log.append(ev(payload={"i": i}))
            log.wait(e["seq"])
            # The ack point: the event must be in a commit now.
            out = git(self.dir, "ls-tree", "-r", "--name-only", "HEAD", "--", "events")
            if ("%012d-" % e["seq"]) not in out:
                errs.append(e["seq"])
            seqs.append(e["seq"])

        t0 = time.time()
        ts = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertGreaterEqual(time.time() - t0, 0.25)
        self.assertEqual(errs, [])
        self.assertEqual(sorted(seqs), list(range(1, 11)))
        commits = git(self.dir, "rev-list", "--count", "HEAD").strip()
        self.assertLess(int(commits), 11)  # batched: fewer commits than events (+1 for meta)

    def test_push_to_remote(self):
        log = self.open()
        e, _ = log.append(ev())
        log.wait(e["seq"])
        deadline = time.time() + 10
        while log.pushed_seq < 1 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(log.pushed_seq, 1)
        self.assertEqual(len(read_branch(self.tr.remote)), 1)

    def test_push_failure_never_blocks(self):
        _coord.git(self.tr.repo, "remote", "set-url", "origin", os.path.join(self.tr.root, "missing.git"))
        log = self.open()
        e, _ = log.append(ev())
        log.wait(e["seq"])  # returns although every push fails
        time.sleep(0.3)
        self.assertEqual(log.pushed_seq, 0)
        self.assertIsNotNone(log.push_error)

    def test_reopen_and_restore_from_remote(self):
        log = self.open()
        for i in range(3):
            log.append(ev(payload={"i": i}))
        log.flush()
        log.close()
        self.assertTrue(log.push_once())
        # A fresh machine: new clone of the consumer repo, no local log branch.
        tr2 = os.path.join(self.tr.root, "clone")
        _coord.git(self.tr.root, "clone", "-q", self.tr.remote, tr2)
        log2 = EventLog(tr2, os.path.join(self.tr.root, "home2", "log")).open(start_threads=False)
        self.assertEqual(log2.last_seq, 3)
        self.assertEqual(log2.hashes, log.hashes)

    def test_uncommitted_tail_is_dropped(self):
        log = EventLog(self.tr.repo, self.dir).open(start_threads=False)
        log.append(ev())
        log.flush()
        log.append(ev(payload={"never": "acked"}))  # written, never committed
        log2 = EventLog(self.tr.repo, self.dir).open(start_threads=False)
        self.assertEqual(log2.last_seq, 1)

    def test_tamper_detected(self):
        log = EventLog(self.tr.repo, self.dir).open(start_threads=False)
        for i in range(3):
            log.append(ev(payload={"i": i}))
        log.flush()
        path = [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(self.dir, "events")) for f in fs
                if f.startswith("%012d" % 2)][0]
        with open(path) as f:
            data = json.loads(f.read())
        data["payload"] = {"i": 99}
        with open(path, "wb") as f:
            f.write(const.canonical(data))
        git(self.dir, "commit", "-qam", "tamper")
        with self.assertRaises(LogError):
            EventLog(self.tr.repo, self.dir).open(start_threads=False)
        with self.assertRaises(LogError):
            read_branch(self.tr.repo)

    def test_snapshots_and_last_seq_at(self):
        log = EventLog(self.tr.repo, self.dir, snapshot_every=2).open(start_threads=False)
        shas = []
        for i in range(4):
            e, _ = log.append(ev(payload={"i": i}))
            if e["seq"] % 2 == 0:
                log.add_snapshot(e["seq"], {"x": e["seq"]})
            log.flush()
            shas.append(log.head())
        self.assertEqual([log.last_seq_at(s) for s in shas], [1, 2, 3, 4])
        self.assertEqual([e["seq"] for e in log.replay(shas[1])], [1, 2])
        self.assertEqual([e["seq"] for e in log.replay()], [1, 2, 3, 4])
        self.assertEqual(log.snapshot_before(3), (2, {"x": 2}))
        self.assertEqual(log.snapshot_before(1), (0, None))
        with self.assertRaises(LogError):
            log.last_seq_at(git(self.tr.repo, "rev-parse", "main").strip())


if __name__ == "__main__":
    unittest.main()
