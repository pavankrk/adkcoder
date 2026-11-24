"""Tests for Tier 2 features: named sessions, /context + /pin, /tdd loop, apply_multi_diff, /model list."""

import asyncio

from adk_coder.harness import AgentHarness
from adk_coder.model_discovery import discover_models


def test_named_sessions_create_resume_fork_delete(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf8")
    h = AgentHarness(tmp_path, approve=lambda _: True)

    asyncio.run(h.execute("/pin app.py"))
    h.write("context_summary.md", "Work in default session\n")
    h._sync_session()
    default_id = h.active_session_id

    # Create a fresh session
    msg = asyncio.run(h.execute("/session new auth-work"))
    assert "auth-work" in msg
    assert h.read("context_summary.md") == ""
    assert h._load_pinned() == []
    auth_id = h.active_session_id

    # Fork the auth session
    h.write("context_summary.md", "JWT design notes\n")
    h._sync_session()
    fork_msg = asyncio.run(h.execute("/session fork auth-experiment"))
    assert "auth-experiment" in fork_msg
    assert "JWT design notes" in h.read("context_summary.md")

    # Resume default session and verify pinned files & summary return
    resumed = asyncio.run(h.execute(f"/session resume {default_id}"))
    assert "Resumed session" in resumed
    assert "Work in default session" in h.read("context_summary.md")
    assert h._load_pinned() == ["app.py"]

    # Delete non-active session
    deleted = asyncio.run(h.execute(f"/session delete {auth_id}"))
    assert f"Deleted session {auth_id}." == deleted


def test_context_inspector_and_pin_unpin(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("Follow SOLID.\n", encoding="utf8")
    (tmp_path / "core.py").write_text("MAGIC_CONST = 999\n", encoding="utf8")
    h = AgentHarness(tmp_path, approve=lambda _: True)

    assert "Pinned core.py" in asyncio.run(h.execute("/pin core.py"))
    ctx = asyncio.run(h.execute("/context"))
    assert "Project Rules" in ctx
    assert "core.py" in ctx

    seen_prompts = []

    async def fake_generate(model, instruction, prompt, tools):
        seen_prompts.append(prompt)
        return "Saw pinned file"

    monkeypatch.setattr("adk_coder.harness.generate", fake_generate)
    assert asyncio.run(h.execute("what is the constant?")) == "Saw pinned file"
    assert "MAGIC_CONST = 999" in seen_prompts[0]

    assert "Unpinned core.py" in asyncio.run(h.execute("/unpin core.py"))
    assert h._load_pinned() == []


def test_tdd_loop_fixes_failing_tests_and_exits_green(tmp_path, monkeypatch):
    h = AgentHarness(tmp_path, approve=lambda _: True)
    h.config["test_command"] = "pytest"

    runs = {"count": 0}

    def fake_shell(command, timeout=60):
        runs["count"] += 1
        if runs["count"] == 1:
            return "Exit 1 1 failed\nAssertionError: expected 4 got 3"
        return "Exit 0 1 passed"

    chat_calls = []

    async def fake_chat(prompt, **kw):
        chat_calls.append(prompt)
        return "Fixed calculation."

    monkeypatch.setattr(h.tools, "run_shell_command", fake_shell)
    monkeypatch.setattr(h, "chat", fake_chat)

    out = asyncio.run(h.execute("/tdd 3"))
    assert "Tests passed on attempt 2/3" in out
    assert len(chat_calls) == 1
    assert "AssertionError: expected 4 got 3" in chat_calls[0]


def test_apply_multi_diff_atomic_success_and_rollback(tmp_path):
    target = tmp_path / "service.py"
    target.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf8")
    h = AgentHarness(tmp_path, approve=lambda _: True)

    # Atomic failure if any hunk does not match
    fail = h.tools.apply_multi_diff(
        "service.py",
        [
            {"search_block": "a = 1", "replace_block": "a = 10"},
            {"search_block": "nonexistent", "replace_block": "z = 0"},
        ],
    )
    assert "No changes were written" in fail
    assert target.read_text(encoding="utf8") == "a = 1\nb = 2\nc = 3\n"

    # Successful multi-hunk apply + /undo-edit reverts all hunks atomically
    ok = h.tools.apply_multi_diff(
        "service.py",
        [
            {"search_block": "a = 1", "replace_block": "a = 10"},
            {"search_block": "c = 3", "replace_block": "c = 30"},
        ],
    )
    assert ok == "Saved (2 edits)."
    assert target.read_text(encoding="utf8") == "a = 10\nb = 2\nc = 30\n"

    asyncio.run(h.execute("/undo-edit"))
    assert target.read_text(encoding="utf8") == "a = 1\nb = 2\nc = 3\n"


def test_dynamic_model_discovery_and_index_selection(tmp_path, monkeypatch):
    def fake_fetch(url, headers=None, timeout=3.0):
        if url.endswith("/models"):
            return {"data": [{"id": "/Volumes/models/mellum-12b"}]}
        if url.endswith("/api/tags"):
            return {"models": [{"name": "qwen2.5-coder:14b"}]}
        return {}

    found = discover_models(
        api_base="http://192.168.1.33:8080/v1",
        ollama_host="http://127.0.0.1:11434",
        fetcher=fake_fetch,
    )
    ids = [m["model"] for m in found]
    assert "openai//Volumes/models/mellum-12b" in ids
    assert "ollama_chat/qwen2.5-coder:14b" in ids
    assert "gemini-2.5-flash" in ids

    h = AgentHarness(tmp_path, approve=lambda _: True)
    monkeypatch.setattr("adk_coder.harness.discover_models", lambda **kw: found)
    listing = asyncio.run(h.execute("/model list"))
    assert "openai//Volumes/models/mellum-12b" in listing
    switched = asyncio.run(h.execute("/model 1"))
    assert switched == found[0]["model"]
    assert h.model == found[0]["model"]
