"""End-to-end unit tests for all 6 developer productivity features:
profiles + auto-fallback, /testgen, /pr-review, /diagram, /resolve-conflicts, and /watch.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from adk_coder import harness as harness_mod
from adk_coder.audit import CACHE
from adk_coder.config_ui import dispatch_request
from adk_coder.conflicts import find_conflicted_files, parse_conflict_hunks
from adk_coder.diagram import analyze_structure
from adk_coder.harness import AgentHarness
from adk_coder.profiles import save_profile, set_fallback_profile
from adk_coder.testgen import detect_test_framework
from adk_coder.watcher import diff_snapshots, snapshot_workspace


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=root, check=True)


def test_profiles_cli_web_api_and_auto_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    env_file = tmp_path / ".env"
    ws = tmp_path / "repo"
    ws.mkdir()
    monkeypatch.setenv("ADK_CODER_ENV_FILE", str(env_file))

    # Save two profiles via Web UI dispatch routes
    status, _, raw = dispatch_request(
        "POST",
        "/api/profiles/save",
        {
            "name": "LocalTunnel",
            "provider": "openai-compatible",
            "base_url": "https://dead-tunnel.example/v1",
            "model": "ornith-35b",
            "api_key": "local",
            "mode": "full-auto",
        },
        env_file,
        ws,
    )
    assert status == 200 and json.loads(raw)["ok"] is True

    status, _, raw = dispatch_request(
        "POST",
        "/api/profiles/save",
        {
            "name": "BackupOllama",
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434",
            "model": "qwen2.5-coder:14b",
            "mode": "auto-edit",
        },
        env_file,
        ws,
    )
    assert status == 200 and json.loads(raw)["ok"] is True

    status, _, raw = dispatch_request(
        "POST",
        "/api/profiles/fallback",
        {"name": "BackupOllama"},
        env_file,
        ws,
    )
    assert status == 200 and json.loads(raw)["fallback"] == "BackupOllama"

    status, _, raw = dispatch_request(
        "POST",
        "/api/profiles/use",
        {"name": "LocalTunnel", "workspace": str(ws)},
        env_file,
        ws,
    )
    assert status == 200 and json.loads(raw)["name"] == "LocalTunnel"

    status, _, raw = dispatch_request("GET", "/api/profiles", {}, env_file, ws)
    listing = json.loads(raw)
    assert listing["active"] == "LocalTunnel"
    assert listing["fallback"] == "BackupOllama"
    assert listing["profiles"]["LocalTunnel"]["has_api_key"] is True

    # Verify automatic failover in AgentHarness.ask() when primary raises ConnectionError
    h = AgentHarness(ws, approve=lambda _: True)
    seen_models: list[str] = []

    async def fake_generate(model, role, prompt, tools=(), **kwargs):
        seen_models.append(model)
        if len(seen_models) == 1:
            raise ConnectionError("Cloudflare tunnel 502 bad gateway")
        return "Recovered via fallback!"

    monkeypatch.setattr(harness_mod, "generate", fake_generate)
    reply = asyncio.run(h.ask("helper", "hello"))
    assert reply == "Recovered via fallback!"
    assert seen_models == ["openai/ornith-35b", "ollama_chat/qwen2.5-coder:14b"]

    # CLI /profile commands
    listed = asyncio.run(h.execute("/profile list"))
    assert "LocalTunnel" in listed and "BackupOllama" in listed
    switched = asyncio.run(h.execute("/profile use BackupOllama"))
    assert "BackupOllama" in switched
    assert h.model == "ollama_chat/qwen2.5-coder:14b"

    # Cleanup profile delete
    save_profile("TempProf", {"provider": "gemini", "model": "gemini-2.5-flash"}, env_path=env_file)
    set_fallback_profile("none", env_path=env_file)
    deleted = asyncio.run(h.execute("/profile delete TempProf"))
    assert "Deleted profile" in deleted


def test_diagram_imports_cycles_and_routes(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/a.py").write_text(
        'import pkg.b\n@app.get("/health")\ndef health(): return {"ok": True}\n',
        encoding="utf8",
    )
    (tmp_path / "pkg/b.py").write_text("from pkg import a\n", encoding="utf8")
    (tmp_path / "src/main/java/com/demo").mkdir(parents=True)
    (tmp_path / "src/main/java/com/demo/LoanController.java").write_text(
        'package com.demo;\n@RequestMapping("/api/loans")\nclass LoanController {\n'
        '  @GetMapping("/emi")\n  void emi() {}\n}\n',
        encoding="utf8",
    )

    data = analyze_structure(tmp_path)
    assert "pkg/b.py" in data["edges"]["pkg/a.py"]
    assert "pkg/a.py" in data["edges"]["pkg/b.py"]
    assert len(data["cycles"]) == 1
    route_paths = {r["path"] for r in data["routes"]}
    assert "/health" in route_paths
    assert "/api/loans/emi" in route_paths

    h = AgentHarness(tmp_path, approve=lambda _: True)
    res = asyncio.run(h.execute("/diagram"))
    assert "1 cycle(s)" in res
    assert "2 route(s)" in res
    md = (tmp_path / "docs/DIAGRAM.md").read_text(encoding="utf8")
    assert "```mermaid" in md
    assert "/api/loans/emi" in md


def test_testgen_detects_frameworks_and_generates_passing_tests(tmp_path, monkeypatch):
    # Framework detection checks
    (tmp_path / "src/main/java/com/x").mkdir(parents=True)
    java_f = tmp_path / "src/main/java/com/x/Calc.java"
    java_f.write_text("class Calc {}", encoding="utf8")
    assert detect_test_framework(tmp_path, java_f)["framework"] == "JUnit 5"
    assert detect_test_framework(tmp_path, java_f)["target_test_path"] == "src/test/java/com/x/CalcTest.java"

    py_f = tmp_path / "calc.py"
    py_f.write_text("def add(a, b):\n    return a + b\n", encoding="utf8")
    assert detect_test_framework(tmp_path, py_f)["framework"] == "pytest"

    h = AgentHarness(tmp_path, approve=lambda _: True)
    attempts = 0

    async def fake_generate(model, instruction, prompt, tools=(), **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            # First attempt writes a failing assertion via fenced code block fallback
            return "```python\nfrom calc import add\ndef test_add():\n    assert add(2, 3) == 999\n```"
        # Second attempt fixes the test using write_file
        h.tools.write_file(
            "tests/test_calc.py",
            "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n    assert add(0, 0) == 0\n",
        )
        return "Fixed assertion in tests/test_calc.py."

    monkeypatch.setattr(harness_mod, "generate", fake_generate)
    cmd = f'"{sys.executable}" -m pytest -q tests/test_calc.py'
    res = asyncio.run(h.execute(f"/testgen calc.py --cmd '{cmd}'"))
    assert attempts == 2
    assert "passed on attempt 2/2" in res
    assert (tmp_path / "tests/test_calc.py").is_file()


def test_pr_review_branch_diff_and_cache_sync(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    (tmp_path / "app.py").write_text("def greet(name):\n    return f'hi {name}'\n", encoding="utf8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

    subprocess.run(["git", "checkout", "-b", "feat/eval"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "app.py").write_text("def greet(name):\n    return eval(name)\n", encoding="utf8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "use eval"], cwd=tmp_path, check=True, capture_output=True)

    h = AgentHarness(tmp_path, approve=lambda _: True)

    async def fake_generate(model, instruction, prompt, tools=(), **kwargs):
        return json.dumps(
            {
                "risk_level": "HIGH",
                "pr_summary": "Replaces string formatting with unsafe eval().",
                "findings": [
                    {
                        "severity": "CRITICAL",
                        "category": "Security",
                        "file": "app.py",
                        "line": 2,
                        "message": "Avoid eval() on untrusted input.",
                        "suggestion": "Use f-string formatting.",
                    }
                ],
                "test_gaps": ["Add unit test for greet() with special characters."],
            }
        )

    monkeypatch.setattr(harness_mod, "generate", fake_generate)
    res = asyncio.run(h.execute("/pr-review main"))
    assert "risk: HIGH" in res
    assert (tmp_path / "docs/PR_REVIEW.md").is_file()
    md = (tmp_path / "docs/PR_REVIEW.md").read_text(encoding="utf8")
    assert "Avoid eval()" in md
    cached = json.loads((tmp_path / ".agent" / CACHE).read_text(encoding="utf8"))
    assert any(f["file"] == "app.py" for f in cached["findings"])


def test_resolve_conflicts_merges_hunks_cleanly(tmp_path, monkeypatch):
    conflicted = tmp_path / "config.py"
    conflicted.write_text(
        "HOST = 'localhost'\n"
        "<<<<<<< HEAD\n"
        "PORT = 8080\n"
        "=======\n"
        "PORT = 9090\n"
        "DEBUG = False\n"
        ">>>>>>> feature\n",
        encoding="utf8",
    )
    assert len(find_conflicted_files(tmp_path)) == 1
    assert len(parse_conflict_hunks(conflicted.read_text(encoding="utf8"))) == 1

    h = AgentHarness(tmp_path, approve=lambda _: True)

    async def fake_generate(model, instruction, prompt, tools=(), **kwargs):
        return json.dumps(
            {
                "resolved_code": "PORT = 8080\nDEBUG = False\n",
                "strategy": "merge",
                "explanation": "Keep production port 8080 and add DEBUG flag.",
            }
        )

    monkeypatch.setattr(harness_mod, "generate", fake_generate)
    res = asyncio.run(h.execute("/resolve-conflicts"))
    assert "Resolved 1/1 conflicted file(s)" in res
    final_txt = conflicted.read_text(encoding="utf8")
    assert "<<<<<<<" not in final_txt
    assert "PORT = 8080" in final_txt and "DEBUG = False" in final_txt


def test_watch_snapshot_diff_and_once_cycle(tmp_path):
    script = tmp_path / "ok.py"
    script.write_text("assert 2 + 2 == 4\n", encoding="utf8")

    snap1 = snapshot_workspace(tmp_path)
    script.write_text("assert 2 + 2 == 4  # updated\n", encoding="utf8")
    snap2 = snapshot_workspace(tmp_path)
    assert diff_snapshots(snap1, snap2) == ["ok.py"]

    h = AgentHarness(tmp_path, approve=lambda _: True)
    cmd = f'"{sys.executable}" ok.py'
    res = asyncio.run(h.execute(f"/watch --once --cmd '{cmd}'"))
    assert "[PASS]" in res
