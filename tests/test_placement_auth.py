"""Placement and AUTH by provider (0.1.1 fix).

The bug: ssh hosts with no claude login but with a provider env file (mode 600)
showed AUTH `no` in `wavecoord hosts`, and an attempt whose provider chain had a
provider those hosts can run went to the `local` host (2 slots) instead of the
build hosts (4 free slots each). A host whose env file for one provider of the
attempt (its provider, then its fallbacks) is present counts as authenticated for
that attempt; then placement takes the most free slots, then the most memory.
A host with `dispatch: false` is never picked automatically, only by --host."""

import contextlib
import io
import json
import os
import tempfile
import types
import unittest

import _paths  # noqa: F401
from wavecoord.launcher import cli, client
from wavecoord.launcher.common import LauncherError
from wavecoord.launcher.config import Config
from wavecoord.lead.base import LeadBase

ZAI = {"present": True, "why": None}
MISSING = {"present": False, "why": "env file ~/.config/wavecoord/providers/zai.env is missing"}
BUILTIN = {"present": True, "why": None, "builtin": True}


def probe(free, mem=64.0, auth="no", providers=None, slots=4, ok=True):
    return {"ok": ok, "free": free, "active": slots - free, "slots": slots, "mem_avail_gib": mem,
            "repo_ok": True, "claude": {"installed": True, "auth": auth, "version": "2.1.302 (Claude Code)"},
            "providers": providers or {}, "error": None if ok else "Permission denied (publickey)"}


def raw_config(dev_extra=None, local_extra=None):
    """The production shape: local (every profile, a keychain login) and build1..3 (zai, openrouter; no login)."""
    local = {"name": "local", "slots": 2, "repo_path": "."}
    local.update(local_extra or {})
    hosts = [local]
    for i in (1, 2, 3):
        h = {"name": "build%d" % i, "ssh": "worker@10.0.0.%d" % i, "slots": 4, "repo_path": "~/repo",
             "providers": ["zai", "openrouter"]}
        h.update((dev_extra or {}).get(h["name"]) or {})
        hosts.append(h)
    return {"project": "demo", "hosts": hosts, "default_provider": "zai",
            "providers": {"zai": {"env_file": "~/.config/wavecoord/providers/zai.env", "cost_basis": "none"},
                          "openrouter": {"env_file": "~/.config/wavecoord/providers/openrouter.env"}}}


def production_probes(dev=None):
    dev = dev or {}
    out = {"local": probe(2, mem=40.0, auth="keychain?", slots=2,
                          providers={"anthropic": BUILTIN, "openrouter": ZAI, "zai": ZAI})}
    for i, mem in ((1, 66.0), (2, 65.6), (3, 65.8)):
        n = "build%d" % i
        out[n] = dev.get(n) or probe(4, mem=mem, providers={"openrouter": ZAI, "zai": ZAI})
    return out


class StubLauncher(client.Launcher):
    """A Launcher whose probe returns fixed results (no host is called)."""

    def __init__(self, raw, probes):
        super(StubLauncher, self).__init__(Config(raw, tempfile.gettempdir()), coord_url="")
        self.probes = probes
        self.probed = []

    def probe(self, names=None):
        names = names or sorted(self.hosts)
        self.probed.append(list(names))
        return {n: self.probes[n] for n in names}


class AttemptAuthTest(unittest.TestCase):
    def test_provider_file_is_the_auth(self):
        p = probe(4, auth="no", providers={"zai": ZAI})
        self.assertEqual(client.attempt_auth(p, "zai"), ("provider:zai", None))
        label, why = client.attempt_auth(p, "anthropic")
        self.assertIsNone(label)
        self.assertIn("not logged in", why)

    def test_missing_or_open_file_is_not_auth(self):
        label, why = client.attempt_auth(probe(4, providers={"zai": MISSING}), "zai")
        self.assertIsNone(label)
        self.assertIn("missing", why)
        label, why = client.attempt_auth(probe(4, auth="logged-in"), "zai")
        self.assertIsNone(label)
        self.assertIn("not on this host", why)

    def test_builtin_anthropic_uses_the_login(self):
        self.assertEqual(client.attempt_auth(probe(2, auth="keychain?"), "anthropic"), ("keychain?", None))
        self.assertEqual(client.attempt_auth(probe(2, auth="logged-in", providers={"anthropic": BUILTIN}),
                                             "anthropic"), ("logged-in", None))


