"""Tests for the multi-sub-agent /debug workflow, log parser, and ADK sub-agent definitions."""

from __future__ import annotations

import asyncio
import sys

from adk_coder import harness as harness_mod
from adk_coder.audit import CACHE
from adk_coder.debugger import (
    FrameRef,
    _discover_companion_files,
    _sanitize_subagent_output,
    build_debug_agent_tool,
    build_debug_sub_agents,
    parse_logs,
)
from adk_coder.fixer import fix
from adk_coder.harness import AgentHarness


def test_parse_logs_multi_runtime_dedup_and_scrub(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("def div(a, b):\n    return a / b\n", encoding="utf8")
    (tmp_path / "OrderService.java").write_text("class OrderService {}\n", encoding="utf8")

    raw = """\
2026-11-24T10:00:01Z ERROR Connection failed with api_key="sk-secret1234567890abcdef"
2026-11-24T10:00:02Z ERROR Connection failed with api_key="sk-secret1234567890abcdef"
Traceback (most recent call last):
  File "/app/src/calc.py", line 2, in div
    return a / b
ZeroDivisionError: division by zero
java.lang.NullPointerException: missing customer
    at com.acme.OrderService.create(OrderService.java:42)
{"level":"error","message":"Worker timeout in src/calc.py:2"}
"""
    parsed = parse_logs(raw, tmp_path, source_label="app.log")

    assert "Python" in parsed.languages
    assert "Java/JVM" in parsed.languages
    assert "JSON-log" in parsed.languages
    # Secret must be scrubbed from parsed output
    payload_str = str(parsed.to_payload())
    assert "sk-secret1234567890abcdef" not in payload_str
    # Repeated timestamped errors deduplicated
    assert any(cnt == 2 and "Connection failed" in msg for msg, cnt in parsed.error_lines)
    # Container path /app/src/calc.py mapped to local workspace path src/calc.py
    ws_paths = {f.workspace_path for f in parsed.frames}
    assert "src/calc.py" in ws_paths
    assert "OrderService.java" in ws_paths
    assert len(parsed.stack_blocks) >= 2


def test_build_debug_sub_agents_and_agent_tool(tmp_path):
    h = AgentHarness(tmp_path, approve=lambda _: True)
    agents = build_debug_sub_agents(h)

    assert set(agents.keys()) == {
        "log_analyst",
        "root_cause_investigator",
        "fix_engineer",
        "verifier",
    }
    assert agents["log_analyst"].output_key == "log_analysis"
    assert agents["log_analyst"].tools == []
    assert agents["root_cause_investigator"].output_key == "root_cause_report"
    assert len(agents["root_cause_investigator"].tools) == 4
    assert agents["fix_engineer"].output_key == "fix_summary"
    assert len(agents["fix_engineer"].tools) >= 7
    assert agents["verifier"].output_key == "verification_verdict"

    tool = build_debug_agent_tool(h)
    assert tool.agent.name == "root_cause_investigator"


def test_debug_workflow_analyze_only(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("def run():\n    return 1 / 0\n", encoding="utf8")
    log_file = tmp_path / "error.log"
    log_file.write_text(
        'Traceback (most recent call last):\n  File "app.py", line 2, in run\nZeroDivisionError: division by zero\n',
        encoding="utf8",
    )

    h = AgentHarness(tmp_path, approve=lambda _: True)
    calls: list[str] = []

    async def fake_generate(model, instruction, prompt, tools=(), **kwargs):
        if "You are the Log Analyst" in instruction:
            calls.append("log_analyst")
            return "Primary error: ZeroDivisionError at app.py:2."
        if "You are the Root-Cause Investigator" in instruction:
            calls.append("root_cause_investigator")
            return "### Root Cause\nDivision by zero in `app.py:2`.\n### Fix Plan\nGuard divisor."
        calls.append("unexpected")
        return "unexpected"

    monkeypatch.setattr(harness_mod, "generate", fake_generate)

    result = asyncio.run(h.execute(f"/debug {log_file.name} --analyze-only"))
    assert calls == ["log_analyst", "root_cause_investigator"]
    assert "Debug analysis saved" in result
    assert (tmp_path / ".agent" / "DEBUG_REPORT.md").is_file()
    assert (tmp_path / "docs" / "DEBUG_REPORT.md").is_file()
    report = (tmp_path / "docs" / "DEBUG_REPORT.md").read_text(encoding="utf8")
    assert "ZeroDivisionError" in report
    assert "Division by zero" in report


def test_debug_workflow_cmd_fix_and_verify_pass(tmp_path, monkeypatch):
    buggy = tmp_path / "calc.py"
    buggy.write_text(
        "def divide(a, b):\n    return a / b\n\nif __name__ == '__main__':\n    assert divide(4, 0) == 0\n",
        encoding="utf8",
    )
    h = AgentHarness(tmp_path, approve=lambda _: True)
    calls: list[str] = []

    async def fake_generate(model, instruction, prompt, tools=(), **kwargs):
        if "You are the Log Analyst" in instruction:
            calls.append("log_analyst")
            return "ZeroDivisionError in calc.py:2"
        if "You are the Root-Cause Investigator" in instruction:
            calls.append("root_cause_investigator")
            return "### Root Cause\nMissing zero check in divide()."
        if "You are the Fix Engineer" in instruction:
            calls.append("fix_engineer")
            res = h.tools.apply_diff(
                "calc.py",
                "    return a / b",
                "    return 0 if b == 0 else a / b",
            )
            assert res == "Saved."
            return "Guarded b == 0 in calc.py."
        calls.append("other")
        return "PASS"

    monkeypatch.setattr(harness_mod, "generate", fake_generate)

    cmd = f'"{sys.executable}" calc.py'
    result = asyncio.run(h.execute(f"/debug --cmd '{cmd}'"))
    assert calls == ["log_analyst", "root_cause_investigator", "fix_engineer"]
    assert "exited 0" in result
    assert "0 if b == 0" in buggy.read_text(encoding="utf8")


def test_debug_workflow_rollback_when_verification_fails(tmp_path, monkeypatch):
    buggy = tmp_path / "broken.py"
    original = "def val():\n    return 1 / 0\n\nval()\n"
    buggy.write_text(original, encoding="utf8")

    h = AgentHarness(tmp_path, approve=lambda _: True)
    fix_attempts = 0

    async def fake_generate(model, instruction, prompt, tools=(), **kwargs):
        nonlocal fix_attempts
        if "You are the Log Analyst" in instruction:
            return "ZeroDivisionError in broken.py:2"
        if "You are the Root-Cause Investigator" in instruction:
            return "### Root Cause\nDivision by zero."
        if "You are the Fix Engineer" in instruction:
            fix_attempts += 1
            # Bad patch that still fails verification
            h.tools.apply_diff(
                "broken.py",
                "return 1 / 0",
                "return 2 / 0",
            )
            return f"Attempt {fix_attempts} applied."
        return "RETRY"

    monkeypatch.setattr(harness_mod, "generate", fake_generate)

    cmd = f'"{sys.executable}" broken.py'
    result = asyncio.run(h.execute(f"/debug --cmd '{cmd}' --max-retries 2"))
    assert fix_attempts == 2
    assert "rolled back" in result.lower()
    # Workspace file must be restored cleanly to original content
    assert buggy.read_text(encoding="utf8") == original


def test_discover_companion_production_files(tmp_path):
    (tmp_path / "src/main/java/com/loan").mkdir(parents=True)
    (tmp_path / "src/test/java/com/loan").mkdir(parents=True)
    (tmp_path / "src/main/java/com/loan/LoanEmiService.java").write_text("class LoanEmiService {}", encoding="utf8")
    (tmp_path / "src/test/java/com/loan/LoanEmiServiceTest.java").write_text("class LoanEmiServiceTest {}", encoding="utf8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/calc.py").write_text("def add(a, b): return a + b\n", encoding="utf8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_calc.py").write_text("def test_add(): pass\n", encoding="utf8")

    frames = [
        FrameRef(file="LoanEmiServiceTest.java", line=27, workspace_path="src/test/java/com/loan/LoanEmiServiceTest.java"),
        FrameRef(file="tests/test_calc.py", line=1, workspace_path="tests/test_calc.py"),
    ]
    companions = _discover_companion_files(tmp_path, frames)
    assert "src/main/java/com/loan/LoanEmiService.java" in companions
    assert "pkg/calc.py" in companions


def test_sanitize_subagent_output_condenses_thinking_loops():
    noisy = (
        "Let me reconsider this calculation.\n"
        + ("Hmm, wait. Let me recompute and let me reconsider again.\n" * 40)
        + "- Real defect: monthlyRate rounded to scale 2\n"
    )
    sanitized = _sanitize_subagent_output(noisy, max_chars=2000)
    assert len(sanitized) < 1000
    assert "monthlyRate rounded to scale 2" in sanitized
    assert "Condensed repetitive reasoning trace" in sanitized


def test_debug_workflow_zero_edit_triggers_structured_patch_fallback(tmp_path, monkeypatch):
    buggy = tmp_path / "calc.py"
    buggy.write_text(
        "def divide(a, b):\n    return a / b\n\nif __name__ == '__main__':\n    assert divide(4, 0) == 0\n",
        encoding="utf8",
    )
    h = AgentHarness(tmp_path, approve=lambda _: True)
    fallback_called = False

    async def fake_generate(model, instruction, prompt, tools=(), **kwargs):
        nonlocal fallback_called
        if "You are the Log Analyst" in instruction:
            return "### Primary Error Signature\nZeroDivisionError in calc.py:2"
        if "You are the Root-Cause Investigator" in instruction:
            return "### Root Cause\nMissing zero check in divide()."
        if "You are the Fix Engineer" in instruction:
            # Simulate a reasoning model that forgets to invoke apply_diff
            return "Let me reconsider... I think divide should handle b == 0."
        if "surgical patch generator" in instruction:
            fallback_called = True
            return (
                '{"edits": [{"file": "calc.py", '
                '"search_block": "    return a / b   ", '
                '"replace_block": "    return 0 if b == 0 else a / b", '
                '"explanation": "Handle zero divisor"}]}'
            )
        return "PASS"

    monkeypatch.setattr(harness_mod, "generate", fake_generate)

    cmd = f'"{sys.executable}" calc.py'
    result = asyncio.run(h.execute(f"/debug --cmd '{cmd}'"))
    assert fallback_called is True
    assert "exited 0" in result
    assert "0 if b == 0" in buggy.read_text(encoding="utf8")


def test_fixer_retries_with_feedback_and_fuzzy_match(tmp_path, monkeypatch):
    import json as _json

    target = tmp_path / "math_util.py"
    target.write_text(
        "def safe_div(a, b):\n    return a / b\n\nif __name__ == '__main__':\n    assert safe_div(10, 0) == 0\n",
        encoding="utf8",
    )
    h = AgentHarness(tmp_path, approve=lambda _: True)
    h.config["test_command"] = f'"{sys.executable}" math_util.py'
    (h.state / CACHE).write_text(
        _json.dumps(
            {
                "findings": [
                    {
                        "file": "math_util.py",
                        "line": 2,
                        "severity": "HIGH",
                        "code": "BUG",
                        "message": "ZeroDivisionError when b == 0",
                    }
                ]
            }
        ),
        encoding="utf8",
    )

    seen_prompts: list[str] = []

    async def fake_generate(model, instruction, prompt, tools=(), **kwargs):
        seen_prompts.append(prompt)
        if len(seen_prompts) == 1:
            # First attempt returns a broken patch that fails test_command
            return _json.dumps(
                {
                    "search_block": "    return a / b",
                    "replace_block": "    return -1 if b == 0 else a / b",
                    "explanation": "Wrong fallback value",
                }
            )
        # Second attempt sees previous_attempt_error and returns trailing-space search_block + fix
        return _json.dumps(
            {
                "search_block": "    return a / b   ",
                "replace_block": "    return 0 if b == 0 else a / b",
                "explanation": "Return 0 when b == 0",
            }
        )

    monkeypatch.setattr(harness_mod, "generate", fake_generate)

    res = asyncio.run(fix(h, "all"))
    assert len(seen_prompts) == 2
    assert "previous_attempt_error" in seen_prompts[1]
    assert "0 audit findings remaining" in res
    assert "return 0 if b == 0 else a / b" in target.read_text(encoding="utf8")


def test_debug_autodetects_project_test_cmd_and_latest_log(tmp_path):
    from adk_coder.debugger import (
        _autodetect_debug_command,
        _autodetect_latest_log,
        _collect_log_input,
    )

    py_ws = tmp_path / "py_proj"
    py_ws.mkdir()
    (py_ws / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf8")
    assert _autodetect_debug_command(py_ws) == "pytest -q"

    mvn_ws = tmp_path / "mvn_proj"
    mvn_ws.mkdir()
    (mvn_ws / "pom.xml").write_text("<project/>", encoding="utf8")
    assert _autodetect_debug_command(mvn_ws) == "mvn -B test"

    log_ws = tmp_path / "log_proj"
    log_ws.mkdir()
    (log_ws / "logs").mkdir()
    err_log = log_ws / "logs" / "crash.log"
    err_log.write_text("ERROR boom\n", encoding="utf8")
    assert _autodetect_latest_log(log_ws) == err_log

    h = AgentHarness(log_ws, approve=lambda _: True)
    raw, label, rc = _collect_log_input(h, {"cmd": None, "targets": []})
    assert "ERROR boom" in raw and label == "logs/crash.log" and rc is None
