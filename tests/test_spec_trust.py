"""The trust rules of the spec tree at merge (0.3.0; docs/specs.md, docs/security.md, edit-fence.md section 8.1).

- The fence check (config and spec changes, the Lease: line) runs before any check that runs PR code.
- Verify and Acceptance run on the merge result of the default branch and the head, with a scrubbed
  environment, as one script in which a trailing `# comment` is harmless.
- Verification fails closed: a missing or broken task or requirement record blocks (verify_unavailable).
- A dropped or on_hold sibling is not open: the last merging task runs the requirement's Verify.
- The fence merge mode and spec.verify_at_merge come from the default branch, never the lead's working tree.
- spec.dir is normalized once (specs/source.py, norm_dir).
"""

import json
import os
import subprocess
import tempfile
import unittest

import _paths
from launcher_env import sh
from test_spec_status import IMPL, IMPL_FILE, StatusFlowCase

from wavecoord.lead.refinery import verify_env, verify_script
from wavecoord.specs import source
from wavecoord.specs.ops import apply_ops

REQ = "auth/sessions/token-rotation"
TESTS = REQ + "/rotate-tests"
REQ_FILE = "specs/auth/sessions/token-rotation/_node.md"
MARKER = os.path.join(tempfile.mkdtemp(prefix="wc-trust-mark-"), "verify-ran")


def tree_with(verify, acceptance, sibling=None):
    """The fixture tree with checks for a test repo; `sibling`: the status of rotate-tests (None: planned)."""
    fs = source.read_tree(_paths.FIXTURES, "specs")
    ops = [{"op": "set_status", "id": TESTS, "status": sibling}] if sibling else []
    ops += [{"op": "set_section", "id": REQ, "section": "Verify", "value": verify},
            {"op": "set_section", "id": REQ + "/rotate-impl", "section": "Acceptance", "value": acceptance},
            {"op": "rebase_basis", "task": REQ + "/rotate-impl"},
            {"op": "rebase_basis", "task": TESTS}]
    changes, _, _ = apply_ops(fs, ops)
    fs.update({k: v for k, v in changes.items() if v is not None})
    return fs