class PlacementTest(unittest.TestCase):
    def test_default_provider_goes_to_the_dev_hosts(self):
        L = StubLauncher(raw_config(), production_probes())
        sel = L.select_host({"task": "t1"})
        self.assertEqual((sel["host"], sel["provider"]), ("build1", "zai"))
        self.assertEqual(sel["skipped"], {})

    def test_fallback_provider_counts_as_auth(self):
        # the reproducer: anthropic first, zai as fallback; the build hosts have no login
        L = StubLauncher(raw_config(), production_probes())
        sel = L.select_host({"task": "t1", "provider": "anthropic", "provider_fallback": ["zai"]})
        self.assertEqual((sel["host"], sel["provider"]), ("build1", "zai"))
        order, why = client.placement(L.probes, ["anthropic", "zai"], L.allowed(sorted(L.hosts)))
        self.assertEqual(order, [("build1", "zai"), ("build3", "zai"), ("build2", "zai"), ("local", "anthropic")])
        self.assertEqual(why, {})

    def test_host_uses_the_fallback_when_its_first_file_is_missing(self):
        probes = production_probes({n: probe(4, mem=64.0, providers={"openrouter": ZAI, "zai": MISSING})
                                    for n in ("build1", "build2", "build3")})
        L = StubLauncher(raw_config(), probes)
        sel = L.select_host({"task": "t1", "provider": "zai", "provider_fallback": ["openrouter"]})
        self.assertEqual((sel["host"], sel["provider"]), ("build1", "openrouter"))
        sel = L.select_host({"task": "t1", "provider": "zai"})
        self.assertEqual((sel["host"], sel["provider"]), ("local", "zai"))
        self.assertIn("missing", sel["skipped"]["build1"])

    def test_config_providers_limit_the_chain(self):
        # build hosts do not list anthropic: a login there must not make them eligible for it
        probes = production_probes({n: probe(4, auth="logged-in", providers={"openrouter": ZAI, "zai": ZAI})
                                    for n in ("build1", "build2", "build3")})
        L = StubLauncher(raw_config(), probes)
        sel = L.select_host({"task": "t1", "provider": "anthropic"})
        self.assertEqual((sel["host"], sel["provider"]), ("local", "anthropic"))
        self.assertEqual(L.probed[-1], ["local"])

    def test_backed_off_fallback_is_not_placed(self):
        L = StubLauncher(raw_config(), production_probes())
        sel = L.select_host({"task": "t1", "provider": "anthropic", "provider_fallback": ["zai"],
                             "placement_avoid": ["zai"]})
        self.assertEqual((sel["host"], sel["provider"]), ("local", "anthropic"))

    def test_skipped_hosts_say_why(self):
        probes = production_probes({"build1": probe(0, ok=False)})
        L = StubLauncher(raw_config(), probes)
        sel = L.select_host({"task": "t1"})
        self.assertEqual(sel["host"], "build3")
        self.assertIn("unreachable", sel["skipped"]["build1"])

    def test_most_free_slots_then_memory(self):
        probes = production_probes({"build1": probe(1, mem=99.0, providers={"zai": ZAI}),
                                    "build2": probe(3, mem=10.0, providers={"zai": ZAI}),
                                    "build3": probe(3, mem=20.0, providers={"zai": ZAI})})
        L = StubLauncher(raw_config(), probes)
        self.assertEqual(L.select_host({"task": "t1"})["host"], "build3")


