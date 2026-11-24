"""Antigravity MCP App plugin generator & installer (`adk-coder --install-mcp-plugin`)."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from . import __version__
from .tools import atomic_write

PLUGIN_NAME = "adk-coder-workbench"

LOGO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" fill="none">
  <rect width="64" height="64" rx="16" fill="#0B57D0"/>
  <path d="M18 42L27 22H31L40 42H35.5L33.4 37H24.6L22.5 42H18ZM26.1 33.4H31.9L29 26.5L26.1 33.4Z" fill="#FFFFFF"/>
  <circle cx="46" cy="24" r="5" fill="#A8C7FA"/>
  <path d="M41 41C41 37.7 43.2 35.5 46 35.5C48.8 35.5 51 37.7 51 41" stroke="#A8C7FA" stroke-width="3" stroke-linecap="round"/>
</svg>
"""


def default_plugin_dir() -> Path:
    return Path.home() / ".gemini" / "config" / "plugins" / PLUGIN_NAME


def install_mcp_plugin(
    dest: Path | None = None,
    *,
    python_executable: str | None = None,
) -> Path:
    """Install the Antigravity MCP App plugin bundle into ~/.gemini/config/plugins/adk-coder-workbench/."""
    plugin_dir = (dest or default_plugin_dir()).expanduser().resolve()
    bin_dir = plugin_dir / "bin"
    assets_dir = plugin_dir / "assets"
    bin_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)

    py_bin = python_executable or sys.executable

    plugin_json = {
        "name": PLUGIN_NAME,
        "version": __version__,
        "description": (
            "Interactive ADK Coder Workbench MCP App: Security & SOLID audits, "
            "CycloneDX/SPDX SBOM, deterministic architecture & API diagrams, "
            "SDLC docs, provider profiles, and enterprise scaffolding."
        ),
        "logo": "assets/logo.svg",
    }
    atomic_write(plugin_dir / "plugin.json", json.dumps(plugin_json, indent=2) + "\n")

    mcp_config = {
        "mcpServers": {
            "workbench": {
                "command": "bash",
                "args": [
                    "${PLUGIN_ROOT}/bin/adk-coder-mcp-server",
                    "--data_dir=${PLUGIN_DATA}",
                ],
            }
        }
    }
    atomic_write(plugin_dir / "mcp_config.json", json.dumps(mcp_config, indent=2) + "\n")

    launcher_script = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'PY_BIN="${{ADK_CODER_PYTHON:-{py_bin}}}"\n'
        'exec "$PY_BIN" -m adk_coder.mcp_server "$@"\n'
    )
    launcher_path = bin_dir / "adk-coder-mcp-server"
    atomic_write(launcher_path, launcher_script)
    try:
        current_mode = launcher_path.stat().st_mode
        os.chmod(launcher_path, current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass

    atomic_write(assets_dir / "logo.svg", LOGO_SVG)
    return plugin_dir
