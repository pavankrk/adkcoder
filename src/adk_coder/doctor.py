"""Workspace and environment health diagnostics (/doctor)."""

from __future__ import annotations

import os
import shutil
import sys

from rich.table import Table
from rich.text import Text

from .config_ui import check_connectivity, detect_provider, parse_env_file
from .spinner import spinning
from .ui import console

TOOL_CHECKS = (
    ("git", "Version control, checkpoints, merge & /sync"),
    ("rg", "Fast code search (fallback available)"),
    ("ruff", "Python lint & security SAST"),
    ("mypy", "Python static type checking"),
    ("eslint", "JS/TS lint checks"),
    ("gh", "GitHub PR creation (/sync)"),
    ("mvn", "Java Maven build & test"),
    ("go", "Go build, vet & test"),
    ("pip-audit", "Python CVE dependency audit"),
    ("gitleaks", "Secret scanning"),
)


def run_doctor(h, *, offline: bool = False) -> str:
    rows: list[tuple[str, str, str]] = []

    # 1. Python & workspace state
    rows.append(
        (
            "Python runtime",
            "OK",
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} ({sys.executable})",
        )
    )
    rows.append(("Workspace", "OK", str(h.root)))

    # 2. Git repository
    branch, dirty = h.git_info()
    if branch:
        rows.append(("Git repository", "OK", f"branch `{branch}`{' (dirty)' if dirty else ' (clean)'}"))
    else:
        rows.append(("Git repository", "WARN", "Not a Git repo (Git commands & checkpoints disabled)"))

    # 3. Model & .env drift check
    env_model = os.environ.get("ADK_CODER_MODEL")
    if env_model and env_model != h.model:
        rows.append(
            (
                "Model config",
                "WARN",
                f"Workspace uses `{h.model}` (overrides env `{env_model}`)",
            )
        )
    else:
        fast = h.config.get("fast_model")
        detail = f"{h.model}" + (f" (fast_model: {fast})" if fast else "")
        rows.append(("Model config", "OK", detail))

    # 4. Rules & test command
    rules_names, _ = h.load_project_rules()
    rows.append(
        (
            "Project rules",
            "OK" if rules_names else "INFO",
            ", ".join(rules_names) if rules_names else "None found (create AGENTS.md)",
        )
    )
    test_cmd = h.test_command()
    rows.append(
        (
            "Test command",
            "OK" if test_cmd else "WARN",
            test_cmd or "Not detected (set with /config test_command <cmd>)",
        )
    )

    # 5. Live LLM connectivity check
    if not offline:
        provider = detect_provider(h.model)
        from .config_ui import default_env_path

        env_vals = parse_env_file(default_env_path())
        if provider == "ollama":
            base_url = (
                h.config.get("ollama_api_base")
                or os.environ.get("OLLAMA_API_BASE")
                or os.environ.get("OLLAMA_HOST")
                or env_vals.get("OLLAMA_HOST")
                or "http://127.0.0.1:11434"
            )
            api_key = ""
            api_token = os.environ.get("ADK_CODER_ENDPOINT_API_KEY") or ""
        elif provider == "gemini":
            base_url = ""
            api_key = os.environ.get("GOOGLE_API_KEY") or env_vals.get("GOOGLE_API_KEY") or ""
            api_token = ""
        elif provider == "anthropic":
            base_url = ""
            api_key = os.environ.get("ANTHROPIC_API_KEY") or env_vals.get("ANTHROPIC_API_KEY") or ""
            api_token = ""
        else:
            base_url = (
                h.config.get("openai_api_base")
                or os.environ.get("OPENAI_API_BASE")
                or os.environ.get("OPENAI_BASE_URL")
                or env_vals.get("OPENAI_API_BASE")
                or ""
            )
            api_key = os.environ.get("OPENAI_API_KEY") or env_vals.get("OPENAI_API_KEY") or ""
            api_token = (
                os.environ.get("ADK_CODER_ENDPOINT_API_KEY")
                or env_vals.get("ADK_CODER_ENDPOINT_API_KEY")
                or ""
            )

        with spinning(f"Testing {provider} connectivity"):
            res = check_connectivity(
                provider=provider,
                model=h.model,
                base_url=base_url,
                api_key=api_key,
                api_token=api_token,
            )
        if res.get("ok"):
            rows.append(
                (
                    "LLM connectivity",
                    "OK",
                    f"{res.get('message')} (reply: {res.get('reply', 'OK')})",
                )
            )
        else:
            rows.append(
                (
                    "LLM connectivity",
                    "FAIL",
                    f"{res.get('message')} — {res.get('hint', '')}".rstrip(" —"),
                )
            )
    else:
        rows.append(("LLM connectivity", "INFO", "Skipped (--offline)"))

    # 6. Developer CLI toolchains
    installed = [name for name, _ in TOOL_CHECKS if shutil.which(name)]
    missing = [name for name, _ in TOOL_CHECKS if not shutil.which(name)]
    rows.append(("Installed tools", "OK", ", ".join(installed) or "(none)"))
    if missing:
        rows.append(("Optional missing", "INFO", ", ".join(missing)))

    if console.is_terminal:
        table = Table(title="adk-coder Doctor Diagnostics", border_style="cyan")
        table.add_column("Check", style="bold")
        table.add_column("Status", no_wrap=True)
        table.add_column("Details")
        styles = {"OK": "bold green", "WARN": "bold yellow", "FAIL": "bold red", "INFO": "dim"}
        for name, state, detail in rows:
            table.add_row(name, Text(state, style=styles.get(state, "white")), detail)
        console.print(table)
        return ""
    return "\n".join(f"[{state}] {name}: {detail}" for name, state, detail in rows)
