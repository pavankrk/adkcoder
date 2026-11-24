"""Named LLM provider profiles and automatic fallback routing."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .config_ui import (
    PROVIDERS,
    default_env_path,
    detect_provider,
    format_model_id,
    load_ui_state,
    save_ui_state,
)
from .scrubber import scrub
from .tools import atomic_write

_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,47}$")


def profiles_path(env_path: Path | None = None) -> Path:
    base_env = env_path or default_env_path()
    return base_env.parent / "profiles.json"


def _empty_store() -> dict[str, Any]:
    return {"active": "", "fallback": "", "profiles": {}}


def load_profiles(env_path: Path | None = None) -> dict[str, Any]:
    path = profiles_path(env_path)
    if not path.is_file():
        return _empty_store()
    try:
        data = json.loads(path.read_text(encoding="utf8"))
    except (OSError, json.JSONDecodeError):
        return _empty_store()
    if not isinstance(data, dict):
        return _empty_store()
    profs = data.get("profiles")
    if not isinstance(profs, dict):
        profs = {}
    return {
        "active": str(data.get("active") or ""),
        "fallback": str(data.get("fallback") or ""),
        "profiles": profs,
    }


def _write_profiles(store: dict[str, Any], env_path: Path | None = None) -> Path:
    path = profiles_path(env_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(store, indent=2) + "\n", mode=0o600)
    return path


def _validate_name(name: str) -> str:
    cleaned = name.strip()
    if not _PROFILE_NAME_RE.match(cleaned):
        raise ValueError(
            "Profile name must start with a letter/digit and contain only letters, digits, spaces, '.', '_', or '-'."
        )
    return cleaned


def save_profile(
    name: str,
    entry: dict[str, Any],
    *,
    env_path: Path | None = None,
    set_active: bool = False,
) -> dict[str, Any]:
    """Save or update a named provider profile."""
    clean_name = _validate_name(name)
    raw_model = str(entry.get("model") or "").strip()
    provider = str(entry.get("provider") or detect_provider(raw_model)).strip()
    if provider not in PROVIDERS:
        raise ValueError(f"Unsupported provider: {provider}")
    full_model = format_model_id(provider, raw_model)
    if not full_model:
        raise ValueError("Model ID is required to save a profile.")

    mode = str(entry.get("mode") or "ask").strip().lower()
    if mode not in {"ask", "auto-edit", "plan", "full-auto"}:
        mode = "ask"

    profile = {
        "provider": provider,
        "model": full_model,
        "base_url": str(entry.get("base_url") or "").strip(),
        "api_key": str(entry.get("api_key") or "").strip(),
        "api_token": str(entry.get("api_token") or "").strip(),
        "mode": mode,
        "max_tokens": int(entry["max_tokens"])
        if str(entry.get("max_tokens", "")).isdecimal() and int(entry["max_tokens"]) >= 256
        else 8192,
        "context_window": int(entry["context_window"])
        if str(entry.get("context_window", "")).isdecimal() and int(entry["context_window"]) >= 1024
        else "",
    }
    store = load_profiles(env_path)
    store["profiles"][clean_name] = profile
    if set_active or not store["active"]:
        store["active"] = clean_name
    _write_profiles(store, env_path)
    return {"name": clean_name, "profile": profile, "store": store}


def delete_profile(name: str, *, env_path: Path | None = None) -> dict[str, Any]:
    clean_name = name.strip()
    store = load_profiles(env_path)
    if clean_name not in store["profiles"]:
        raise ValueError(f"Profile not found: {clean_name}")
    del store["profiles"][clean_name]
    if store["active"] == clean_name:
        store["active"] = ""
    if store["fallback"] == clean_name:
        store["fallback"] = ""
    _write_profiles(store, env_path)
    return store


def set_fallback_profile(name: str, *, env_path: Path | None = None) -> dict[str, Any]:
    clean_name = name.strip()
    store = load_profiles(env_path)
    if clean_name.lower() in {"", "none", "off", "clear"}:
        store["fallback"] = ""
    else:
        if clean_name not in store["profiles"]:
            raise ValueError(f"Fallback profile not found: {clean_name}")
        store["fallback"] = clean_name
    _write_profiles(store, env_path)
    return store


def apply_profile_to_env(profile: dict[str, Any]) -> None:
    """Apply a profile's credentials/model directly into os.environ for live failover."""
    provider = str(profile.get("provider") or "openai-compatible")
    model = str(profile.get("model") or "")
    base_url = str(profile.get("base_url") or "")
    api_key = str(profile.get("api_key") or "")
    api_token = str(profile.get("api_token") or "")

    if model:
        os.environ["ADK_CODER_MODEL"] = model
    if profile.get("mode") in {"ask", "auto-edit", "plan", "full-auto"}:
        os.environ["ADK_CODER_MODE"] = str(profile["mode"])

    if provider == "openai-compatible":
        if base_url:
            os.environ["OPENAI_API_BASE"] = base_url
            os.environ["OPENAI_BASE_URL"] = base_url
        os.environ["OPENAI_API_KEY"] = api_key or "local"
        if api_token:
            os.environ["ADK_CODER_ENDPOINT_API_KEY"] = api_token
        else:
            os.environ.pop("ADK_CODER_ENDPOINT_API_KEY", None)
    elif provider == "ollama":
        host = base_url or "http://127.0.0.1:11434"
        os.environ["OLLAMA_HOST"] = host
        os.environ["OLLAMA_API_BASE"] = host
        if api_token:
            os.environ["ADK_CODER_ENDPOINT_API_KEY"] = api_token
    elif provider == "gemini":
        if api_key:
            os.environ["GOOGLE_API_KEY"] = api_key
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "FALSE"
    elif provider == "anthropic" and api_key:
        os.environ["ANTHROPIC_API_KEY"] = api_key
    elif provider == "openrouter" and api_key:
        os.environ["OPENROUTER_API_KEY"] = api_key
    elif provider == "groq" and api_key:
        os.environ["GROQ_API_KEY"] = api_key


