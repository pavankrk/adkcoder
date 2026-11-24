"""Tests for Tier 3 platform features: custom commands, skills, hooks, MCP config, headless CLI, sub-agent explore, fetch_url, sandbox."""

import asyncio
import io
import json
import sys

from adk_coder.cli import main as cli_main
from adk_coder.harness import AgentHarness


def test_custom_commands_and_skills(tmp_path, monkeypatch):
    h = AgentHarness(tmp_path, approve=lambda _: True)

    # Create and execute custom slash command with $ARGUMENTS
    created = asyncio.run(h.execute("/commands new release-check"))
    assert "release-check.md" in created
    (h.state / "commands/release-check.md").write_text(
        "Verify changelog and tests for $ARGUMENTS.", encoding="utf8"
    )
    assert "/release-check" in asyncio.run(h.execute("/commands list"))

    seen_prompts = []

    async def fake_chat(prompt, **kw):
        seen_prompts.append(prompt)
        return "Checked."

    monkeypatch.setattr(h, "chat", fake_chat)
    assert asyncio.run(h.execute("/release-check v1.2.0")) == "Checked."
    assert "Verify changelog and tests for v1.2.0." in seen_prompts[0]

    # Workspace skill discovery & execution
    skill_dir = h.state / "skills/java-perf"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: java-perf\ndescription: Tune JVM GC and allocations\n---\nCheck allocations in hot loops.\n",
        encoding="utf8",
    )
    assert "java-perf" in asyncio.run(h.execute("/skills"))
    assert asyncio.run(h.execute("/skill java-perf review TodoService")) == "Checked."
    assert "Check allocations in hot loops." in seen_prompts[1]
    assert "review TodoService" in seen_prompts[1]


def test_lifecycle_hooks_block_pre_edit_and_run_post_edit(tmp_path):
    h = AgentHarness(tmp_path, approve=lambda _: True)
    asyncio.run(h.execute("/hooks init"))
    marker = tmp_path / "hook_ran.txt"
    h.hooks.config_path.write_text(
        json.dumps(
            {
                "pre_edit": ["test \"$ADK_CODER_FILE\" != \"blocked.txt\""],
                "post_edit": [f"printf '%s' \"$ADK_CODER_FILE\" > {marker}"],
            }
        ),
        encoding="utf8",
    )

    # pre_edit blocks forbidden file
    blocked = h.tools.write_file("blocked.txt", "nope\n")
    assert "Blocked by pre_edit hook" in blocked
    assert not (tmp_path / "blocked.txt").exists()

    # post_edit executes after valid edit
    assert h.tools.write_file("allowed.txt", "ok\n") == "Saved."
    assert marker.read_text(encoding="utf8") == "allowed.txt"


def test_mcp_registry_and_subagent_explore(tmp_path, monkeypatch):
    h = AgentHarness(tmp_path, approve=lambda _: True)

    added = asyncio.run(h.execute("/mcp add github npx -y @modelcontextprotocol/server-github"))
    assert "github" in added
    listed = asyncio.run(h.execute("/mcp list"))
    assert "@modelcontextprotocol/server-github" in listed
    assert "Removed MCP server `github`." == asyncio.run(h.execute("/mcp remove github"))

    # Isolated read-only sub-agent delegation
    seen_tools = []

    async def fake_generate(model, instruction, prompt, tools):
        seen_tools.append([t.__name__ for t in tools])
        return "Found entry point in app.py"

    monkeypatch.setattr("adk_coder.harness.generate", fake_generate)
    summary = asyncio.run(h.execute("/explore Where is the entry point?"))
    assert "app.py" in summary
    assert "read_file" in seen_tools[0]
    assert "write_file" not in seen_tools[0]


def test_headless_json_mode_and_piped_stdin(tmp_path, monkeypatch, capsys):
    (tmp_path / "sample.py").write_text("x = 1\n", encoding="utf8")

    # --json + --yes headless execution
    code = cli_main(["--workspace", str(tmp_path), "--json", "--yes", "/status"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["mode"] == "ask"

    # Piped stdin combined with --print
    seen = []

    async def fake_chat(self, req, **kw):
        seen.append(req)
        return "Looks good"

    monkeypatch.setattr(AgentHarness, "chat", fake_chat)
    monkeypatch.setattr(sys, "stdin", io.StringIO("diff --git a/x b/x\n+print(1)\n"))
    code2 = cli_main(["--workspace", str(tmp_path), "--print", "review this"])
    assert code2 == 0
    assert "Looks good" in capsys.readouterr().out
    assert "diff --git" in seen[0]


def test_fetch_url_ssrf_guard_and_seatbelt_wrap(tmp_path):
    h = AgentHarness(tmp_path, approve=lambda _: True)
    assert "Blocked" in h.tools.fetch_url("http://169.254.169.254/latest/meta-data/")
    assert "Blocked" in h.tools.fetch_url("http://localhost:8080/secret")
    assert "Only valid http" in h.tools.fetch_url("file:///etc/passwd")

    asyncio.run(h.execute("/config sandbox_shell true"))
    assert h.tools.sandbox_shell is True
    wrapped = h.tools._seatbelt_wrap(["echo", "hi"])
    if sys.platform == "darwin":
        assert wrapped[0] == "sandbox-exec"
        assert "(deny network*)" in wrapped[2]
