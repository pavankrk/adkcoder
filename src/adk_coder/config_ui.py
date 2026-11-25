"""Local Web UI & CLI utility to configure ADK Coder LLM providers, keys, and test connectivity."""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .model_discovery import discover_models
from .scrubber import scrub
from .tools import atomic_write

ENV_KEYS = (
    "ADK_CODER_MODEL",
    "ADK_CODER_MODE",
    "OPENAI_API_BASE",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "ADK_CODER_ENDPOINT_API_KEY",
    "OLLAMA_HOST",
    "OLLAMA_API_BASE",
    "GOOGLE_API_KEY",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "ANTHROPIC_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
)

PROVIDERS = (
    "openai-compatible",
    "ollama",
    "gemini",
    "anthropic",
    "openrouter",
    "groq",
)


def default_env_path() -> Path:
    """Resolve user config .env path: explicit env var, checkout .env, or XDG ~/.config/adk-coder/.env."""
    explicit = os.environ.get("ADK_CODER_ENV_FILE")
    if explicit:
        return Path(explicit).expanduser().resolve()
    try:
        checkout_root = Path(__file__).resolve().parents[3]
        if (checkout_root / "run-adk-coder.sh").is_file():
            return checkout_root / ".env"
    except (IndexError, OSError):
        pass
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        base = Path(xdg).expanduser()
    elif sys.platform == "win32" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"])
    else:
        base = Path.home() / ".config"
    return base / "adk-coder" / ".env"


def load_global_env(env_path: Path | None = None) -> Path:
    """Load saved credentials/model from user .env without overriding explicit shell env vars."""
    path = env_path or default_env_path()
    try:
        entries = parse_env_file(path)
    except OSError:
        entries = {}
    for key, val in entries.items():
        if val:
            os.environ.setdefault(key, val)
    return path


