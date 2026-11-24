"""Tests for existing-app /analyze -> docs/ANALYSIS.md, interactive fix prompt, and /feature."""

import asyncio

from adk_coder.analyzer import add_feature, analyze
from adk_coder.harness import AgentHarness


def test_analyze_writes_markdown_offers_fix_and_adds_feature(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "legacy-app"\ndependencies = ["fastapi>=0.115"]\n',
        encoding="utf8",
    )
    (tmp_path / "app.py").write_text(
        "def run_calc(expr):\n    return eval(expr)\n",
        encoding="utf8",
    )
    h = AgentHarness(tmp_path, approve=lambda _: True)
    monkeypatch.setattr("adk_coder.security.shutil.which", lambda _: None)

    async def fake_ask(role, prompt):
        return "### Architecture\n```mermaid\nflowchart LR\nAPI-->Calc\n```\nMain risk: eval usage."

    fixed_calls = []

    async def fake_fix(harness, selector):
        fixed_calls.append(selector)
        return "0 audit findings remaining."

    workflow_requests = []

    async def fake_workflow(req):
        workflow_requests.append(req)
        return "PASS feature implemented"

    monkeypatch.setattr(h, "ask", fake_ask)
    monkeypatch.setattr("adk_coder.analyzer.fix", fake_fix)
    monkeypatch.setattr(h, "workflow", fake_workflow)

    # Run /analyze in interactive mode: approves writing docs/ANALYSIS.md, fixing, and adding a feature
    result = asyncio.run(
        analyze(
            h,
            ["."],
            interactive=True,
            ask_input=lambda _: "Add rate limiting middleware",
        )
    )

    doc = tmp_path / "docs/ANALYSIS.md"
    assert doc.is_file()
    content = doc.read_text(encoding="utf8")
    assert "# Application Analysis" in content
    assert "Python (pyproject)" in content
    assert "SAST-EXEC" in content
    assert "API-->Calc" in content
    assert (h.state / "ANALYSIS.md").is_file()

    # Interactive fix prompt triggered automatically because eval() error was found
    assert fixed_calls == ["all"]
    assert "Fix result:" in result

    # Interactive new feature prompt grounded in ANALYSIS.md
    assert len(workflow_requests) == 1
    assert "Add rate limiting middleware" in workflow_requests[0]
    assert "Existing Application Analysis" in workflow_requests[0]
    assert "Feature: Add rate limiting middleware" in h.read("TASKS.md")


def test_feature_command_quick_and_file_input(tmp_path, monkeypatch):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/ANALYSIS.md").write_text(
        "# Application Analysis\nUses FastAPI + Clean Architecture.\n",
        encoding="utf8",
    )
    (tmp_path / "feat.md").write_text(
        "Add CSV export endpoint for todos.\n",
        encoding="utf8",
    )
    h = AgentHarness(tmp_path, approve=lambda _: True)

    seen_chat = []

    async def fake_chat(req, **kw):
        seen_chat.append(req)
        return "Implemented CSV export."

    monkeypatch.setattr(h, "chat", fake_chat)

    out = asyncio.run(add_feature(h, ["@feat.md", "--quick"]))
    assert out == "Implemented CSV export."
    assert "Add CSV export endpoint for todos." in seen_chat[0]
    assert "Uses FastAPI + Clean Architecture." in seen_chat[0]
