"""Tests for audit stderr resilience, multi-fix offset tracking, /sync, /branch, /task, /config."""

import asyncio
import json
import subprocess

import pytest

from adk_coder.audit import _ruff_severity, response_json
from adk_coder.harness import AgentHarness


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root)


def _init_repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "base.txt").write_text("v1\n", encoding="utf8")
    git(tmp_path, "add", "base.txt")
    git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.org",
        "commit",
        "-qm",
        "feat: initial",
    )


def test_response_json_and_ruff_severity():
    assert response_json('Here is the result:\n```JSON\n{"findings": []}\n```') == {
        "findings": []
    }
    assert response_json('Summary: {"ok": true} done.') == {"ok": True}
    assert _ruff_severity("S602") == "CRITICAL"
    assert _ruff_severity("S101") == "HIGH"
    assert _ruff_severity("I001") == "LOW"
    assert _ruff_severity("E501") == "MEDIUM"


def test_audit_handles_ruff_stderr_without_corrupting_json(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf8")
    h = AgentHarness(tmp_path, approve=lambda _: True)

    ruff_payload = json.dumps(
        [
            {
                "filename": str(tmp_path / "app.py"),
                "location": {"row": 1},
                "code": "S602",
                "message": "subprocess with shell=True",
            }
        ]
    )

    def fake_which(name):
        return "/usr/bin/ruff" if name == "ruff" else None

    def fake_run_process(args, **kwargs):
        return 1, ruff_payload, "warning: deprecated ruff option on stderr\n"

    async def fake_ask(role, prompt):
        return json.dumps({"findings": [], "recommendation": "Looks good"})

    monkeypatch.setattr("adk_coder.audit.shutil.which", fake_which)
    monkeypatch.setattr(h.tools, "run_process", fake_run_process)
    monkeypatch.setattr(h, "ask", fake_ask)

    asyncio.run(h.execute("/audit app.py"))
    cached = json.loads(h.read("audit_cache.json"))["findings"]
    assert len(cached) == 1
    assert cached[0]["severity"] == "CRITICAL"
    assert cached[0]["code"] == "S602"


def test_multi_fix_tracks_line_offsets_and_works_without_test_command(tmp_path, monkeypatch):
    source = tmp_path / "multi.py"
    source.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf8")
    h = AgentHarness(tmp_path, approve=lambda _: True)
    h.write(
        "audit_cache.json",
        json.dumps(
            {
                "findings": [
                    {"file": "multi.py", "line": 1, "severity": "MEDIUM", "code": "T1", "message": "fix a"},
                    {"file": "multi.py", "line": 3, "severity": "MEDIUM", "code": "T2", "message": "fix c"},
                ]
            }
        ),
    )

    seen_adjusted = []

    async def fake_ask(role, prompt):
        payload = json.loads(prompt)
        seen_adjusted.append(payload["finding"]["adjusted_line"])
        if payload["finding"]["code"] == "T1":
            return json.dumps(
                {
                    "search_block": "a = 1",
                    "replace_block": "a = 10\na2 = 11",
                    "explanation": "expand a",
                }
            )
        return json.dumps(
            {
                "search_block": "c = 3",
                "replace_block": "c = 30",
                "explanation": "fix c",
            }
        )

    monkeypatch.setattr(h, "ask", fake_ask)
    result = asyncio.run(h.execute("/fix all"))
    assert "0 audit findings remaining" in result
    assert seen_adjusted == [1, 4]
    assert source.read_text(encoding="utf8") == "a = 10\na2 = 11\nb = 2\nc = 30\n"


def test_sync_includes_committed_branch_changes(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    h = AgentHarness(tmp_path, approve=lambda _: False)

    # Create feature branch and commit a file
    asyncio.run(h.execute("/branch feature-login"))
    (tmp_path / "login.py").write_text("def login():\n    return True\n", encoding="utf8")
    git(tmp_path, "add", "login.py")
    git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.org",
        "commit",
        "-qm",
        "feat: add login endpoint",
    )

    captured_evidence = []

    async def fake_ask(role, prompt):
        captured_evidence.append(prompt)
        return "# PR: Add login endpoint"

    monkeypatch.setattr(h, "ask", fake_ask)
    asyncio.run(h.execute("/sync"))
    assert captured_evidence
    assert "feat: add login endpoint" in captured_evidence[0]
    assert "login.py" in captured_evidence[0]


def test_branch_switch_task_lifecycle_config_and_checkpoint_delete(tmp_path):
    _init_repo(tmp_path)
    h = AgentHarness(tmp_path, approve=lambda _: True)

    # Branch create & switch back to existing branch
    asyncio.run(h.execute("/branch feat-a"))
    assert h.git.run("branch", "--show-current") == "feat-a"
    asyncio.run(h.execute("/branch main"))
    assert h.git.run("branch", "--show-current") == "main"
    asyncio.run(h.execute("/branch -d feat-a"))
    assert "feat-a" not in h.git.run("branch")

    # Task add, done, rm
    asyncio.run(h.execute("/task first task"))
    asyncio.run(h.execute("/task add second task"))
    tasks = asyncio.run(h.execute("/task done 1"))
    assert "- [x] first task" in tasks
    tasks = asyncio.run(h.execute("/task rm 1"))
    assert "first task" not in tasks
    assert "second task" in tasks

    # Config command
    cfg = json.loads(asyncio.run(h.execute("/config test_command pytest -q")))
    assert cfg["test_command"] == "pytest -q"
    cfg = json.loads(asyncio.run(h.execute("/config context_window 65536")))
    assert cfg["context_window"] == 65536
    assert h.context_window == 65536
    with pytest.raises(ValueError):
        asyncio.run(h.execute("/config unknown_key 1"))

    # Checkpoint create and delete
    key = asyncio.run(h.execute("/checkpoint create snap"))
    assert key in asyncio.run(h.execute("/checkpoint list"))
    assert f"Deleted {key}" == asyncio.run(h.execute(f"/checkpoint delete {key}"))
    assert asyncio.run(h.execute("/checkpoint list")) == "No checkpoints."