def parse_env_file(path: Path) -> dict[str, str]:
    """Read `export KEY="VAL"` or `KEY=VAL` entries from a .env file."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
            val = val[1:-1]
        if key:
            values[key] = val
    return values


def write_env_file(path: Path, updates: dict[str, str]) -> Path:
    """Safely update .env with `export KEY="value"` syntax and 0o600 permissions."""
    existing = parse_env_file(path)
    for key, val in updates.items():
        if val is None or val == "":
            existing.pop(key, None)
        else:
            existing[key] = str(val)

    ordered_keys = [k for k in ENV_KEYS if k in existing]
    for key in sorted(existing):
        if key not in ordered_keys:
            ordered_keys.append(key)

    lines = []
    for key in ordered_keys:
        val = existing[key]
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'export {key}="{escaped}"')

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, ("\n".join(lines) + "\n") if lines else "", mode=0o600)
    return path


def detect_provider(model: str) -> str:
    lowered = (model or "").lower()
    if lowered.startswith(("ollama/", "ollama_chat/")):
        return "ollama"
    if lowered.startswith("gemini-"):
        return "gemini"
    if lowered.startswith("anthropic/"):
        return "anthropic"
    if lowered.startswith("openrouter/"):
        return "openrouter"
    if lowered.startswith("groq/"):
        return "groq"
    return "openai-compatible"


def format_model_id(provider: str, raw_model: str) -> str:
    cleaned = (raw_model or "").strip()
    if not cleaned:
        return ""
    if provider == "gemini":
        return cleaned.removeprefix("gemini/")
    if provider == "ollama":
        if cleaned.startswith(("ollama_chat/", "ollama/")):
            return cleaned
        return f"ollama_chat/{cleaned}"
    if provider == "openai-compatible":
        return cleaned if cleaned.startswith("openai/") else f"openai/{cleaned}"
    prefix = f"{provider}/"
    return cleaned if cleaned.startswith(prefix) else f"{prefix}{cleaned}"


def _normalize_http_url(raw: str) -> str:
    cleaned = (raw or "").strip().rstrip("/")
    if not cleaned:
        return ""
    if cleaned.startswith("http:") and not cleaned.startswith("http://"):
        cleaned = "http://" + cleaned[len("http:") :].lstrip("/")
    elif cleaned.startswith("https:") and not cleaned.startswith("https://"):
        cleaned = "https://" + cleaned[len("https:") :].lstrip("/")
    elif not cleaned.startswith(("http://", "https://")):
        cleaned = "http://" + cleaned
    # Strip endpoint suffixes if user pasted full /models or /chat/completions URL.
    for suffix in ("/chat/completions", "/completions", "/models", "/api/chat", "/api/tags"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].rstrip("/")
            break
    return cleaned


def _ssl_context() -> ssl.SSLContext:
    """Build a verified SSLContext that works on macOS python.org builds without SSL_CERT_FILE."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _http_post_json(
    url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float = 60.0
) -> tuple[int, dict[str, Any] | str]:
    body = json.dumps(payload).encode("utf8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "adk-coder/0.1",
            **headers,
        },
        method="POST",
    )
    ctx = _ssl_context() if url.startswith("https://") else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read(64_000).decode("utf8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        err_body = exc.read(8_000).decode("utf8", errors="replace")
        return exc.code, err_body


def check_connectivity(
    *,
    provider: str,
    model: str,
    base_url: str = "",
    api_key: str = "",
    api_token: str = "",
    poster=_http_post_json,
) -> dict[str, Any]:
    """Run a live model connectivity + authentication check and return structured diagnostics."""
    start = time.monotonic()
    full_model = format_model_id(provider, model)
    if not full_model:
        return {
            "ok": False,
            "latency_ms": 0,
            "message": "Model name is required.",
            "hint": "Enter a model ID (or click Discover Models).",
        }

    try:
        if provider == "openai-compatible":
            root = _normalize_http_url(base_url) or "https://api.openai.com/v1"
            url = f"{root}/chat/completions"
            bare_model = full_model.removeprefix("openai/")
            token = api_token or api_key or "local"
            status, data = poster(
                url,
                {
                    "model": bare_model,
                    "messages": [{"role": "user", "content": "Reply with OK"}],
                    "max_tokens": 16,
                },
                {"Authorization": f"Bearer {token}"},
                60.0,
            )
            ms = int((time.monotonic() - start) * 1000)
            if 200 <= status < 300 and isinstance(data, dict):
                choices = data.get("choices") or []
                reply = ""
                if choices and isinstance(choices[0], dict):
                    msg = choices[0].get("message") or {}
                    reply = str(msg.get("content") or msg.get("reasoning") or "").strip()
                return {
                    "ok": True,
                    "status": status,
                    "latency_ms": ms,
                    "endpoint": url,
                    "model": full_model,
                    "reply": scrub(reply[:120] or "OK"),
                    "message": f"Connected to {root} ({ms} ms).",
                }
            return _error_result(status, data, url, ms)

        if provider == "ollama":
            root = _normalize_http_url(base_url) or "http://127.0.0.1:11434"
            url = f"{root}/api/chat"
            bare_model = re.sub(r"^ollama(?:_chat)?/", "", full_model)
            headers = {"Authorization": f"Bearer {api_token}"} if api_token else {}
            status, data = poster(
                url,
                {
                    "model": bare_model,
                    "messages": [{"role": "user", "content": "Reply with OK"}],
                    "stream": False,
                    "options": {"num_predict": 16},
                },
                headers,
                60.0,
            )
            ms = int((time.monotonic() - start) * 1000)
            if 200 <= status < 300 and isinstance(data, dict):
                reply = str((data.get("message") or {}).get("content") or "").strip()
                return {
                    "ok": True,
                    "status": status,
                    "latency_ms": ms,
                    "endpoint": url,
                    "model": full_model,
                    "reply": scrub(reply[:120] or "OK"),
                    "message": f"Connected to Ollama at {root} ({ms} ms).",
                }
            return _error_result(status, data, url, ms)

        if provider == "gemini":
            key = api_key or api_token
            if not key:
                return {
                    "ok": False,
                    "latency_ms": 0,
                    "message": "GOOGLE_API_KEY is required for Gemini.",
                    "hint": "Paste your Gemini API key in the API Key field.",
                }
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{full_model}:generateContent?key={key}"
            )
            status, data = poster(
                url,
                {
                    "contents": [{"parts": [{"text": "Reply with OK"}]}],
                    "generationConfig": {"maxOutputTokens": 16},
                },
                {},
                15.0,
            )
            ms = int((time.monotonic() - start) * 1000)
            if 200 <= status < 300 and isinstance(data, dict):
                reply = ""
                cands = data.get("candidates") or []
                if cands:
                    parts = ((cands[0].get("content") or {}).get("parts")) or []
                    reply = "".join(str(p.get("text", "")) for p in parts).strip()
                return {
                    "ok": True,
                    "status": status,
                    "latency_ms": ms,
                    "endpoint": "https://generativelanguage.googleapis.com",
                    "model": full_model,
                    "reply": scrub(reply[:120] or "OK"),
                    "message": f"Connected to Google Gemini ({ms} ms).",
                }
            return _error_result(status, data, "generativelanguage.googleapis.com", ms)

        if provider == "anthropic":
            key = api_key or api_token
            if not key:
                return {
                    "ok": False,
                    "latency_ms": 0,
                    "message": "ANTHROPIC_API_KEY is required.",
                    "hint": "Provide your Anthropic API key.",
                }
            root = _normalize_http_url(base_url) or "https://api.anthropic.com"
            url = f"{root}/v1/messages"
            bare_model = full_model.removeprefix("anthropic/")
            status, data = poster(
                url,
                {
                    "model": bare_model,
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "Reply with OK"}],
                },
                {"x-api-key": key, "anthropic-version": "2023-06-01"},
                15.0,
            )
            ms = int((time.monotonic() - start) * 1000)
            if 200 <= status < 300:
                return {
                    "ok": True,
                    "status": status,
                    "latency_ms": ms,
                    "endpoint": url,
                    "model": full_model,
                    "reply": "OK",
                    "message": f"Connected to Anthropic ({ms} ms).",
                }
            return _error_result(status, data, url, ms)

        # OpenRouter / Groq (OpenAI-compatible chat completions)
        default_bases = {
            "openrouter": "https://openrouter.ai/api/v1",
            "groq": "https://api.groq.com/openai/v1",
        }
        root = _normalize_http_url(base_url) or default_bases.get(provider, "")
        url = f"{root}/chat/completions"
        bare_model = full_model.removeprefix(f"{provider}/")
        token = api_token or api_key
        if not token:
            return {
                "ok": False,
                "latency_ms": 0,
                "message": f"API key is required for {provider}.",
            }
        status, data = poster(
            url,
            {
                "model": bare_model,
                "messages": [{"role": "user", "content": "Reply with OK"}],
                "max_tokens": 16,
            },
            {"Authorization": f"Bearer {token}"},
            15.0,
        )
        ms = int((time.monotonic() - start) * 1000)
        if 200 <= status < 300:
            return {
                "ok": True,
                "status": status,
                "latency_ms": ms,
                "endpoint": url,
                "model": full_model,
                "reply": "OK",
                "message": f"Connected to {provider} ({ms} ms).",
            }
        return _error_result(status, data, url, ms)
    except (OSError, urllib.error.URLError) as exc:
        ms = int((time.monotonic() - start) * 1000)
        return {
            "ok": False,
            "latency_ms": ms,
            "message": f"Connection failed: {scrub(str(exc))}",
            "hint": "Verify the host/IP, port, and that the local or remote model server is running.",
        }