class DispatchFalseTest(unittest.TestCase):
    def raw(self):
        return raw_config(local_extra={"dispatch": False})

    def test_schema_accepts_dispatch(self):
        from wavecoord import jsonschema_lite
        from wavecoord.launcher.config import config_schema
        self.assertEqual(jsonschema_lite.validate(self.raw(), config_schema()), [])
        bad = self.raw()
        bad["hosts"][0]["dispatch"] = "no"
        self.assertTrue(jsonschema_lite.validate(bad, config_schema()))

    def test_automatic_placement_skips_the_host(self):
        probes = production_probes({n: probe(0, providers={"zai": ZAI}) for n in ("build1", "build2", "build3")})
        L = StubLauncher(self.raw(), probes)
        with self.assertRaises(LauncherError) as e:
            L.select_host({"task": "t1"})
        self.assertNotIn("local", str(e.exception))
        self.assertNotIn("local", L.probed[-1])

    def test_explicit_host_still_works(self):
        L = StubLauncher(self.raw(), production_probes())
        sel = L.select_host({"task": "t1", "host": "local"})
        self.assertEqual((sel["host"], sel["provider"]), ("local", "zai"))

    def test_every_host_off(self):
        raw = self.raw()
        for h in raw["hosts"]:
            h["dispatch"] = False
        L = StubLauncher(raw, production_probes())
        with self.assertRaises(LauncherError) as e:
            L.select_host({"task": "t1"})
        self.assertIn("dispatch: false", str(e.exception))

    def test_lead_candidates_skip_the_host(self):
        cfg = Config(self.raw(), tempfile.gettempdir())
        fake = types.SimpleNamespace(cfg=lambda: cfg)
        self.assertEqual(LeadBase.host_candidates(fake, {"id": "t1"}), ["build1", "build2", "build3"])
        self.assertEqual(LeadBase.host_candidates(fake, {"id": "t1"}, host="local"), ["local"])


class HostsCommandTest(unittest.TestCase):
    def run_hosts(self, raw, probes, as_json=False):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".claude", "wavecoord.json")
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as f:
                json.dump(raw, f)
            saved = client.Launcher.probe
            client.Launcher.probe = lambda self, names=None: {n: dict(probes[n]) for n in (names or sorted(probes))}
            try:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    args = types.SimpleNamespace(config=path, coord_url=None, plugin_dir=None, host=None,
                                                 json=as_json)
                    rc = cli.cmd_hosts(args)
            finally:
                client.Launcher.probe = saved
        self.assertEqual(rc, 0)
        return out.getvalue()

    def test_auth_column_shows_the_provider(self):
        out = self.run_hosts(raw_config(), production_probes())
        rows = {l.split()[0]: l for l in out.splitlines()[1:]}
        for n in ("build1", "build2", "build3"):
            self.assertIn("provider:zai", rows[n])
            self.assertNotIn(" no ", rows[n])
            self.assertTrue(rows[n].rstrip().endswith("ok"), rows[n])
        self.assertIn("provider:zai", rows["local"])

    def test_json_and_dispatch_false(self):
        raw = raw_config(local_extra={"dispatch": False})
        data = json.loads(self.run_hosts(raw, production_probes(), as_json=True))
        self.assertEqual(data["build1"]["auth"], "provider:zai")
        self.assertEqual(data["build1"]["claude"]["auth"], "no")
        self.assertFalse(data["local"]["dispatch"])
        self.assertTrue(data["build1"]["dispatch"])
        out = self.run_hosts(raw, production_probes())
        local = [l for l in out.splitlines() if l.startswith("local")][0]
        self.assertIn("dispatch: false", local)

    def test_missing_file_falls_back_to_the_login_label(self):
        probes = production_probes({"build1": probe(4, providers={"openrouter": ZAI, "zai": MISSING})})
        out = self.run_hosts(raw_config(), probes)
        build1 = [l for l in out.splitlines() if l.startswith("build1")][0]
        self.assertNotIn("provider:zai", build1)
        self.assertIn("missing", build1)


if __name__ == "__main__":
    unittest.main()
