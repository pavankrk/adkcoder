"""Tests for CLI UX, token capacities, completer, prompt theme, diff viewer, and notifier."""

from prompt_toolkit.document import Document

from adk_coder.cli import build_completer, normalize_cli_request
from adk_coder.diff_viewer import preview
from adk_coder.harness import AgentHarness
from adk_coder.notifier import build_command
from adk_coder.theme import bottom_toolbar, prompt
from adk_coder.token_manager import TokenTracker, capacity, needs_compaction


def test_token_manager_model_windows_and_tracker():
    assert capacity("gemini-2.5-flash") == 1_000_000
    assert capacity("openai/gpt-4o-mini") == 128_000
    assert capacity("anthropic/claude-sonnet-4-20250514") == 200_000
    assert capacity("ollama_chat/qwen2.5-coder:14b") == 32_768
    assert capacity("openai/local-custom-model") == 32_768
    assert capacity("openai/local-custom-model", override=64_000) == 64_000

    # Compaction triggers properly for smaller OpenAI-compatible windows
    big_text = "x" * (32_768 * 4)
    assert needs_compaction(big_text, "openai/local-custom-model") is True

    tracker = TokenTracker()
    tracker.record(100, 50)
    tracker.record(200, 75)
    assert tracker.prompt_tokens == 300
    assert tracker.output_tokens == 125
    assert tracker.total_tokens == 425
    assert tracker.turns == 2


def test_cli_normalization_and_dynamic_completer(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.py").write_text("print('hi')\n", encoding="utf8")

    assert normalize_cli_request(["/audit src"]) == "/audit src"
    assert normalize_cli_request(["/audit", "src"]) == "/audit src"
    assert normalize_cli_request(["explain", "this", "bug"]) == "explain this bug"

    h = AgentHarness(tmp_path, approve=lambda _: True)
    completer = build_completer(h)

    # Slash command completion
    cmd_completions = [
        c.text for c in completer.get_completions(Document("/con"), None)
    ]
    assert "/config" in cmd_completions

    # Subcommand completion for /scaffold
    scaffold_completions = [
        c.text for c in completer.get_completions(Document("/scaffold adk"), None)
    ]
    assert "adk-python" in scaffold_completions
    assert "adk-typescript" in scaffold_completions

    # @file mention completion
    file_completions = [
        c.text for c in completer.get_completions(Document("please check @src/m"), None)
    ]
    assert "@src/main.py" in file_completions


def test_theme_non_git_safety_and_toolbar(tmp_path):
    h = AgentHarness(tmp_path, approve=lambda _: True)
    plain = prompt(tmp_path, h.model, "", styled=False)
    assert h.model in plain
    assert "[0%]" in plain

    styled = prompt(tmp_path, h.model, "", styled=True)
    assert any(h.model in text for _, text in styled)

    toolbar = bottom_toolbar(h)
    assert any(tmp_path.name in text for _, text in toolbar)


def test_diff_viewer_and_notifier_escaping():
    # Smoke-test preview does not raise on small/large diffs
    preview("example.py", "a = 1\nb = 2\n", "a = 1\nb = 3\nc = 4\n")

    mac_cmd = build_command('Build "done" & ok', platform="darwin")
    assert mac_cmd[0] == "osascript"
    assert '\\"done\\"' in mac_cmd[2]

    win_cmd = build_command("It's done", platform="win32")
    assert win_cmd[0] == "powershell"
    assert "It''s done" in win_cmd[-1]

    linux_cmd = build_command("Tests passed", platform="linux")
    assert linux_cmd == ["notify-send", "adk-coder", "Tests passed"]


def test_openai_env_api_base_forwarded_to_litellm(tmp_path, monkeypatch):
    import asyncio

    from google.adk.models.lite_llm import LiteLlm

    monkeypatch.setenv("OPENAI_API_BASE", "http://192.168.1.33:8080/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "local")
    monkeypatch.setenv(
        "ADK_CODER_MODEL", "openai//Users/pavankumar/AI/models/ornith-1.5-9b-8bit"
    )
    h = AgentHarness(tmp_path, approve=lambda _: True)

    seen = {}

    async def inspect_agent(agent, prompt):
        seen["model"] = agent.model
        return "ok"

    monkeypatch.setattr("adk_coder.model_factory.run_agent", inspect_agent)
    assert asyncio.run(h.execute("ping")) == "ok"
    assert isinstance(seen["model"], LiteLlm)
    assert seen["model"].model == "openai//Users/pavankumar/AI/models/ornith-1.5-9b-8bit"
    assert seen["model"]._additional_args.get("api_base") == "http://192.168.1.33:8080/v1"
    assert seen["model"]._additional_args.get("api_key") == "local"
    assert seen["model"]._additional_args.get("max_tokens") == 8192


def test_ollama_model_routing_and_host_forwarding(tmp_path, monkeypatch):
    import asyncio

    from google.adk.models.lite_llm import LiteLlm

    monkeypatch.setenv("OLLAMA_HOST", "192.168.1.33:11434")
    monkeypatch.setenv("ADK_CODER_MODEL", "ollama_chat/qwen2.5-coder:14b")
    h = AgentHarness(tmp_path, approve=lambda _: True)

    seen = {}

    async def inspect_agent(agent, prompt):
        seen["model"] = agent.model
        return "ok"

    monkeypatch.setattr("adk_coder.model_factory.run_agent", inspect_agent)
    assert asyncio.run(h.execute("ping")) == "ok"
    assert isinstance(seen["model"], LiteLlm)
    assert seen["model"].model == "ollama_chat/qwen2.5-coder:14b"
    assert seen["model"]._additional_args.get("api_base") == "http://192.168.1.33:11434"
    assert seen["model"]._additional_args.get("max_tokens") == 8192
