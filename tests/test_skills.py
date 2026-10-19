"""Plugin skills and agents: frontmatter, tool names, links, size; the manifest; `claude plugin validate`."""

import json
import os
import re
import shutil
import subprocess
import unittest

import _paths

PLUGIN = os.path.join(_paths.ROOT, "plugin")
SKILLS = os.path.join(PLUGIN, "skills")
AGENTS = os.path.join(PLUGIN, "agents")
TOOL_PREFIX = "mcp__plugin_wavecoord_wavecoord__"
EXPECTED_SKILLS = {"coord-status", "wave-plan", "wave-run", "triage", "revise", "resolve"}
EXPECTED_AGENTS = {"planner", "reviewer", "steerer"}
BUILTIN_TOOLS = {"Read", "Grep", "Glob", "Monitor", "Bash", "Edit", "Write", "Agent", "WebFetch"}
PLUGIN_AGENT_IGNORED = ("hooks", "mcpServers", "permissionMode", "initialPrompt")
SKILL_KEYS = {"name", "description", "when_to_use", "argument-hint", "arguments", "disable-model-invocation",
              "user-invocable", "allowed-tools", "disallowed-tools", "model", "effort", "context", "agent",
              "background", "hooks", "paths", "shell", "metadata", "license", "compatibility"}
AGENT_KEYS = {"name", "description", "tools", "disallowedTools", "model", "permissionMode", "maxTurns",
              "autoCompactWindow", "skills", "mcpServers", "hooks", "memory", "background", "omitClaudeMd",
              "effort", "isolation", "color", "initialPrompt", "experimental"}


def parse_frontmatter(text):
    """A strict reader for the YAML subset the plugin uses: `key: scalar` and `key:` + `  - item` lists.

    Raises ValueError on anything else, so a construct that real YAML would read differently fails here.
    """
    lines = text.split("\n")
    if lines[0] != "---":
        raise ValueError("the first line is not ---")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise ValueError("no closing ---")
    out, key = {}, None
    for ln in lines[1:end]:
        if not ln.strip():
            continue
        m = re.match(r"^  - (.+)$", ln)
        if m:
            if key is None or not isinstance(out[key], list):
                raise ValueError("list item without a list key: %r" % ln)
            out[key].append(scalar(m.group(1)))
            continue
        m = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):(?: (.*))?$", ln)
        if not m:
            raise ValueError("not a key line: %r" % ln)
        key = m.group(1)
        if key in out:
            raise ValueError("duplicate key %s" % key)
        out[key] = scalar(m.group(2)) if m.group(2) is not None else []
    return out, "\n".join(lines[end + 1:])


def scalar(v):
    v = v.strip()
    if v.startswith('"'):
        return json.loads(v)  # double-quoted YAML is a superset; ours is JSON-compatible
    if v.startswith(("'", "[", "{", "&", "*", "!", "|", ">", "%", "@", "`")):
        raise ValueError("unsupported scalar %r" % v)
    if ": " in v or " #" in v:
        raise ValueError("plain scalar with ': ' or ' #' (quote it): %r" % v)
    return {"true": True, "false": False}.get(v, v)


def contract_tools():
    with open(os.path.join(_paths.ROOT, "docs", "contract.md"), encoding="utf-8") as f:
        text = f.read()
    sec = text.split("## 8. MCP and the channel", 1)[1].split("**Channel.**", 1)[0]
    return set(re.findall(r"^\| `([a-z_]+)` \|", sec, re.M))


def skill_dirs():
    return sorted(d for d in os.listdir(SKILLS) if os.path.isfile(os.path.join(SKILLS, d, "SKILL.md")))


def agent_files():
    return sorted(f for f in os.listdir(AGENTS) if f.endswith(".md"))


