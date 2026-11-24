"""Tests for Tier 1 Claude/Codex features: AGENTS.md, /mode, /plan, !cmd, /undo-edit, /commit, SSE streaming."""

import asyncio
import subprocess

from adk_coder.harness import AgentHarness


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root)


def test_agents_md_auto_loaded_into_chat_instruction(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text(
        "Always use Java 21 records and Clean Architecture.\n",
        encoding="utf8",
    )
    h = AgentHarness(tmp_path, approve=lambda _: True)

    seen_instructions = []

    async def fake_generate(model, instruction, prompt, tools):
        seen_instructions.append(instruction)
        return "Understood"

    monkeypatch.setattr("adk_coder.harness.generate", fake_generate)
    assert asyncio.run(h.execute("hello")) == "Understood"
    assert "Always use Java 21 records and Clean Architecture." in seen_instructions[0]


def test_execution_modes_and_plan_mode(tmp_path, monkeypatch):
    prompts_asked = []

    def deny_all(msg):
        prompts_asked.append(msg)
        return False

    h = AgentHarness(tmp_path, approve=deny_all)

    # plan mode blocks writes even without prompting and exposes read-only tools
    assert "plan" in asyncio.run(h.execute("/mode plan"))
    assert h.tools.write_file("a.txt", "hi") == "Edit declined."
    assert prompts_asked == []
    tool_names = [fn.__name__ for fn in h._agent_tools()]
    assert "read_file" in tool_names
    assert "write_file" not in tool_names
    assert "run_shell_command" not in tool_names

    # auto-edit approves edits automatically without prompting, still asks for shell
    assert "auto-edit" in asyncio.run(h.execute("/mode auto-edit"))
    assert h.tools.write_file("a.txt", "hi") == "Saved."
    assert prompts_asked == []
    assert h.tools.run_shell_command("echo hi") == "Command declined."
    assert len(prompts_asked) == 1

    # full-auto approves both edits and shell commands without prompting
    assert "full-auto" in asyncio.run(h.execute("/mode full-auto"))
    assert h.tools.run_shell_command("printf ok").startswith("Exit 0")

    # cycle_mode rotates cleanly
    assert h.cycle_mode() == "ask"

    # /plan runs read-only pass and writes .agent/PLAN.md
    seen_tools = []

    async def fake_generate(model, instruction, prompt, tools):
        seen_tools.append([t.__name__ for t in tools])
        return "1. Define domain\n2. Add tests"

    monkeypatch.setattr("adk_coder.harness.generate", fake_generate)
    out = asyncio.run(h.execute("/plan Add OAuth2 login"))
    assert "Define domain" in out
    assert "Define domain" in h.read("PLAN.md")
    assert "write_file" not in seen_tools[0]
    assert h.mode == "ask"  # restored after /plan


def test_shell_escape_and_undo_edit_non_git(tmp_path):
    h = AgentHarness(tmp_path, approve=lambda _: True)

    # !cmd executes immediately and appends output into context
    out = asyncio.run(h.execute("!printf 'hello-shell'"))
    assert out.startswith("Exit 0")
    assert "hello-shell" in out
    assert "hello-shell" in h.read("context_summary.md")

    # /undo-edit works cleanly outside Git for create, edit, and delete
    assert h.tools.write_file("demo.txt", "v1\n") == "Saved."
    assert h.tools.apply_diff("demo.txt", "v1", "v2") == "Saved."
    assert (tmp_path / "demo.txt").read_text(encoding="utf8") == "v2\n"

    assert "restored demo.txt" in asyncio.run(h.execute("/undo-edit"))
    assert (tmp_path / "demo.txt").read_text(encoding="utf8") == "v1\n"

    assert "removed demo.txt" in asyncio.run(h.execute("/undo-edit"))
    assert not (tmp_path / "demo.txt").exists()


def test_smart_commit_drafts_conventional_message(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.org")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf8")
    git(tmp_path, "add", "app.py")
    git(tmp_path, "commit", "-qm", "chore: init")

    (tmp_path / "app.py").write_text("x = 2\n", encoding="utf8")
    h = AgentHarness(tmp_path, approve=lambda _: True)

    async def fake_ask(role, prompt):
        return "feat(core): update x value to 2"

    monkeypatch.setattr(h, "ask", fake_ask)
    asyncio.run(h.execute("/commit"))
    last_msg = git(tmp_path, "log", "-1", "--format=%s").decode().strip()
    assert last_msg == "feat(core): update x value to 2"


def test_sse_streaming_and_thought_callbacks(tmp_path):
    from google.adk.agents import Agent
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    from adk_coder.model_factory import run_agent

    class StreamingFixtureModel(BaseLlm):
        async def generate_content_async(self, llm_request, stream=False):
            yield LlmResponse(
                partial=True,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="thinking...", thought=True)],
                ),
            )
            yield LlmResponse(
                partial=True,
                content=types.Content(role="model", parts=[types.Part(text="Hello ")]),
            )
            yield LlmResponse(
                partial=True,
                content=types.Content(role="model", parts=[types.Part(text="world!")]),
            )
            yield LlmResponse(
                partial=False,
                content=types.Content(
                    role="model", parts=[types.Part(text="Hello world!")]
                ),
            )

    agent = Agent(
        name="stream_test",
        model=StreamingFixtureModel(model="fixture"),
        instruction="Reply briefly.",
    )
    thoughts, chunks = [], []
    result = asyncio.run(
        run_agent(
            agent,
            "Hi",
            on_text=chunks.append,
            on_thought=thoughts.append,
        )
    )
    assert result == "Hello world!"
    assert thoughts == ["thinking..."]
    assert chunks == ["Hello ", "world!"]
