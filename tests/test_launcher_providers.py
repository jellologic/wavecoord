"""Provider profiles: env files on the host, loaded into the claude child only (docs/providers.md).

The tokens here are test strings, not secrets."""

import json
import os
import stat
import tempfile
import unittest

import _paths
from launcher_env import LauncherTestCase

from wavecoord import jsonschema_lite
from wavecoord.launcher import client, providers
from wavecoord.launcher.common import LauncherError
from wavecoord.launcher.transport import Host

PROFILE_TOKEN = "test-provider-token-0000"
PARENT_TOKEN = "parent-token-1111"


def write_env(path, text, mode=0o600):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)
    return path


def api_schema():
    with open(os.path.join(_paths.SCHEMAS, "api.schema.json")) as f:
        return json.load(f)


class EnvFileTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_parse(self):
        p = write_env(os.path.join(self.d, "p.env"),
                      "# comment\n\nexport ANTHROPIC_BASE_URL='https://provider.invalid/api'\n"
                      "ANTHROPIC_AUTH_TOKEN=\"tok en\"\nANTHROPIC_API_KEY=''\nAPI_TIMEOUT_MS=3000000\n")
        self.assertEqual(providers.parse_env_file(p), {
            "ANTHROPIC_BASE_URL": "https://provider.invalid/api", "ANTHROPIC_AUTH_TOKEN": "tok en",
            "ANTHROPIC_API_KEY": "", "API_TIMEOUT_MS": "3000000"})

    def test_parse_errors_never_show_the_value(self):
        for text in ("NOVALUE\n", "BAD KEY=x\n", "K=a b\n", "K='unclosed\n"):
            p = write_env(os.path.join(self.d, "bad.env"), "SECRETISH=" + "s3cr3t-value\n" + text)
            with self.assertRaises(providers.ProviderError) as cm:
                providers.parse_env_file(p)
            self.assertNotIn("s3cr3t-value", str(cm.exception))
            self.assertIn("line 2", str(cm.exception))

    def test_check_mode_and_missing(self):
        p = write_env(os.path.join(self.d, "ok.env"), "A=1\n")
        self.assertIsNone(providers.check_env_file(p))
        os.chmod(p, 0o400)
        self.assertIsNone(providers.check_env_file(p))
        os.chmod(p, 0o644)
        self.assertIn("must be 600", providers.check_env_file(p))
        self.assertIn("missing", providers.check_env_file(os.path.join(self.d, "nope.env")))

    def test_probe_uses_stat_only(self):
        p = write_env(os.path.join(self.d, "locked.env"), "A=1\n", mode=0o000)  # unreadable, still "present"
        res = providers.probe({"x": {"env_file": p}, "y": {"env_file": p + ".missing"}, "anthropic": None})
        self.assertTrue(res["x"]["present"])
        self.assertFalse(res["y"]["present"])
        self.assertTrue(res["anthropic"]["builtin"])
        os.chmod(p, 0o600)

    def test_apply_strips_parent_and_sets_models(self):
        p = write_env(os.path.join(self.d, "p.env"), "ANTHROPIC_BASE_URL=https://provider.invalid\n"
                                                     "ANTHROPIC_AUTH_TOKEN=%s\n" % PROFILE_TOKEN)
        parent = {"PATH": "/bin", "ANTHROPIC_AUTH_TOKEN": PARENT_TOKEN, "ANTHROPIC_MODEL": "x",
                  "CLAUDE_CODE_OAUTH_TOKEN": PARENT_TOKEN, "CLAUDE_CODE_USE_BEDROCK": "1"}
        env, names = providers.apply(parent, "glm", {"env_file": p, "models": {"opus": "m-big", "subagent": "m-small"}})
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], PROFILE_TOKEN)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "")
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "m-big")
        self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "m-small")
        for k in ("ANTHROPIC_MODEL", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK"):
            self.assertNotIn(k, env)
        self.assertEqual(env["PATH"], "/bin")
        self.assertNotIn(PROFILE_TOKEN, " ".join(names))
        same, names = providers.apply(parent, "anthropic", None)
        self.assertEqual((same, names), (parent, []))

    def test_cost_basis(self):
        self.assertEqual(providers.cost_basis("anthropic", None), "anthropic")
        self.assertEqual(providers.cost_basis("glm", {"env_file": "x"}), "anthropic_estimate")
        self.assertEqual(providers.cost_basis("glm", {"env_file": "x", "cost_basis": "none"}), "none")


class PlacementByProviderTest(unittest.TestCase):
    def probe(self, auth="no", prov=None):
        return {"ok": True, "free": 1, "slots": 1, "active": 0, "repo_ok": True, "mem_avail_gib": 1,
                "claude": {"installed": True, "auth": auth}, "providers": prov or {}}

    def test_provider_needs_present_file_not_login(self):
        probes = {"a": self.probe(auth="no", prov={"glm": {"present": True}}),
                  "b": self.probe(auth="logged-in", prov={"glm": {"present": False, "why": "env file x is missing"}}),
                  "c": self.probe(auth="logged-in")}
        order, why = client.place(probes, "glm")
        self.assertEqual(order, ["a"])
        self.assertIn("missing", why["b"])
        self.assertIn("not on this host", why["c"])
        order, why = client.place(probes, "anthropic")
        self.assertEqual(order, ["b", "c"])
        self.assertIn("not logged in", why["a"])


class ProviderEndToEnd(LauncherTestCase):
    def setUp(self):
        super(ProviderEndToEnd, self).setUp()
        self.env_file = os.path.join(self.tmp, "glmtest.env")
        write_env(self.env_file, "ANTHROPIC_BASE_URL='https://provider.invalid/api/anthropic'\n"
                                 "ANTHROPIC_AUTH_TOKEN='%s'\nAPI_TIMEOUT_MS=3000000\n" % PROFILE_TOKEN)
        # a parent session's provider variables must not reach the worker
        os.environ.update({"ANTHROPIC_BASE_URL": "https://parent.invalid", "ANTHROPIC_AUTH_TOKEN": PARENT_TOKEN,
                           "CLAUDE_CODE_OAUTH_TOKEN": PARENT_TOKEN})

    def provider_launcher(self, **host_extra):
        host = {"name": "local", "slots": 2, "repo_path": self.repo}
        host.update(host_extra)
        return self.launcher(hosts=[host], providers={
            "glmtest": {"env_file": self.env_file, "models": {"opus": "glm-test-big", "haiku": "glm-test-flash",
                                                              "subagent": "glm-test-flash"}, "cost_basis": "none"},
            "spare": {"env_file": self.env_file}}, default_provider="glmtest")

    def test_profile_env_reaches_claude_only(self):
        expect = os.path.join(self.tmp, "expect.json")
        with open(expect, "w") as f:
            json.dump({"ANTHROPIC_BASE_URL": "https://provider.invalid/api/anthropic",
                       "ANTHROPIC_AUTH_TOKEN": PROFILE_TOKEN, "ANTHROPIC_API_KEY": "",
                       "API_TIMEOUT_MS": "3000000", "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-test-big",
                       "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-test-flash", "CLAUDE_CODE_SUBAGENT_MODEL": "glm-test-flash",
                       "CLAUDE_CODE_OAUTH_TOKEN": None}, f)
        os.environ["FAKE_CLAUDE_EXPECT_ENV"] = expect
        os.environ["FAKE_CLAUDE_COMMIT"] = "1"
        noise = '[claude-code:unrecognized_model] {"model":"glm-test-big","note":"pricing unknown"}'
        os.environ["FAKE_CLAUDE_STDERR"] = noise
        prep_env = os.path.join(self.tmp, "prep-env.txt")
        L = self.provider_launcher(prepare_cmd="env > %s" % prep_env)
        rec = L.launch(self.opts(model="opus"))
        self.assertEqual(rec["provider"], "glmtest")
        r = self.wait_state(L, "t1-1", ("reported", "failed", "timed_out"))
        self.assertEqual(r["state"], "reported", r)  # the fake exits 3 on a wrong env
        self.assertEqual((r["provider"], r["cost_basis"]), ("glmtest", "none"))
        self.assertTrue(r["usage"])
        self.assertTrue(r["model_usage"])
        self.assertFalse(r["throttled"])

        d = r["state_dir"]
        with open(os.path.join(d, "stderr.log")) as f:
            self.assertIn("unrecognized_model", f.read())
        with open(os.path.join(d, "stream.jsonl")) as f:
            self.assertNotIn("unrecognized_model", f.read())
        # the secret is in no log, state file or coordinator call, and not in prepare_cmd's env
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                with open(p, errors="replace") as f:
                    self.assertNotIn(PROFILE_TOKEN, f.read(), name)
        self.assertNotIn(PROFILE_TOKEN, json.dumps(self.coord.requests))
        with open(prep_env) as f:
            self.assertNotIn(PROFILE_TOKEN, f.read())

        body = self.coord.posts("/api/attempts/t1/1/report")[0]["body"]
        self.assertEqual((body["provider"], body["cost_basis"]), ("glmtest", "none"))
        self.assertIn("provider `glmtest`", self.prs()[0]["body"])
        rows = L.ls()["local"]
        self.assertEqual(rows[0]["provider"], "glmtest")

    def test_refuses_mode_0644(self):
        os.chmod(self.env_file, 0o644)
        L = self.provider_launcher()
        with self.assertRaises(LauncherError) as cm:
            L.launch(self.opts())
        self.assertIn("must be 600", str(cm.exception))
        self.assertNotIn("t1-1", [r["id"] for rs in L.ls().values() for r in rs])

    def test_refuses_missing_file(self):
        os.unlink(self.env_file)
        L = self.provider_launcher()
        with self.assertRaises(LauncherError) as cm:
            L.launch(self.opts())
        self.assertIn("missing", str(cm.exception))

    def test_host_side_refusal(self):
        os.chmod(self.env_file, 0o644)
        L = self.provider_launcher()
        spec = {"task": "t1", "attempt": 1, "attempt_id": "t1-1", "branch": "wave/t1-1", "agent": "worker-x",
                "session_id": "5f0c1e9a-3b7d-4c2e-9a61-0d8e2f4b7c10", "prompt": "x", "provider": "glmtest",
                "provider_profile": {"env_file": self.env_file}}
        out = Host(L.cfg.hosts["local"]).call("launch", dict(L.base_payload(), spec=spec))
        self.assertFalse(out["ok"])
        self.assertIn("refusing to start", out["error"])
        self.assertIn("must be 600", out["error"])

    def test_host_without_profile_and_unknown_provider(self):
        L = self.provider_launcher(providers=["spare"])
        with self.assertRaises(LauncherError) as cm:
            L.launch(self.opts())
        self.assertIn("no host has provider glmtest", str(cm.exception))
        with self.assertRaises(LauncherError) as cm:
            L.launch(self.opts(provider="nope"))
        self.assertIn("unknown provider", str(cm.exception))

    def test_throttle_recorded_and_next_provider(self):
        ev = os.path.join(self.tmp, "events.jsonl")
        with open(ev, "w") as f:
            f.write(json.dumps({"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 10,
                                "retry_delay_ms": 500, "error_status": 429, "error": "rate_limit"}) + "\n")
            f.write(json.dumps({"type": "system", "subtype": "hook_started", "hook_event": "StopFailure",
                                "hook_name": "StopFailure"}) + "\n")
        os.environ["FAKE_CLAUDE_EVENTS"] = ev
        L = self.provider_launcher()
        L.launch(self.opts(provider_fallback=["spare", "anthropic"]))
        r = self.wait_state(L, "t1-1", ("reported", "failed", "timed_out"))
        self.assertTrue(r["throttled"])
        self.assertEqual(sorted(r["throttle"]), ["api_retry:1", "stop_failure"])
        self.assertEqual(r["next_provider"], "spare")
        body = self.coord.posts("/api/attempts/t1/1/report")[0]["body"]
        self.assertEqual((body["throttled"], body["next_provider"]), (True, "spare"))
        notes = self.coord.posts("/api/notify")
        self.assertTrue(notes)
        schema = api_schema()
        for n in notes:
            self.assertEqual(jsonschema_lite.validate(n["body"], schema, "#/definitions/notify_request"), [], n["body"])
            self.assertEqual(n["body"]["kind"], "rate_limit")
            self.assertEqual(n["body"]["detail"]["provider"], "glmtest")
            self.assertTrue(n["headers"].get("Idempotency-Key"))


if __name__ == "__main__":
    unittest.main()
