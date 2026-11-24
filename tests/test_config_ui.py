"""Tests for the LLM configuration utility, Web UI API, .env sync, and connectivity checker."""

import json
import os

from adk_coder.config_ui import (
    check_connectivity,
    dispatch_request,
    parse_env_file,
    save_ui_state,
)


def test_save_env_and_workspace_config(tmp_path):
    env_file = tmp_path / ".env"
    ws = tmp_path / "my-repo"
    ws.mkdir()

    res = save_ui_state(
        env_file,
        {
            "provider": "openai-compatible",
            "base_url": "http:192.168.1.33:8080/v1",
            "model": "/Volumes/models/mellum-12b",
            "api_key": "local",
            "api_token": "tok-123",
            "workspace": str(ws),
            "mode": "auto-edit",
            "max_tokens": 8192,
            "test_command": "mvn -B test",
            "sandbox_shell": True,
        },
    )
    assert res["ok"] is True
    assert res["model"] == "openai//Volumes/models/mellum-12b"

    parsed = parse_env_file(env_file)
    assert parsed["OPENAI_API_BASE"] == "http://192.168.1.33:8080/v1"
    assert parsed["OPENAI_BASE_URL"] == "http://192.168.1.33:8080/v1"
    assert parsed["ADK_CODER_MODEL"] == "openai//Volumes/models/mellum-12b"
    assert parsed["ADK_CODER_ENDPOINT_API_KEY"] == "tok-123"

    ws_cfg = json.loads((ws / ".agent/config.json").read_text(encoding="utf8"))
    assert ws_cfg["model"] == "openai//Volumes/models/mellum-12b"
    assert ws_cfg["openai_api_base"] == "http://192.168.1.33:8080/v1"
    assert ws_cfg["mode"] == "auto-edit"
    assert ws_cfg["test_command"] == "mvn -B test"
    assert ws_cfg["sandbox_shell"] is True


def test_connectivity_checker_success_and_error_hints():
    def fake_ok(url, payload, headers, timeout=15.0):
        assert url == "http://192.168.1.33:8080/v1/chat/completions"
        assert headers["Authorization"] == "Bearer my-token"
        return 200, {"choices": [{"message": {"content": "OK"}}]}

    ok = check_connectivity(
        provider="openai-compatible",
        model="openai//Volumes/models/mellum",
        base_url="http://192.168.1.33:8080/v1",
        api_token="my-token",
        poster=fake_ok,
    )
    assert ok["ok"] is True
    assert ok["reply"] == "OK"

    def fake_401(url, payload, headers, timeout=15.0):
        return 401, "Unauthorized"

    bad = check_connectivity(
        provider="ollama",
        model="qwen2.5-coder:14b",
        base_url="http://127.0.0.1:11434",
        poster=fake_401,
    )
    assert bad["ok"] is False
    assert "Authentication failed" in bad["hint"]


def test_config_ui_dispatch_routes(tmp_path):
    env_file = tmp_path / ".env"

    def fake_poster(url, payload, headers, timeout=15.0):
        return 200, {"choices": [{"message": {"content": "OK"}}]}

    status, ctype, html = dispatch_request("GET", "/", {}, env_file, tmp_path)
    assert status == 200
    assert "text/html" in ctype
    assert b"ADK Coder" in html
    assert b"#f6f8fc" in html  # Google/Gmail light surface theme

    status, _, raw = dispatch_request(
        "POST",
        "/api/save",
        {
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434",
            "model": "qwen2.5-coder:14b",
            "workspace": str(tmp_path),
        },
        env_file,
        tmp_path,
    )
    assert status == 200
    saved = json.loads(raw)
    assert saved["ok"] is True
    assert saved["model"] == "ollama_chat/qwen2.5-coder:14b"

    status, _, state_raw = dispatch_request("GET", "/api/state", {}, env_file, tmp_path)
    assert status == 200
    state = json.loads(state_raw)
    assert state["provider"] == "ollama"
    assert state["model"] == "ollama_chat/qwen2.5-coder:14b"

    status, _, test_raw = dispatch_request(
        "POST",
        "/api/test",
        {
            "provider": "openai-compatible",
            "base_url": "http://192.168.1.33:8080/v1",
            "model": "test-model",
            "api_key": "local",
        },
        env_file,
        tmp_path,
        poster=fake_poster,
    )
    assert status == 200
    assert json.loads(test_raw)["ok"] is True

    # Pasting full /v1/models URL strips /models cleanly before calling /chat/completions
    seen_urls = []

    def capture_poster(url, payload, headers, timeout=60.0):
        seen_urls.append(url)
        return 200, {"choices": [{"message": {"content": "OK"}}]}

    res = check_connectivity(
        provider="openai-compatible",
        model="hf.co/ornith-ai/Ornith-1.5-35B-A3B-GGUF:Q4_K_M",
        base_url="https://shoes-anonymous-dear-nyc.trycloudflare.com/v1/models",
        api_key="local",
        poster=capture_poster,
    )
    assert res["ok"] is True
    assert seen_urls == ["https://shoes-anonymous-dear-nyc.trycloudflare.com/v1/chat/completions"]


def test_global_xdg_env_loading_respects_shell_precedence(tmp_path, monkeypatch):
    from adk_coder.config_ui import default_env_path, load_global_env

    custom_env = tmp_path / "adk-coder/.env"
    save_ui_state(
        custom_env,
        {
            "provider": "gemini",
            "model": "gemini-2.5-pro",
            "api_key": "AIzaSyExampleKey123456789012345678901",
            "sync_workspace": False,
        },
    )
    monkeypatch.setenv("ADK_CODER_ENV_FILE", str(custom_env))
    assert default_env_path() == custom_env.resolve()

    monkeypatch.delenv("ADK_CODER_MODEL", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "explicit-shell-key")
    load_global_env()
    assert os.environ["ADK_CODER_MODEL"] == "gemini-2.5-pro"
    # Explicit shell env var takes precedence over saved file
    assert os.environ["GOOGLE_API_KEY"] == "explicit-shell-key"



def test_full_auto_mode_persists_globally_and_auto_approves(tmp_path, monkeypatch):
    from adk_coder.config_ui import load_global_env, load_ui_state
    from adk_coder.harness import AgentHarness

    env_file = tmp_path / ".env"
    save_ui_state(
        env_file,
        {
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "api_key": "AIzaSyExampleKey123456789012345678901",
            "mode": "full-auto",
            "sync_workspace": False,
        },
    )
    assert parse_env_file(env_file)["ADK_CODER_MODE"] == "full-auto"

    monkeypatch.setenv("ADK_CODER_ENV_FILE", str(env_file))
    monkeypatch.delenv("ADK_CODER_MODE", raising=False)
    load_global_env()
    assert os.environ["ADK_CODER_MODE"] == "full-auto"

    # A separate project workspace picks up full-auto from global .env automatically
    ws = tmp_path / "other-project"
    ws.mkdir()
    assert load_ui_state(env_file, ws)["mode"] == "full-auto"

    prompted = False

    def fail_if_prompted(_msg):
        nonlocal prompted
        prompted = True
        return False

    h = AgentHarness(ws, approve=fail_if_prompted)
    assert h.mode == "full-auto"
    assert h.approve("Write debug report?") is True
    assert prompted is False