def _error_result(status: int, data: Any, endpoint: str, ms: int) -> dict[str, Any]:
    detail = scrub(json.dumps(data) if isinstance(data, dict) else str(data))[:240]
    hint = "Check the model ID and endpoint URL."
    if status in (401, 403):
        hint = "Authentication failed. Verify your API key / bearer token."
    elif status == 404:
        hint = "Endpoint or model not found. Verify base URL ends with `/v1` (OpenAI) or pull the model (Ollama)."
    return {
        "ok": False,
        "status": status,
        "latency_ms": ms,
        "endpoint": endpoint,
        "message": f"HTTP {status}: {detail}",
        "hint": hint,
    }


def load_ui_state(env_path: Path, workspace: Path | None = None) -> dict[str, Any]:
    env_vals = parse_env_file(env_path)
    merged = {k: env_vals.get(k, os.environ.get(k, "")) for k in ENV_KEYS}

    ws_cfg: dict[str, Any] = {}
    if workspace and (workspace / ".agent/config.json").is_file():
        try:
            ws_cfg = json.loads((workspace / ".agent/config.json").read_text(encoding="utf8"))
        except (OSError, json.JSONDecodeError):
            ws_cfg = {}

    model = ws_cfg.get("model") or merged.get("ADK_CODER_MODEL") or "gemini-2.5-flash"
    provider = detect_provider(model)

    if provider == "ollama":
        base_url = ws_cfg.get("ollama_api_base") or merged.get("OLLAMA_API_BASE") or merged.get("OLLAMA_HOST") or "http://127.0.0.1:11434"
        api_key = ""
        api_token = merged.get("ADK_CODER_ENDPOINT_API_KEY") or ""
    elif provider == "gemini":
        base_url = ""
        api_key = merged.get("GOOGLE_API_KEY") or ""
        api_token = ""
    elif provider == "anthropic":
        base_url = ""
        api_key = merged.get("ANTHROPIC_API_KEY") or ""
        api_token = ""
    elif provider == "openrouter":
        base_url = "https://openrouter.ai/api/v1"
        api_key = merged.get("OPENROUTER_API_KEY") or ""
        api_token = ""
    elif provider == "groq":
        base_url = "https://api.groq.com/openai/v1"
        api_key = merged.get("GROQ_API_KEY") or ""
        api_token = ""
    else:
        base_url = ws_cfg.get("openai_api_base") or merged.get("OPENAI_API_BASE") or merged.get("OPENAI_BASE_URL") or ""
        api_key = merged.get("OPENAI_API_KEY") or ""
        api_token = merged.get("ADK_CODER_ENDPOINT_API_KEY") or ""

    return {
        "env_path": str(env_path),
        "workspace": str(workspace) if workspace else "",
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "api_token": api_token,
        "max_tokens": ws_cfg.get("max_tokens", 8192),
        "context_window": ws_cfg.get("context_window", ""),
        "mode": ws_cfg.get("mode") or merged.get("ADK_CODER_MODE") or "ask",
        "test_command": ws_cfg.get("test_command", ""),
        "sandbox_shell": bool(ws_cfg.get("sandbox_shell", False)),
        "notifications": bool(ws_cfg.get("notifications", False)),
    }