def activate_profile(
    name: str,
    *,
    env_path: Path | None = None,
    workspace: Path | None = None,
) -> dict[str, Any]:
    """Activate a named profile: persist to .env + workspace .agent/config.json + os.environ."""
    clean_name = name.strip()
    resolved_env = env_path or default_env_path()
    store = load_profiles(resolved_env)
    if clean_name not in store["profiles"]:
        raise ValueError(f"Profile not found: {clean_name}")
    prof = dict(store["profiles"][clean_name])

    payload = {
        **prof,
        "workspace": str(workspace) if workspace else "",
        "sync_workspace": bool(workspace),
    }
    res = save_ui_state(resolved_env, payload)
    apply_profile_to_env(prof)
    store["active"] = clean_name
    _write_profiles(store, resolved_env)
    return {"name": clean_name, "profile": prof, "saved": res}


def get_fallback_profile(
    current_model: str = "",
    *,
    env_path: Path | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Return (name, profile) for the configured fallback if usable and distinct from current."""
    store = load_profiles(env_path)
    fb_name = store.get("fallback") or ""
    if not fb_name or fb_name not in store["profiles"]:
        return None
    prof = store["profiles"][fb_name]
    if current_model and prof.get("model") == current_model and fb_name == store.get("active"):
        return None
    return fb_name, prof


def is_transient_provider_error(exc: Exception) -> bool:
    """Return True for network/endpoint/timeout errors that warrant profile failover."""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if any(
        tok in name
        for tok in (
            "connection",
            "timeout",
            "connect",
            "apiconnection",
            "serviceunavailable",
            "internalserver",
            "badgateway",
            "ratelimit",
        )
    ):
        return True
    return any(
        tok in msg
        for tok in (
            "connection refused",
            "failed to connect",
            "cannot connect",
            "name or service not known",
            "nodename nor servname",
            "timed out",
            "502 bad gateway",
            "503 service unavailable",
            "504 gateway timeout",
            "cloudflare",
            "tunnel",
        )
    )


def masked_profiles_summary(env_path: Path | None = None) -> dict[str, Any]:
    """Return profiles store safe for JSON API / UI display (secrets masked)."""
    store = load_profiles(env_path)
    safe_profiles: dict[str, Any] = {}
    for name, prof in store["profiles"].items():
        safe_profiles[name] = {
            "provider": prof.get("provider", "openai-compatible"),
            "model": prof.get("model", ""),
            "base_url": prof.get("base_url", ""),
            "mode": prof.get("mode", "ask"),
            "max_tokens": prof.get("max_tokens", 8192),
            "context_window": prof.get("context_window", ""),
            "has_api_key": bool(prof.get("api_key")),
            "has_api_token": bool(prof.get("api_token")),
        }
    return {
        "active": store["active"],
        "fallback": store["fallback"],
        "profiles": safe_profiles,
    }


def format_profiles_list(env_path: Path | None = None) -> str:
    store = load_profiles(env_path)
    if not store["profiles"]:
        return (
            "No saved profiles yet.\n"
            "Save the current setup with `/profile save <name>` or open `/settings`."
        )
    lines = ["Saved provider profiles:"]
    for name, prof in sorted(store["profiles"].items()):
        tags: list[str] = []
        if name == store["active"]:
            tags.append("active")
        if name == store["fallback"]:
            tags.append("fallback")
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        base = f" @ {prof['base_url']}" if prof.get("base_url") else ""
        lines.append(
            f"  - {name}{tag_str}: {scrub(prof.get('model', ''))} ({prof.get('provider', '')}, mode:{prof.get('mode', 'ask')}){base}"
        )
    return "\n".join(lines)


def handle_profile_command(h, args: list[str]) -> str:
    """Handle `/profile [list|save <name>|use <name>|fallback <name|none>|delete <name>]`."""
    env_path = default_env_path()
    if not args or args[0] == "list":
        return format_profiles_list(env_path)

    sub = args[0].lower()
    rest = " ".join(args[1:]).strip()

    if sub == "save":
        if not rest:
            raise ValueError("Usage: /profile save <name>")
        ui_state = load_ui_state(env_path, h.root)
        ui_state["model"] = h.model
        ui_state["mode"] = h.mode
        res = save_profile(rest, ui_state, env_path=env_path, set_active=True)
        return f"Saved active profile `{res['name']}` ({res['profile']['model']})."

    if sub == "use":
        if not rest:
            raise ValueError("Usage: /profile use <name>")
        res = activate_profile(rest, env_path=env_path, workspace=h.root)
        h.reload_config()
        return (
            f"Switched to profile `{res['name']}` -> model `{res['profile']['model']}` "
            f"(mode: `{res['profile']['mode']}`)."
        )

    if sub == "fallback":
        if not rest:
            store = load_profiles(env_path)
            fb = store.get("fallback") or "(none)"
            return f"Current fallback profile: {fb}\nUsage: /profile fallback <name|none>"
        store = set_fallback_profile(rest, env_path=env_path)
        return (
            f"Fallback profile set to `{store['fallback']}`."
            if store["fallback"]
            else "Fallback profile cleared."
        )

    if sub in {"delete", "rm", "remove"}:
        if not rest:
            raise ValueError("Usage: /profile delete <name>")
        delete_profile(rest, env_path=env_path)
        return f"Deleted profile `{rest}`."

    raise ValueError(
        "Usage: /profile [list|save <name>|use <name>|fallback <name|none>|delete <name>]"
    )
