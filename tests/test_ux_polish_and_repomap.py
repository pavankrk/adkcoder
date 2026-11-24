"""Tests for UX polish: AST repo map, /doctor, typo suggestions, /paste, /export, fast_model, multimodal image attachments."""

import asyncio

import pytest

from adk_coder.harness import AgentHarness
from adk_coder.repo_map import build_repo_map


def test_repo_map_extracts_python_java_ts_go_and_injects_into_chat(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text(
        "class TodoService:\n    def create(self):\n        pass\n\ndef health():\n    return True\n",
        encoding="utf8",
    )
    (tmp_path / "src/TodoController.java").write_text(
        'public class TodoController {\n    @GetMapping("/api/todos")\n    public String list() { return ""; }\n}\n',
        encoding="utf8",
    )
    (tmp_path / "src/api.ts").write_text(
        "export interface Todo { id: string }\nexport function fetchTodos() {}\n",
        encoding="utf8",
    )
    (tmp_path / "src/main.go").write_text(
        "package main\ntype Server struct{}\nfunc StartServer() {}\n",
        encoding="utf8",
    )

    symbol_map = build_repo_map(tmp_path)
    assert "class TodoService(create)" in symbol_map
    assert "def health()" in symbol_map
    assert "class TodoController" in symbol_map
    assert "route /api/todos" in symbol_map
    assert "interface Todo" in symbol_map
    assert "type Server" in symbol_map
    assert "func StartServer()" in symbol_map

    h = AgentHarness(tmp_path, approve=lambda _: True)
    assert "TodoService" in asyncio.run(h.execute("/map"))

    seen_instructions = []

    async def fake_generate(model, instruction, prompt, tools):
        seen_instructions.append(instruction)
        return "ok"

    monkeypatch.setattr("adk_coder.harness.generate", fake_generate)
    asyncio.run(h.execute("where is TodoController?"))
    assert "Repository Symbol Map" in seen_instructions[0]
    assert "TodoController" in seen_instructions[0]


def test_doctor_offline_and_typo_suggestions(tmp_path):
    h = AgentHarness(tmp_path, approve=lambda _: True)
    report = asyncio.run(h.execute("/doctor --offline"))
    assert "Python runtime" in report
    assert "Skipped (--offline)" in report

    with pytest.raises(ValueError, match="Did you mean /scaffold"):
        asyncio.run(h.execute("/scafhold"))


def test_paste_export_fast_model_and_image_attachments(tmp_path, monkeypatch):
    h = AgentHarness(tmp_path, approve=lambda _: True)

    # Dual-model fast_model routing for /compact
    asyncio.run(h.execute("/config fast_model gemini-2.5-flash"))
    asyncio.run(h.execute("/model gemini-2.5-pro"))
    h.write("context_summary.md", "Some conversation history.\n")

    used_models = []

    async def fake_generate(model, instruction, prompt, tools):
        used_models.append(model)
        return "Compact summary"

    monkeypatch.setattr("adk_coder.harness.generate", fake_generate)
    asyncio.run(h.execute("/compact"))
    assert used_models == ["gemini-2.5-flash"]

    # /paste injects clipboard text
    seen_chat = []

    async def fake_chat(req, **kw):
        seen_chat.append(req)
        return "Analyzed traceback"

    monkeypatch.setattr(h, "chat", fake_chat)
    monkeypatch.setattr(h, "_read_clipboard", lambda: "Traceback: ZeroDivisionError")
    assert asyncio.run(h.execute("/paste explain this")) == "Analyzed traceback"
    assert "ZeroDivisionError" in seen_chat[0]

    # Multimodal image attachment detection
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    attachments = h._extract_image_attachments("Look at @shot.png please")
    assert len(attachments) == 1
    assert attachments[0][1] == "image/png"

    # /export writes clean Markdown transcript
    out = asyncio.run(h.execute("/export docs/my_session.md"))
    assert "Exported session" in out
    exported = (tmp_path / "docs/my_session.md").read_text(encoding="utf8")
    assert "# ADK Coder Session Export" in exported
    assert "Compact summary" in exported