def save_ui_state(env_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    provider = str(payload.get("provider") or "openai-compatible")
    if provider not in PROVIDERS:
        raise ValueError(f"Unsupported provider: {provider}")
    full_model = format_model_id(provider, str(payload.get("model") or ""))
    if not full_model:
        raise ValueError("Model ID is required.")
    base_url = _normalize_http_url(str(payload.get("base_url") or ""))
    api_key = str(payload.get("api_key") or "").strip()
    api_token = str(payload.get("api_token") or "").strip()

    mode = str(payload.get("mode") or "ask").strip().lower()
    if mode not in {"ask", "auto-edit", "plan", "full-auto"}:
        mode = "ask"

    env_updates: dict[str, str] = {
        "ADK_CODER_MODEL": full_model,
        "ADK_CODER_MODE": mode,
    }
    if provider == "openai-compatible":
        env_updates["OPENAI_API_BASE"] = base_url
        env_updates["OPENAI_BASE_URL"] = base_url
        env_updates["OPENAI_API_KEY"] = api_key or "local"
        env_updates["ADK_CODER_ENDPOINT_API_KEY"] = api_token
    elif provider == "ollama":
        env_updates["OLLAMA_HOST"] = base_url or "http://127.0.0.1:11434"
        env_updates["OLLAMA_API_BASE"] = base_url or "http://127.0.0.1:11434"
        if api_token:
            env_updates["ADK_CODER_ENDPOINT_API_KEY"] = api_token
    elif provider == "gemini":
        env_updates["GOOGLE_API_KEY"] = api_key
        env_updates["GOOGLE_GENAI_USE_VERTEXAI"] = "FALSE"
    elif provider == "anthropic":
        env_updates["ANTHROPIC_API_KEY"] = api_key
    elif provider == "openrouter":
        env_updates["OPENROUTER_API_KEY"] = api_key
    elif provider == "groq":
        env_updates["GROQ_API_KEY"] = api_key

    write_env_file(env_path, env_updates)

    saved_ws = ""
    ws_raw = str(payload.get("workspace") or "").strip()
    if ws_raw and payload.get("sync_workspace", True):
        ws_path = Path(ws_raw).expanduser().resolve()
        if ws_path.is_dir():
            state_dir = ws_path / ".agent"
            state_dir.mkdir(mode=0o700, exist_ok=True)
            cfg_path = state_dir / "config.json"
            cfg: dict[str, Any] = {}
            if cfg_path.is_file():
                try:
                    cfg = json.loads(cfg_path.read_text(encoding="utf8"))
                except (OSError, json.JSONDecodeError):
                    cfg = {}
            cfg["model"] = full_model
            cfg["workspace_root"] = str(ws_path)
            if provider == "openai-compatible" and base_url:
                cfg["openai_api_base"] = base_url
            elif provider != "openai-compatible":
                cfg.pop("openai_api_base", None)
            if provider == "ollama" and base_url:
                cfg["ollama_api_base"] = base_url
            elif provider != "ollama":
                cfg.pop("ollama_api_base", None)

            max_tok = payload.get("max_tokens")
            if str(max_tok).isdecimal() and int(max_tok) >= 256:
                cfg["max_tokens"] = int(max_tok)
            ctx_win = payload.get("context_window")
            if str(ctx_win).isdecimal() and int(ctx_win) >= 1024:
                cfg["context_window"] = int(ctx_win)
            elif ctx_win in ("", None):
                cfg.pop("context_window", None)

            cfg["mode"] = mode
            test_cmd = str(payload.get("test_command") or "").strip()
            if test_cmd:
                cfg["test_command"] = test_cmd
            elif "test_command" in payload:
                cfg.pop("test_command", None)
            cfg["sandbox_shell"] = bool(payload.get("sandbox_shell", False))
            cfg["notifications"] = bool(payload.get("notifications", False))

            atomic_write(cfg_path, json.dumps(cfg, indent=2) + "\n", mode=0o600)
            saved_ws = str(cfg_path)

    return {
        "ok": True,
        "env_path": str(env_path),
        "workspace_config": saved_ws,
        "model": full_model,
    }


HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>ADK Coder — Settings</title>
<style>
  :root {
    --bg: #f6f8fc;
    --surface: #ffffff;
    --surface-hover: #edf2fa;
    --pill-active: #d3e3fd;
    --pill-active-text: #041e49;
    --border: #e0e3eb;
    --border-focus: #0b57d0;
    --text: #1f1f1f;
    --muted: #5f6368;
    --primary: #0b57d0;
    --primary-hover: #0842a0;
    --tonal-bg: #c2e7ff;
    --tonal-text: #001d35;
    --ok-bg: #e6f4ea;
    --ok-text: #137333;
    --ok-border: #ceead6;
    --err-bg: #fce8e6;
    --err-text: #b3261e;
    --err-border: #f6aea9;
    --info-bg: #e8f0fe;
    --info-text: #174ea6;
    --info-border: #d2e3fc;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: "Google Sans", Roboto, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
  }
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 24px;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
    gap: 12px;
  }
  .brand-row { display: flex; align-items: center; gap: 10px; }
  .logo-mark {
    width: 32px; height: 32px; border-radius: 10px;
    background: var(--surface); border: 1px solid var(--border);
    display: grid; place-items: center; font-weight: 700; font-size: 14px;
    color: var(--primary);
  }
  .brand-title { font-size: 18px; font-weight: 500; letter-spacing: -0.01em; color: var(--text); }
  .brand-title span { color: var(--muted); font-weight: 400; font-size: 14px; margin-left: 6px; }
  .env-chip {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px; color: var(--muted);
    background: var(--surface); border: 1px solid var(--border);
    padding: 5px 12px; border-radius: 16px; max-width: 420px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .layout {
    max-width: 980px;
    margin: 20px auto;
    padding: 0 20px 32px;
    display: grid;
    grid-template-columns: 220px 1fr;
    gap: 20px;
    align-items: start;
  }
  .rail { padding-top: 4px; }
  .rail-title {
    font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.08em; color: var(--muted);
    padding: 0 14px 8px;
  }
  .prov-btn {
    width: 100%;
    text-align: left;
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: transparent;
    border: none;
    color: var(--text);
    padding: 10px 16px;
    margin-bottom: 2px;
    border-radius: 20px;
    cursor: pointer;
    font-size: 14px;
    font-weight: 400;
    transition: background .12s;
  }
  .prov-btn:hover { background: var(--surface-hover); }
  .prov-btn.active {
    background: var(--pill-active);
    color: var(--pill-active-text);
    font-weight: 600;
  }
  .prov-dot {
    width: 7px; height: 7px; border-radius: 50%;
    background: transparent;
  }
  .prov-btn.active .prov-dot { background: var(--primary); }
  .main-col { display: flex; flex-direction: column; gap: 16px; }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 22px 24px;
  }
  .card-head {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 16px;
  }
  .card h2 {
    margin: 0; font-size: 16px; font-weight: 500; color: var(--text);
  }
  .card-sub { font-size: 12px; color: var(--muted); }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  .full { grid-column: 1 / -1; }
  label {
    display: block; font-size: 12px; font-weight: 500;
    color: var(--muted); margin-bottom: 6px;
  }
  input, select {
    width: 100%;
    background: var(--surface);
    border: 1px solid #c4c7c5;
    color: var(--text);
    padding: 9px 12px;
    border-radius: 8px;
    font-size: 14px;
    outline: none;
    transition: border-color .12s, box-shadow .12s;
  }
  input:focus, select:focus {
    border-color: var(--border-focus);
    box-shadow: 0 0 0 2px rgba(11, 87, 208, 0.14);
  }
  .row { display: flex; gap: 8px; align-items: center; }
  .row input, .row select { flex: 1; }
  .btn {
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    padding: 9px 16px;
    border-radius: 20px;
    cursor: pointer;
    font-weight: 500;
    font-size: 13px;
    white-space: nowrap;
    transition: background .12s, border-color .12s;
  }
  .btn:hover { background: var(--surface-hover); }
  .btn.primary {
    background: var(--primary);
    border-color: var(--primary);
    color: #fff;
  }
  .btn.primary:hover { background: var(--primary-hover); }
  .btn.tonal {
    background: var(--tonal-bg);
    border-color: transparent;
    color: var(--tonal-text);
  }
  .btn.tonal:hover { filter: brightness(0.96); }
  .btn:disabled { opacity: 0.55; cursor: wait; }
  .actions {
    display: flex; gap: 10px; flex-wrap: wrap;
    align-items: center; margin-top: 18px;
  }
  .check-row { display: flex; gap: 20px; flex-wrap: wrap; margin-top: 4px; }
  .check {
    display: inline-flex; align-items: center; gap: 8px;
    font-size: 13px; color: var(--text); cursor: pointer;
  }
  .check input { width: 15px; height: 15px; accent-color: var(--primary); }
  .status-box {
    margin-top: 14px; padding: 12px 14px; border-radius: 12px;
    font-size: 13px; display: none; line-height: 1.5; border: 1px solid transparent;
  }
  .status-box.ok { display: block; background: var(--ok-bg); color: var(--ok-text); border-color: var(--ok-border); }
  .status-box.err { display: block; background: var(--err-bg); color: var(--err-text); border-color: var(--err-border); }
  .status-box.info { display: block; background: var(--info-bg); color: var(--info-text); border-color: var(--info-border); }
  code {
    background: rgba(0, 0, 0, 0.05);
    padding: 2px 6px; border-radius: 6px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
  }
  @media (max-width: 760px) {
    .layout { grid-template-columns: 1fr; }
    .rail { display: flex; gap: 6px; flex-wrap: wrap; }
    .rail-title { width: 100%; }
    .prov-btn { width: auto; }
    .grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<header class="topbar">
  <div class="brand-row">
    <div class="logo-mark">A</div>
    <div class="brand-title">ADK Coder <span>Settings</span></div>
  </div>
  <div class="env-chip" id="envPathBadge" title="Configuration file">.env</div>
</header>

<div class="layout">
  <nav class="rail" aria-label="LLM Providers">
    <div class="rail-title">Provider</div>
    <button type="button" class="prov-btn" data-p="openai-compatible"><span>OpenAI / Local</span><span class="prov-dot"></span></button>
    <button type="button" class="prov-btn" data-p="ollama"><span>Ollama</span><span class="prov-dot"></span></button>
    <button type="button" class="prov-btn" data-p="gemini"><span>Google Gemini</span><span class="prov-dot"></span></button>
    <button type="button" class="prov-btn" data-p="anthropic"><span>Anthropic</span><span class="prov-dot"></span></button>
    <button type="button" class="prov-btn" data-p="openrouter"><span>OpenRouter</span><span class="prov-dot"></span></button>
    <button type="button" class="prov-btn" data-p="groq"><span>Groq</span><span class="prov-dot"></span></button>
  </nav>

  <main class="main-col">
    <section class="card">
      <div class="card-head">
        <h2>Model &amp; Connection</h2>
        <span class="card-sub">Lightweight local configuration</span>
      </div>
      <div class="grid">
        <div class="full">
          <label for="base_url">Base URL</label>
          <input id="base_url" placeholder="https://your-endpoint/v1 or http://127.0.0.1:11434" />
        </div>

        <div class="full">
          <label for="model">Model ID</label>
          <div class="row">
            <input id="model" placeholder="e.g. hf.co/ornith-ai/... or qwen2.5-coder:14b or gemini-2.5-flash" />
            <button type="button" class="btn tonal" id="discoverBtn">Discover</button>
          </div>
          <select id="discoveredSelect" style="margin-top:8px; display:none;"></select>
        </div>

        <div>
          <label for="api_key">API Key</label>
          <div class="row">
            <input id="api_key" type="password" placeholder="local, sk-..., or AIza..." />
            <button type="button" class="btn" onclick="toggleSecret('api_key', this)">Show</button>
          </div>
        </div>

        <div>
          <label for="api_token">Endpoint Token (optional)</label>
          <div class="row">
            <input id="api_token" type="password" placeholder="Bearer token for custom endpoint" />
            <button type="button" class="btn" onclick="toggleSecret('api_token', this)">Show</button>
          </div>
        </div>
      </div>

      <div class="actions">
        <button type="button" class="btn tonal" id="testBtn">Test connection</button>
        <button type="button" class="btn primary" id="saveBtn">Save settings</button>
      </div>
      <div id="testResult" class="status-box"></div>
      <div id="saveResult" class="status-box"></div>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>Workspace Preferences</h2>
        <span class="card-sub">Optional per-project sync</span>
      </div>
      <div class="grid">
        <div class="full">
          <label for="workspace">Workspace directory (syncs .agent/config.json)</label>
          <input id="workspace" placeholder="/path/to/your/project (leave blank for global .env only)" />
        </div>
        <div>
          <label for="mode">Execution mode</label>
          <select id="mode">
            <option value="ask">Ask before edits &amp; shell</option>
            <option value="auto-edit">Auto-approve edits</option>
            <option value="plan">Plan (read-only)</option>
            <option value="full-auto">Full auto</option>
          </select>
        </div>
        <div>
          <label for="max_tokens">Max output tokens</label>
          <input id="max_tokens" type="number" min="256" step="256" value="8192" />
        </div>
        <div>
          <label for="context_window">Context window (blank = auto)</label>
          <input id="context_window" type="number" min="1024" placeholder="Auto" />
        </div>
        <div>
          <label for="test_command">Test command</label>
          <input id="test_command" placeholder="mvn -B test or python -m pytest" />
        </div>
        <div class="full check-row">
          <label class="check"><input id="sync_workspace" type="checkbox" checked /> Sync workspace config</label>
          <label class="check"><input id="sandbox_shell" type="checkbox" /> macOS shell sandbox</label>
          <label class="check"><input id="notifications" type="checkbox" /> Desktop alerts</label>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>Saved Profiles &amp; Auto-Fallback</h2>
        <span class="card-sub">Switch endpoints in 1 click or fail over automatically</span>
      </div>
      <div class="grid">
        <div class="full">
          <label for="profileName">Save current form as profile</label>
          <div class="row">
            <input id="profileName" placeholder="e.g. Local Ornith, Mac Mini Mellum, Gemini Pro" />
            <button type="button" class="btn tonal" id="saveProfileBtn">Save profile</button>
          </div>
        </div>
        <div class="full">
          <label for="fallbackSelect">Auto-fallback profile (used if primary server is unreachable)</label>
          <div class="row">
            <select id="fallbackSelect"><option value="">None (disabled)</option></select>
            <button type="button" class="btn" id="setFallbackBtn">Set fallback</button>
          </div>
        </div>
        <div class="full" id="profilesList" style="display:flex; flex-direction:column; gap:8px;"></div>
      </div>
      <div id="profileResult" class="status-box"></div>
    </section>
  </main>
</div>

<script>
let currentProvider = 'openai-compatible';
const DEFAULTS = {
  'openai-compatible': { base: 'http://127.0.0.1:8080/v1', model: 'openai/gpt-4o-mini' },
  'ollama':            { base: 'http://127.0.0.1:11434', model: 'ollama_chat/qwen2.5-coder:14b' },
  'gemini':            { base: '', model: 'gemini-2.5-flash' },
  'anthropic':         { base: 'https://api.anthropic.com', model: 'anthropic/claude-sonnet-4-20250514' },
  'openrouter':        { base: 'https://openrouter.ai/api/v1', model: 'openrouter/openai/gpt-4o-mini' },
  'groq':              { base: 'https://api.groq.com/openai/v1', model: 'groq/llama-3.3-70b-versatile' },
};

function esc(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function toggleSecret(id, btn) {
  const el = document.getElementById(id);
  el.type = el.type === 'password' ? 'text' : 'password';
  btn.textContent = el.type === 'password' ? 'Show' : 'Hide';
}

function setProvider(p, applyDefaults) {
  currentProvider = p;
  document.querySelectorAll('.prov-btn').forEach(b => b.classList.toggle('active', b.dataset.p === p));
  if (applyDefaults && DEFAULTS[p]) {
    document.getElementById('base_url').value = DEFAULTS[p].base;
    document.getElementById('model').value = DEFAULTS[p].model;
  }
}

document.querySelectorAll('.prov-btn').forEach(b => {
  b.addEventListener('click', () => setProvider(b.dataset.p, true));
});

function formPayload() {
  return {
    provider: currentProvider,
    base_url: document.getElementById('base_url').value.trim(),
    model: document.getElementById('model').value.trim(),
    api_key: document.getElementById('api_key').value.trim(),
    api_token: document.getElementById('api_token').value.trim(),
    workspace: document.getElementById('workspace').value.trim(),
    mode: document.getElementById('mode').value,
    max_tokens: document.getElementById('max_tokens').value.trim(),
    context_window: document.getElementById('context_window').value.trim(),
    test_command: document.getElementById('test_command').value.trim(),
    sync_workspace: document.getElementById('sync_workspace').checked,
    sandbox_shell: document.getElementById('sandbox_shell').checked,
    notifications: document.getElementById('notifications').checked,
  };
}

function showBox(id, kind, html) {
  const box = document.getElementById(id);
  box.className = 'status-box ' + kind;
  box.innerHTML = html;
}

async function loadState() {
  const res = await fetch('/api/state');
  const s = await res.json();
  document.getElementById('envPathBadge').textContent = s.env_path;
  setProvider(s.provider || 'openai-compatible', false);
  document.getElementById('base_url').value = s.base_url || '';
  document.getElementById('model').value = s.model || '';
  document.getElementById('api_key').value = s.api_key || '';
  document.getElementById('api_token').value = s.api_token || '';
  document.getElementById('workspace').value = s.workspace || '';
  document.getElementById('mode').value = s.mode || 'ask';
  document.getElementById('max_tokens').value = s.max_tokens || 8192;
  document.getElementById('context_window').value = s.context_window || '';
  document.getElementById('test_command').value = s.test_command || '';
  document.getElementById('sandbox_shell').checked = !!s.sandbox_shell;
  document.getElementById('notifications').checked = !!s.notifications;
}

document.getElementById('discoverBtn').addEventListener('click', async () => {
  const btn = document.getElementById('discoverBtn');
  btn.disabled = true;
  try {
    const res = await fetch('/api/discover', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(formPayload())
    });
    const data = await res.json();
    const sel = document.getElementById('discoveredSelect');
    sel.innerHTML = '';
    (data.models || []).forEach(m => {
      const opt = document.createElement('option');
      opt.value = m.model;
      opt.textContent = `[${m.source}] ${m.model}${m.note ? ' — ' + m.note : ''}`;
      sel.appendChild(opt);
    });
    if (sel.options.length) {
      sel.style.display = 'block';
      sel.onchange = () => { document.getElementById('model').value = sel.value; };
    }
  } finally { btn.disabled = false; }
});

document.getElementById('testBtn').addEventListener('click', async () => {
  const btn = document.getElementById('testBtn');
  btn.disabled = true;
  showBox('testResult', 'info', 'Testing connection &amp; sending live ping...');
  try {
    const res = await fetch('/api/test', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(formPayload())
    });
    const r = await res.json();
    if (r.ok) {
      showBox('testResult', 'ok',
        `<strong>Connected</strong> — ${esc(r.message)}<br/>Model: <code>${esc(r.model)}</code> · Reply: <code>${esc(r.reply)}</code>`);
    } else {
      showBox('testResult', 'err',
        `<strong>Connection failed</strong> (${r.latency_ms || 0} ms) — ${esc(r.message)}` +
        (r.hint ? `<br/>Hint: ${esc(r.hint)}` : ''));
    }
  } catch (e) {
    showBox('testResult', 'err', 'Request error: ' + esc(e));
  } finally { btn.disabled = false; }
});

