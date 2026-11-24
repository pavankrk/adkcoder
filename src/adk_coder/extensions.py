"""Custom slash commands, skills, lifecycle hooks, and MCP server configuration."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .scrubber import scrub
from .tools import atomic_write

HOOK_EVENTS = ("pre_edit", "post_edit", "pre_shell", "post_shell")


# ---------------------------------------------------------------------------
# Custom Slash Commands (.agent/commands/<name>.md)
# ---------------------------------------------------------------------------
def list_custom_commands(state_dir: Path) -> dict[str, Path]:
    cmd_dir = state_dir / "commands"
    if not cmd_dir.is_dir():
        return {}
    found: dict[str, Path] = {}
    for path in sorted(cmd_dir.glob("*.md")):
        if path.is_file() and not path.is_symlink():
            name = path.stem.lower()
            if re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", name):
                found[name] = path
    return found


def render_custom_command(path: Path, arguments: str) -> str:
    template = path.read_text(encoding="utf8").strip()
    if "$ARGUMENTS" in template:
        return template.replace("$ARGUMENTS", arguments)
    return f"{template}\n\n{arguments}".strip() if arguments else template


def create_custom_command(state_dir: Path, name: str, body: str = "") -> Path:
    clean = name.strip().lower().lstrip("/")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", clean):
        raise ValueError("Use a lowercase command name (a-z, 0-9, -, _).")
    cmd_dir = state_dir / "commands"
    cmd_dir.mkdir(mode=0o700, exist_ok=True)
    dest = cmd_dir / f"{clean}.md"
    content = body.strip() or (
        f"# Custom command: /{clean}\n\n"
        "Inspect the workspace and fulfill the following request:\n\n"
        "$ARGUMENTS\n"
    )
    atomic_write(dest, content + "\n", mode=0o600)
    return dest


# ---------------------------------------------------------------------------
# Workspace Skills (.agent/skills/<name>/SKILL.md)
# ---------------------------------------------------------------------------
def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip().strip('"').strip("'")
    return meta, parts[2].strip()


def list_skills(state_dir: Path) -> dict[str, dict[str, str]]:
    skills_dir = state_dir / "skills"
    if not skills_dir.is_dir():
        return {}
    found: dict[str, dict[str, str]] = {}
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        if skill_md.is_symlink() or not skill_md.is_file():
            continue
        try:
            raw = skill_md.read_text(encoding="utf8")
        except (OSError, UnicodeError):
            continue
        meta, body = _parse_frontmatter(raw)
        key = meta.get("name") or skill_md.parent.name
        found[key] = {
            "name": key,
            "description": meta.get("description", "Workspace skill"),
            "path": str(skill_md),
            "body": body,
        }
    return found


# ---------------------------------------------------------------------------
# Lifecycle Hooks (.agent/hooks.json)
# ---------------------------------------------------------------------------
class HookRunner:
    def __init__(self, root: Path, state_dir: Path):
        self.root = root
        self.config_path = state_dir / "hooks.json"

    def load(self) -> dict[str, list[str]]:
        if not self.config_path.is_file():
            return {}
        try:
            data = json.loads(self.config_path.read_text(encoding="utf8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        result: dict[str, list[str]] = {}
        for event in HOOK_EVENTS:
            cmds = data.get(event)
            if isinstance(cmds, list):
                result[event] = [str(c) for c in cmds if isinstance(c, str) and c.strip()]
        return result

    def init_template(self) -> Path:
        sample = {
            "pre_edit": [],
            "post_edit": [],
            "pre_shell": [],
            "post_shell": [],
        }
        atomic_write(self.config_path, json.dumps(sample, indent=2) + "\n", mode=0o600)
        return self.config_path

    def run(
        self,
        event: str,
        *,
        file_path: str = "",
        command: str = "",
    ) -> tuple[bool, str]:
        """Run hooks for `event`. Returns (allowed, combined_output)."""
        hooks = self.load().get(event, [])
        if not hooks:
            return True, ""
        env = dict(
            os.environ,
            ADK_CODER_EVENT=event,
            ADK_CODER_WORKSPACE=str(self.root),
            ADK_CODER_FILE=file_path,
            ADK_CODER_COMMAND=command,
        )
        outputs: list[str] = []
        for raw_cmd in hooks:
            expanded = raw_cmd.replace("$FILE", file_path).replace("$COMMAND", command)
            exec_args = (
                ["cmd", "/c", expanded]
                if sys.platform == "win32"
                else ["/bin/sh", "-c", expanded]
            )
            try:
                proc = subprocess.run(
                    exec_args,
                    cwd=self.root,
                    env=env,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=60,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                msg = f"Hook `{event}` failed: {exc}"
                if event.startswith("pre_"):
                    return False, msg
                outputs.append(msg)
                continue
            text = scrub((proc.stdout + proc.stderr).strip())
            if proc.returncode != 0:
                msg = text or f"Hook `{event}` exited with code {proc.returncode}"
                if event.startswith("pre_"):
                    return False, msg
                outputs.append(msg)
            elif text:
                outputs.append(text)
        return True, "\n".join(outputs)


# ---------------------------------------------------------------------------
# MCP Server Config (.agent/mcp.json) + ADK McpToolset Loader
# ---------------------------------------------------------------------------
class McpRegistry:
    def __init__(self, state_dir: Path):
        self.config_path = state_dir / "mcp.json"

    def load(self) -> dict[str, dict[str, Any]]:
        if not self.config_path.is_file():
            return {}
        try:
            data = json.loads(self.config_path.read_text(encoding="utf8"))
        except (OSError, json.JSONDecodeError):
            return {}
        servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
        return servers if isinstance(servers, dict) else {}

    def save(self, servers: dict[str, dict[str, Any]]) -> None:
        atomic_write(
            self.config_path,
            json.dumps({"mcpServers": servers}, indent=2) + "\n",
            mode=0o600,
        )

    def add(self, name: str, command: str, args: list[str] | None = None) -> dict[str, Any]:
        clean = name.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", clean):
            raise ValueError("Invalid MCP server name.")
        servers = self.load()
        entry: dict[str, Any] = {"command": command, "args": list(args or [])}
        servers[clean] = entry
        self.save(servers)
        return entry

    def remove(self, name: str) -> bool:
        servers = self.load()
        if name not in servers:
            return False
        del servers[name]
        self.save(servers)
        return True

    def build_adk_toolsets(self) -> tuple[list[Any], list[str]]:
        """Instantiate ADK McpToolset objects for configured servers when `mcp` is installed."""
        servers = self.load()
        if not servers:
            return [], []
        try:
            from google.adk.tools import McpToolset
            from mcp import StdioServerParameters
        except ImportError:
            return [], [
                "MCP servers configured in .agent/mcp.json, but the `mcp` package is not installed (`pip install mcp`)."
            ]

        toolsets: list[Any] = []
        notes: list[str] = []
        for name, cfg in servers.items():
            cmd = cfg.get("command")
            if not cmd:
                continue
            try:
                params = StdioServerParameters(
                    command=str(cmd),
                    args=[str(a) for a in cfg.get("args", [])],
                    env=cfg.get("env") or None,
                )
                toolsets.append(McpToolset(connection_params=params))
            except Exception as exc:  # noqa: BLE001
                notes.append(f"Skipped MCP server `{name}`: {exc}")
        return toolsets, notes
