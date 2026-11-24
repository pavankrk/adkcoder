"""Dynamic model discovery for OpenAI-compatible servers, Ollama, and Gemini presets."""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from contextlib import suppress

GEMINI_PRESETS = (
    ("gemini", "gemini-2.5-flash", "Fast Gemini 2.5 (1M context)"),
    ("gemini", "gemini-2.5-pro", "Reasoning Gemini 2.5 Pro (1M context)"),
)


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _fetch_json(url: str, headers: dict[str, str] | None = None, timeout: float = 10.0) -> dict:
    if not url.startswith(("http://", "https://")):
        raise ValueError("Unsupported URL scheme.")
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "adk-coder/0.1",
            **(headers or {}),
        },
    )
    ctx = _ssl_context() if url.startswith("https://") else None
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode("utf8", errors="replace"))


def discover_models(
    api_base: str | None = None,
    ollama_host: str | None = None,
    fetcher=_fetch_json,
) -> list[dict[str, str]]:
    """Query configured OpenAI-compatible and Ollama servers, plus standard presets."""
    discovered: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(source: str, model_id: str, note: str = "") -> None:
        if model_id not in seen:
            seen.add(model_id)
            discovered.append({"source": source, "model": model_id, "note": note})

    base = (
        api_base
        or os.environ.get("OPENAI_API_BASE")
        or os.environ.get("OPENAI_BASE_URL")
        or ""
    ).rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models"):
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
            break
    if base:
        key = (
            os.environ.get("ADK_CODER_ENDPOINT_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or "local"
        )
        headers = {"Authorization": f"Bearer {key}"}
        with suppress(OSError, ValueError, urllib.error.URLError):
            payload = fetcher(f"{base}/models", headers=headers, timeout=10.0)
            for item in payload.get("data", []) or []:
                mid = item.get("id") if isinstance(item, dict) else None
                if mid:
                    add("openai-endpoint", f"openai/{mid}", base)

    ollama = (
        ollama_host or os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434"
    ).rstrip("/")
    if not ollama.startswith(("http://", "https://")):
        ollama = f"http://{ollama}"
    with suppress(OSError, ValueError, urllib.error.URLError):
        payload = fetcher(f"{ollama}/api/tags", headers={}, timeout=2.0)
        for item in payload.get("models", []) or []:
            name = item.get("name") if isinstance(item, dict) else None
            if name:
                add("ollama", f"ollama_chat/{name}", ollama)

    env_model = os.environ.get("ADK_CODER_MODEL")
    if env_model:
        add("env", env_model, "ADK_CODER_MODEL")

    for src, mid, note in GEMINI_PRESETS:
        add(src, mid, note)

    return discovered