document.getElementById('saveBtn').addEventListener('click', async () => {
  const btn = document.getElementById('saveBtn');
  btn.disabled = true;
  try {
    const res = await fetch('/api/save', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(formPayload())
    });
    const r = await res.json();
    if (r.ok) {
      showBox('saveResult', 'ok',
        `<strong>Saved</strong> — <code>${esc(r.model)}</code> written to <code>${esc(r.env_path)}</code>` +
        (r.workspace_config ? ` and <code>${esc(r.workspace_config)}</code>` : '') + '.');
    } else {
      showBox('saveResult', 'err', `<strong>Error</strong> — ${esc(r.error)}`);
    }
  } catch (e) {
    showBox('saveResult', 'err', 'Save error: ' + esc(e));
  } finally { btn.disabled = false; }
});

async function loadProfiles() {
  try {
    const res = await fetch('/api/profiles');
    const data = await res.json();
    const list = document.getElementById('profilesList');
    const fbSel = document.getElementById('fallbackSelect');
    const names = Object.keys(data.profiles || {}).sort();
    fbSel.innerHTML = '<option value="">None (disabled)</option>' +
      names.map(n => `<option value="${esc(n)}" ${n === data.fallback ? 'selected' : ''}>${esc(n)}</option>`).join('');
    if (!names.length) {
      list.innerHTML = '<div style="font-size:13px; color:var(--muted);">No saved profiles yet. Enter a name above to save your current configuration.</div>';
      return;
    }
    list.innerHTML = names.map(n => {
      const p = data.profiles[n];
      const badges = [];
      if (n === data.active) badges.push('<span style="background:var(--pill-active);color:var(--pill-active-text);padding:2px 8px;border-radius:999px;font-size:11px;font-weight:500;">Active</span>');
      if (n === data.fallback) badges.push('<span style="background:var(--ok-bg);color:var(--ok-text);padding:2px 8px;border-radius:999px;font-size:11px;font-weight:500;">Fallback</span>');
      return `<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 12px;border:1px solid var(--border);border-radius:12px;">
        <div style="min-width:0;">
          <div style="font-size:14px;font-weight:500;display:flex;align-items:center;gap:6px;">${esc(n)} ${badges.join(' ')}</div>
          <div style="font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;"><code>${esc(p.model)}</code> · ${esc(p.provider)} · mode: ${esc(p.mode || 'ask')}</div>
        </div>
        <div style="display:flex;gap:6px;flex-shrink:0;">
          <button type="button" class="btn tonal" data-use-profile="${esc(n)}">Use</button>
          <button type="button" class="btn" data-delete-profile="${esc(n)}">Delete</button>
        </div>
      </div>`;
    }).join('');
    list.querySelectorAll('[data-use-profile]').forEach(btn => {
      btn.addEventListener('click', () => window.useProfile(btn.dataset.useProfile));
    });
    list.querySelectorAll('[data-delete-profile]').forEach(btn => {
      btn.addEventListener('click', () => window.deleteProfile(btn.dataset.deleteProfile));
    });
  } catch (_) {}
}