class UnitTest(unittest.TestCase):
    def test_verify_script_survives_a_comment(self):
        for cmds, rc in ((["true  # quick unit tests", "true"], 0), (["true # c", "false # d"], 1),
                         (["false  # x", "true"], 1)):
            p = subprocess.run(["sh", "-c", verify_script(cmds)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(p.returncode, rc, (cmds, p.stderr))

    def test_verify_env_is_scrubbed(self):
        env = {"PATH": "/bin", "HOME": "/h", "LANG": "C", "LC_ALL": "C", "TMPDIR": "/t", "TERM": "x",
               "WAVECOORD_TOKEN": "s", "WAVECOORD_TOKEN_FILE": "/f", "GH_TOKEN": "s", "GITHUB_TOKEN": "s",
               "CLAUDE_PLUGIN_OPTION_TOKEN": "s", "SSH_AUTH_SOCK": "/s", "GIT_DIR": "/g", "AWS_SECRET": "s",
               "CARGO_HOME": "/c"}
        self.assertEqual(sorted(verify_env(env)), ["HOME", "LANG", "LC_ALL", "PATH", "TERM", "TMPDIR"])
        # spec.verify_env adds names, never a credential
        self.assertEqual(sorted(verify_env(env, ["CARGO_HOME", "GH_TOKEN", "WAVECOORD_TOKEN"])),
                         ["CARGO_HOME", "HOME", "LANG", "LC_ALL", "PATH", "TERM", "TMPDIR"])

    def test_every_config_key_is_lead_config_or_listed_as_worker_safe(self):
        """Item 8: each key of config.schema.json is in gatecheck.CONFIG_KEYS (a task PR may not change it) or
        in WORKER_KEYS; each host key in HOST_KEYS or WORKER_HOST_KEYS. A new key must be sorted in."""
        from wavecoord import gatecheck
        with open(os.path.join(_paths.SCHEMAS, "config.schema.json")) as f:
            schema = json.load(f)
        top = set(schema["properties"]) - {"hosts"}
        self.assertEqual(top, set(gatecheck.CONFIG_KEYS) | set(gatecheck.WORKER_KEYS))
        self.assertFalse(set(gatecheck.CONFIG_KEYS) & set(gatecheck.WORKER_KEYS))
        host = set(schema["properties"]["hosts"]["items"]["properties"])
        self.assertEqual(host, set(gatecheck.HOST_KEYS) | set(gatecheck.WORKER_HOST_KEYS))
        for k in ("pr_cmd", "pr_state_cmd", "coord_url", "log_branch", "default_provider", "auto_resolve",
                  "branch_prefix"):
            self.assertIn(k, gatecheck.CONFIG_KEYS)
        for k in ("prepare_cmd", "ssh", "repo_path", "providers", "dispatch", "labels"):
            self.assertIn(k, gatecheck.HOST_KEYS)

    def test_norm_dir(self):
        for raw, want in (("specs", "specs"), ("./specs", "specs"), ("specs/.", "specs"), ("specs/", "specs"),
                          ("docs//specs", "docs/specs"), ("docs/./specs/", "docs/specs"), (None, "specs"),
                          ("", "specs"), (".", "specs"), ("/abs", "specs"), ("../x", "specs"), ("a/../..", "specs")):
            self.assertEqual(source.norm_dir(raw), want, raw)

    def test_read_ref_ids_with_a_non_canonical_dir(self):
        repo = tempfile.mkdtemp(prefix="wc-trust-")
        files = source.read_tree(_paths.FIXTURES, "specs")
        for rel, text in files.items():
            p = os.path.join(repo, "docs", "specs", rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(text)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@example.invalid")
        for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "s"]):
            subprocess.run(["git"] + args, cwd=repo, env=env, check=True, stdout=subprocess.PIPE)
        for d in ("docs/specs", "./docs/specs", "docs//specs", "docs/specs/.", "docs/specs/"):
            self.assertEqual(sorted(source.read_ref(repo, "HEAD", d)), sorted(files), d)
            self.assertEqual(sorted(source.read_tree(repo, d)), sorted(files), d)


class TrustCase(StatusFlowCase):
    VERIFY = ["true"]
    ACCEPT = ["true"]
    SIBLING = "dropped"  # rotate-impl is the requirement's last open task

    def setUp(self):
        self.FILES = tree_with(self.VERIFY, self.ACCEPT, self.SIBLING)
        super(StatusFlowCase, self).setUp()

    def run_impl(self, fname="src/auth/rotate.py"):
        os.environ.update(FAKE_CLAUDE_COMMIT="1", FAKE_CLAUDE_COMMIT_FILE=fname, FAKE_CLAUDE_COMMIT_TEXT="rotate\n")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FAKE_CLAUDE_COMMIT", "FAKE_CLAUDE_COMMIT_FILE",
                                                                   "FAKE_CLAUDE_COMMIT_TEXT")])
        d = self.lead.dispatch(IMPL)
        self.collect_until(d["id"])
        return d

    def push_main_files(self, files, msg="main"):
        sh(["git", "pull", "-q", "origin", "main"], self.seed)
        for rel, text in files.items():
            p = os.path.join(self.seed, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(text)
        sh(["git", "add", "-A"], self.seed)
        sh(["git", "commit", "-q", "-m", msg], self.seed)
        sh(["git", "push", "-q", "origin", "main"], self.seed)

    def push_main_cfg(self, **keys):
        with open(os.path.join(self.repo, ".claude", "wavecoord.json")) as f:
            cfg = json.load(f)
        cfg.update(keys)
        self.push_main_files({".claude/wavecoord.json": json.dumps(cfg)}, "config")

    def merge_one(self):
        out = self.lead.refinery_merge()
        self.assertEqual(len(out["results"]), 1, out)
        return out["results"][0]

    def merges(self):
        return [c for c in self.gh()["calls"] if c[:2] == ["pr", "merge"]]


class FenceBeforeVerifyTest(TrustCase):
    VERIFY = ["touch %s" % MARKER]
    ACCEPT = ["touch %s" % MARKER]

    def test_a_spec_change_is_refused_before_any_pr_code_runs(self):
        """Item 9: the fence/spec check comes first; Verify (worker code on the lead) never runs for it."""
        if os.path.exists(MARKER):
            os.remove(MARKER)
        self.run_impl(fname="specs/auth/sessions/token-rotation/notes.md")
        r = self.merge_one()
        self.assertEqual((r["state"], r["reason"]), ("blocked", "spec_change"), r)
        self.assertFalse(os.path.exists(MARKER), "Verify ran before the spec change was refused")
        self.assertEqual(self.merges(), [])


class LeaseLineTest(TrustCase):
    def edit_body(self, fn):
        st = self.gh()
        st["prs"][0]["body"] = fn(st["prs"][0]["body"])
        with open(self.gh_state, "w") as f:
            json.dump(st, f)

    def test_two_lease_lines_do_not_skip_the_config_check(self):
        """Item 7: a summary line `Lease: ...` makes two; the config change is still refused."""
        self.run_impl(fname=".claude/settings.json")
        epoch = self.core.state.task_lease(IMPL)["epoch"]
        self.edit_body(lambda b: b + "\n\nLease: %s@%d\n" % (IMPL, epoch))
        r = self.merge_one()
        self.assertEqual((r["state"], r["reason"]), ("blocked", "config_change"), r)

    def test_no_lease_line_does_not_skip_the_spec_check_and_is_never_merged(self):
        self.run_impl(fname="specs/auth/notes.md")
        self.edit_body(lambda b: "\n".join(l for l in b.split("\n") if not l.startswith("Lease:")))
        r = self.merge_one()
        self.assertEqual((r["state"], r["reason"]), ("blocked", "spec_change"), r)

    def test_no_lease_line_is_never_merged(self):
        self.run_impl()
        self.edit_body(lambda b: "\n".join(l for l in b.split("\n") if not l.startswith("Lease:")))
        r = self.merge_one()
        self.assertEqual((r["state"], r["reason"]), ("blocked", "stale_lease"), r)
        self.assertEqual(self.merges(), [])


class MergeResultTest(TrustCase):
    VERIFY = ["test -f sibling.txt  # the sibling's merged work", "test -z \"$WAVECOORD_TOKEN$GH_TOKEN\"",
              "test \"$EXTRA_OK\" = 1"]
    ACCEPT = ["test -f src/auth/rotate.py  # this task's file"]

    def test_verify_sees_the_merge_result_with_a_scrubbed_env(self):
        """Items 36, 9 and 84: sibling work merged after this branch started is in the checked tree; no lead
        credential reaches Verify; spec.verify_env adds a name; trailing comments are harmless."""
        self.run_impl()
        self.push_main_files({"sibling.txt": "x\n"}, "sibling")
        self.push_main_cfg(spec={"status_flush_s": 0, "verify_env": ["EXTRA_OK"]})
        for k, v in (("WAVECOORD_TOKEN", "lead-secret"), ("GH_TOKEN", "gh-secret"), ("EXTRA_OK", "1")):
            self.addCleanup(self._restore, k, os.environ.get(k))
            os.environ[k] = v
        r = self.merge_one()
        self.assertEqual(r["state"], "merged", r)
        spec = self.events("refinery.merge")[-1]["payload"]["spec"]
        self.assertEqual((spec["verified"], spec["commands"]), (True, 4))


class FailClosedTest(TrustCase):
    def test_a_broken_task_record_blocks(self):
        """Item 83: a task record that does not parse at merge blocks; it does not skip verification."""
        self.run_impl()
        self.push_main_files({IMPL_FILE: "---\nstatus: [\n---\n"}, "break the task")
        r = self.merge_one()
        self.assertEqual((r["state"], r["reason"]), ("blocked", "verify_unavailable"), r)
        self.assertEqual(self.merges(), [])

    def test_a_broken_requirement_record_blocks(self):
        self.run_impl()
        self.push_main_files({REQ_FILE: "---\nstatus: [\n---\n"}, "break the requirement")
        r = self.merge_one()
        self.assertEqual((r["state"], r["reason"]), ("blocked", "verify_unavailable"), r)


class OnHoldSiblingTest(TrustCase):
    VERIFY = ["false"]
    SIBLING = "on_hold"

    def test_an_on_hold_sibling_does_not_defer_verify(self):
        """Item 82: with the other task on hold, this merge is the requirement's last: its Verify runs."""
        self.run_impl()
        r = self.merge_one()
        self.assertEqual((r["state"], r["reason"]), ("blocked", "verify_failed"), r)


class AuthorityTest(TrustCase):
    VERIFY = ["false"]

    def test_verify_at_merge_comes_from_the_default_branch(self):
        """Item 35: verify_at_merge false in the lead's working tree does not turn Verify off."""
        self.run_impl()
        cfgp = os.path.join(self.repo, ".claude", "wavecoord.json")
        with open(cfgp) as f:
            cfg = json.load(f)
        cfg["spec"] = {"status_flush_s": 0, "verify_at_merge": False}
        cfg["fence"] = "off"
        with open(cfgp, "w") as f:
            json.dump(cfg, f)
        os.utime(cfgp, (2, 2))
        r = self.merge_one()
        self.assertEqual((r["state"], r["reason"]), ("blocked", "verify_failed"), r)
        self.push_main_cfg(spec={"status_flush_s": 0, "verify_at_merge": False})
        r = self.merge_one()
        self.assertEqual(r["state"], "merged", r)


if __name__ == "__main__":
    unittest.main()