class SkillTest(unittest.TestCase):
    def test_expected_skills(self):
        self.assertEqual(set(skill_dirs()), EXPECTED_SKILLS)

    def test_frontmatter_and_tools(self):
        tools = contract_tools()
        self.assertIn("dispatch_wave", tools)
        for d in skill_dirs():
            with self.subTest(skill=d):
                with open(os.path.join(SKILLS, d, "SKILL.md"), encoding="utf-8") as f:
                    text = f.read()
                fm, body = parse_frontmatter(text)
                self.assertEqual(set(fm) - SKILL_KEYS, set())
                self.assertEqual(fm.get("name"), d)
                desc = fm.get("description")
                self.assertIsInstance(desc, str)
                self.assertGreater(len(desc), 40)
                self.assertLessEqual(len(desc) + len(fm.get("when_to_use", "")), 1536)
                self.assertLess(len(text.splitlines()), 500)
                allowed = fm.get("allowed-tools", [])
                self.assertIsInstance(allowed, list)
                self.assertTrue(allowed)
                for t in allowed:
                    if t.startswith(TOOL_PREFIX):
                        self.assertIn(t[len(TOOL_PREFIX):], tools, t)
                    elif t.startswith("Bash("):
                        self.assertRegex(t, r"^Bash\([a-z][a-z-]*( [a-z][a-z-]*)*( \*)?\)$")
                    else:
                        self.assertIn(t, BUILTIN_TOOLS)
                # every wavecoord tool the body names in backticks is pre-allowed
                named = set(re.findall(r"`(%s)`" % "|".join(sorted(tools)), body))
                allowed_names = {t[len(TOOL_PREFIX):] for t in allowed if t.startswith(TOOL_PREFIX)}
                self.assertEqual(named - allowed_names, set(), d)
                self.assertIn("never", body.lower())
                self.assertRegex(body, r"consent")

    def test_relative_links_exist(self):
        for d in skill_dirs():
            base = os.path.join(SKILLS, d)
            for name in os.listdir(base):
                if not name.endswith(".md"):
                    continue
                with open(os.path.join(base, name), encoding="utf-8") as f:
                    text = f.read()
                self.assertLess(len(text.splitlines()), 500, name)
                for link in re.findall(r"\]\(([^)#:]+\.md)\)", text):
                    with self.subTest(skill=d, file=name, link=link):
                        self.assertTrue(os.path.isfile(os.path.join(base, link)))

    def test_monitor_command_and_loop_documented(self):
        with open(os.path.join(SKILLS, "wave-run", "SKILL.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("wavecoord events --follow --for lead", text)
        self.assertIn("/loop 10m /wavecoord:triage", text)
        with open(os.path.join(SKILLS, "wave-plan", "llm-risks.md"), encoding="utf-8") as f:
            risks = f.read().lower()
        for r in ("context overflow", "long builds", "flaky tests", "ambiguous spec", "protected paths",
                  "large or generated files", "rate limits", "non-anthropic"):
            self.assertIn(r, risks)


class AgentTest(unittest.TestCase):
    def test_agents(self):
        self.assertEqual({f[:-3] for f in agent_files()}, EXPECTED_AGENTS)
        tools = contract_tools()
        for f in agent_files():
            with self.subTest(agent=f):
                with open(os.path.join(AGENTS, f), encoding="utf-8") as fh:
                    fm, body = parse_frontmatter(fh.read())
                self.assertEqual(set(fm) - AGENT_KEYS, set())
                self.assertEqual(fm["name"], f[:-3])
                self.assertGreater(len(fm["description"]), 40)
                for k in PLUGIN_AGENT_IGNORED:
                    self.assertNotIn(k, fm, "plugin agents ignore %s" % k)
                listed = [t.strip() for t in fm["tools"].split(",")]
                for t in listed:
                    if t.startswith(TOOL_PREFIX):
                        self.assertIn(t[len(TOOL_PREFIX):], {"plan_get", "plan_graph", "config", "attempt_get"}, t)
                    else:
                        self.assertIn(t, {"Read", "Grep", "Glob"}, "read-only tools only")
                self.assertIn("data", body)


class ManifestTest(unittest.TestCase):
    def test_channel_binds_a_declared_server(self):
        with open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            m = json.load(f)
        servers = dict(m.get("mcpServers") or {})
        mcp_json = os.path.join(PLUGIN, ".mcp.json")
        if os.path.isfile(mcp_json):
            with open(mcp_json, encoding="utf-8") as f:
                servers.update(json.load(f).get("mcpServers", {}))
        self.assertEqual([c["server"] for c in m["channels"]], ["lead-feed"])
        feed = servers["lead-feed"]
        self.assertEqual(feed["args"], ["channel"])
        for ref in re.findall(r"\$\{user_config\.([a-z_]+)\}", json.dumps(m)):
            self.assertIn(ref, m["userConfig"])
        self.assertEqual(m["userConfig"]["lead"]["type"], "boolean")

    @unittest.skipUnless(shutil.which("claude"), "claude CLI not installed")
    def test_claude_plugin_validate_strict(self):
        for target in ("plugin", "."):
            p = subprocess.run(["claude", "plugin", "validate", target, "--strict"], cwd=_paths.ROOT,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                               timeout=120)
            self.assertEqual(p.returncode, 0, p.stdout)
            self.assertIn("Validation passed", p.stdout)


class ParserTest(unittest.TestCase):
    def test_rejects_ambiguous_yaml(self):
        for bad in ("---\nname: a: b\n---\n", "---\n- x\n---\n", "---\nname: [a]\n---\n", "no frontmatter",
                    "---\nk: v\nk: w\n---\n"):
            with self.assertRaises(ValueError):
                parse_frontmatter(bad)
        fm, _ = parse_frontmatter('---\nname: x\nlist:\n  - a\n  - "b: c"\nflag: true\n---\nbody')
        self.assertEqual(fm, {"name": "x", "list": ["a", "b: c"], "flag": True})


if __name__ == "__main__":
    unittest.main()