window.useProfile = async function(name) {
  const res = await fetch('/api/profiles/use', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ name, workspace: document.getElementById('workspace').value.trim() })
  });
  const r = await res.json();
  if (r.ok) {
    showBox('profileResult', 'ok', `Activated profile <strong>${esc(r.name)}</strong> (<code>${esc(r.profile.model)}</code>).`);
    await loadState();
    await loadProfiles();
  } else {
    showBox('profileResult', 'err', esc(r.error || 'Failed to activate profile'));
  }
};

window.deleteProfile = async function(name) {
  const res = await fetch('/api/profiles/delete', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ name })
  });
  const r = await res.json();
  if (r.ok) {
    showBox('profileResult', 'info', `Deleted profile <strong>${esc(name)}</strong>.`);
    await loadProfiles();
  } else {
    showBox('profileResult', 'err', esc(r.error || 'Delete failed'));
  }
};

document.getElementById('saveProfileBtn').addEventListener('click', async () => {
  const name = document.getElementById('profileName').value.trim();
  if (!name) { showBox('profileResult', 'err', 'Enter a profile name first.'); return; }
  const res = await fetch('/api/profiles/save', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ ...formPayload(), name })
  });
  const r = await res.json();
  if (r.ok) {
    document.getElementById('profileName').value = '';
    showBox('profileResult', 'ok', `Saved profile <strong>${esc(r.name)}</strong> (<code>${esc(r.profile.model)}</code>).`);
    await loadProfiles();
  } else {
    showBox('profileResult', 'err', esc(r.error || 'Save failed'));
  }
});

document.getElementById('setFallbackBtn').addEventListener('click', async () => {
  const name = document.getElementById('fallbackSelect').value;
  const res = await fetch('/api/profiles/fallback', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ name })
  });
  const r = await res.json();
  if (r.ok) {
    showBox('profileResult', 'ok', r.fallback ? `Fallback set to <strong>${esc(r.fallback)}</strong>.` : 'Fallback disabled.');
    await loadProfiles();
  } else {
    showBox('profileResult', 'err', esc(r.error || 'Fallback update failed'));
  }
});

loadState();
loadProfiles();
</script>
</body>
</html>
"""


def dispatch_request(
    method: str,
    path: str,
    payload: dict[str, Any],
    env_path: Path,
    workspace: Path | None = None,
    *,
    poster=_http_post_json,
    on_save=None,
) -> tuple[int, str, bytes]:
    """Pure HTTP router for the config UI (unit-testable without sockets)."""
    if method == "GET":
        if path in {"/", "/index.html"}:
            return HTTPStatus.OK, "text/html; charset=utf-8", HTML_PAGE.encode("utf8")
        if path == "/api/state":
            return (
                HTTPStatus.OK,
                "application/json; charset=utf-8",
                json.dumps(load_ui_state(env_path, workspace)).encode("utf8"),
            )
        if path == "/api/profiles":
            from .profiles import masked_profiles_summary

            return (
                HTTPStatus.OK,
                "application/json; charset=utf-8",
                json.dumps(masked_profiles_summary(env_path)).encode("utf8"),
            )
        return (
            HTTPStatus.NOT_FOUND,
            "application/json; charset=utf-8",
            json.dumps({"error": "Not found"}).encode("utf8"),
        )

    if method == "POST":
        try:
            if path == "/api/save":
                data = save_ui_state(env_path, payload)
                if on_save is not None:
                    on_save(data)
            elif path == "/api/test":
                data = check_connectivity(
                    provider=str(payload.get("provider") or "openai-compatible"),
                    model=str(payload.get("model") or ""),
                    base_url=str(payload.get("base_url") or ""),
                    api_key=str(payload.get("api_key") or ""),
                    api_token=str(payload.get("api_token") or ""),
                    poster=poster,
                )
            elif path == "/api/discover":
                prov = str(payload.get("provider") or "")
                base = _normalize_http_url(str(payload.get("base_url") or ""))
                models = discover_models(
                    api_base=base if prov == "openai-compatible" else None,
                    ollama_host=base if prov == "ollama" else None,
                )
                data = {"models": models}
            elif path == "/api/profiles/save":
                from .profiles import save_profile

                res = save_profile(str(payload.get("name") or ""), payload, env_path=env_path)
                data = {"ok": True, "name": res["name"], "profile": {"model": res["profile"]["model"]}}
            elif path == "/api/profiles/use":
                from .profiles import activate_profile

                ws_override = str(payload.get("workspace") or "").strip()
                target_ws = Path(ws_override).expanduser().resolve() if ws_override else workspace
                res = activate_profile(
                    str(payload.get("name") or ""), env_path=env_path, workspace=target_ws
                )
                if on_save is not None:
                    on_save(res["saved"])
                data = {"ok": True, "name": res["name"], "profile": {"model": res["profile"]["model"]}}
            elif path == "/api/profiles/fallback":
                from .profiles import set_fallback_profile

                store = set_fallback_profile(str(payload.get("name") or ""), env_path=env_path)
                data = {"ok": True, "fallback": store["fallback"]}
            elif path == "/api/profiles/delete":
                from .profiles import delete_profile

                delete_profile(str(payload.get("name") or ""), env_path=env_path)
                data = {"ok": True}
            else:
                return (
                    HTTPStatus.NOT_FOUND,
                    "application/json; charset=utf-8",
                    json.dumps({"error": "Not found"}).encode("utf8"),
                )
            return (
                HTTPStatus.OK,
                "application/json; charset=utf-8",
                json.dumps(data).encode("utf8"),
            )
        except Exception as exc:  # noqa: BLE001
            return (
                HTTPStatus.BAD_REQUEST,
                "application/json; charset=utf-8",
                json.dumps(
                    {"ok": False, "error": scrub(str(exc)) or type(exc).__name__}
                ).encode("utf8"),
            )

    return (
        HTTPStatus.METHOD_NOT_ALLOWED,
        "application/json; charset=utf-8",
        json.dumps({"error": "Method not allowed"}).encode("utf8"),
    )


def create_server(
    env_path: Path,
    workspace: Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8089,
    poster=_http_post_json,
    on_save=None,
) -> ThreadingHTTPServer:
    class ConfigHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            return

        def _respond(self, status: int, content_type: str, raw: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            status, ctype, raw = dispatch_request(
                "GET", self.path, {}, env_path, workspace, poster=poster, on_save=on_save
            )
            self._respond(status, ctype, raw)

        def do_POST(self) -> None:
            length = min(int(self.headers.get("Content-Length", "0") or 0), 256_000)
            body = self.rfile.read(length).decode("utf8", errors="replace") if length else "{}"
            try:
                payload = json.loads(body or "{}")
            except json.JSONDecodeError:
                payload = {}
            status, ctype, raw = dispatch_request(
                "POST", self.path, payload, env_path, workspace, poster=poster, on_save=on_save
            )
            self._respond(status, ctype, raw)

    return ThreadingHTTPServer((host, port), ConfigHandler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="adk-coder-config",
        description="Configure ADK Coder LLM provider, endpoint, credentials, and test connectivity.",
    )
    parser.add_argument("--env", type=Path, default=default_env_path(), help="Path to .env file.")
    parser.add_argument("--workspace", type=Path, default=None, help="Optional target workspace.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8089)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run a terminal connectivity check using current config and exit.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically open the browser.",
    )
    args = parser.parse_args(argv)

    env_path = args.env.expanduser().resolve()
    workspace = args.workspace.expanduser().resolve() if args.workspace else None

    if args.check:
        state = load_ui_state(env_path, workspace)
        result = check_connectivity(
            provider=state["provider"],
            model=state["model"],
            base_url=state["base_url"],
            api_key=state["api_key"],
            api_token=state["api_token"],
        )
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1

    server = create_server(env_path, workspace, host=args.host, port=args.port)
    url = f"http://{args.host}:{args.port}"
    print(f"ADK Coder Config UI running at {url}")
    print(f"Env file:  {env_path}")
    if workspace:
        print(f"Workspace: {workspace}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
