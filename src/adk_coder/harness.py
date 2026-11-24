"""Core workspace orchestrator for adk-coder commands, workflows, and chat."""

from __future__ import annotations

import asyncio
import difflib
import json
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from .changelog import record
from .checkpoint import Checkpoints, Git
from .diff_viewer import show_patch
from .extensions import (
    HookRunner,
    McpRegistry,
    create_custom_command,
    list_custom_commands,
    list_skills,
    render_custom_command,
)
from .model_discovery import discover_models
from .model_factory import LITELLM_PREFIXES, generate
from .repo_map import build_repo_map
from .scrubber import scrub
from .semver import next_version
from .sessions import SessionStore
from .spinner import spinning
from .token_manager import TokenTracker, capacity, estimate, needs_compaction
from .tools import MODES, WorkspaceTools, atomic_write
from .ui import (
    StreamDisplay,
    console,
    interactive_confirm,
    render_help,
    render_status,
    render_tool_call,
    render_tool_result,
)

_DEFAULT_GENERATE = generate

COMMANDS = {
    "init": "",
    "help": "",
    "status": "",
    "doctor": "[--offline]",
    "map": "[path]",
    "paste": "[prompt]",
    "settings": "[port]",
    "export": "[output.md]",
    "mode": "[ask|auto-edit|plan|full-auto]",
    "plan": "[goal|@file]",
    "session": "[list|new [name]|resume <id|name>|fork [name]|delete <id|name>]",
    "context": "",
    "pin": "<file>",
    "unpin": "<file|all>",
    "tdd": "[max-attempts]",
    "test-fix": "[max-attempts]",
    "explore": "<question>",
    "commands": "[list|new <name>]",
    "skills": "",
    "skill": "<name> [request]",
    "hooks": "[list|init]",
    "mcp": "[list|add <name> <cmd> [args...]|remove <name>]",
    "model": "[list|index|model]",
    "profile": "[list|save <name>|use <name>|fallback <name|none>|delete <name>]",
    "config": "[key [value]]",
    "task": "[add <text>|done <n>|rm <n>|list]",
    "compact": "",
    "reset": "",
    "clear": "",
    "review": "",
    "pr-review": "[base-branch] [--out docs/PR_REVIEW.md] [--post]",
    "analyze": "[path] [--out docs/ANALYSIS.md]",
    "analyse": "[path] [--out docs/ANALYSIS.md]",
    "diagram": "[path] [--out docs/DIAGRAM.md]",
    "debug": "[logfile|@file|error] [--cmd <cmd>] [--tail N] [--analyze-only] [--out docs/DEBUG_REPORT.md]",
    "testgen": "<file> [--out <test-file>] [--cmd <cmd>] [--max-retries N]",
    "resolve-conflicts": "[file] [--dry-run]",
    "watch": "[test-cmd] [--auto-fix] [--interval 1.5] [--once]",
    "feature": "<description|@file> [--quick]",
    "audit": "[path]",
    "scan": "[path]",
    "security": "[path]",
    "sbom": "[cyclonedx|spdx] [output-path]",
    "fix": "[all|debug|index|path]",
    "scaffold": "[template project-name [parent-directory] [--from <file>] [--minimal]]",
    "new": "[template project-name [parent-directory] [--from <file>] [--minimal]]",
    "create": "[--from <requirements.md>]",
    "blueprint": "<template> <project-name> [requirement|@file]",
    "workflow": "[requirement|@file]",
    "diff": "[--staged] [path]",
    "commit": "[message]",
    "undo-edit": "[count]",
    "branch": "[name|-d <name>]",
    "merge": "[target] [--squash|--rebase]",
    "stash": "[list|push|show|pop|apply|drop] [argument]",
    "revert": "<file>",
    "undo": "<file>",
    "checkpoint": "[list|create <label>|restore <id>|delete <id>]",
    "changelog": "",
    "sync": "[--title <text>|--release [auto|vX.Y.Z]]",
    "exit": "",
}

CONFIG_KEYS = {
    "model",
    "fast_model",
    "mode",
    "notifications",
    "test_command",
    "openai_api_base",
    "ollama_api_base",
    "context_window",
    "max_tokens",
    "request_timeout",
    "shell_mode",
    "sandbox_shell",
    "repo_map",
}

_PROVIDER_GROUP = "|".join(re.escape(p.rstrip("/")) for p in LITELLM_PREFIXES)
MODEL_PATTERN = re.compile(rf"(?:gemini-[\w.-]+|(?:{_PROVIDER_GROUP})/[\w./:-]+)")


def confirm(message: str):
    return interactive_confirm(message)


class AgentHarness:
    def __init__(self, root, approve=confirm):
        self.root = Path(root).resolve(strict=True)
        self.state = self.root / ".agent"
        if self.state.is_symlink():
            raise ValueError("Workspace state directory must be local.")
        self.state.mkdir(mode=0o700, exist_ok=True)
        if any(p.is_symlink() for p in self.state.rglob("*")):
            raise ValueError("Workspace state contains symlinks.")
        self._approve = approve
        self.git = Git(self.root)
        self.checkpoints = Checkpoints(self.root, self.git)
        self.checkpoints.directory.mkdir(mode=0o700, exist_ok=True)
        env_mode = (os.environ.get("ADK_CODER_MODE") or "").strip().lower()
        default_mode = env_mode if env_mode in MODES else "ask"
        defaults = {
            "model": os.environ.get("ADK_CODER_MODEL") or "gemini-2.5-flash",
            "workspace_root": str(self.root),
            "notifications": False,
            "shell_mode": True,
        }
        files = {
            "config.json": json.dumps(defaults, indent=2),
            "TASKS.md": "# Tasks\n",
            "context_summary.md": "",
            "session_history.json": "[]",
            "pinned.json": "[]",
            "CHANGELOG.md": "# Workspace changelog\n",
            ".history": "",
        }
        for name, content in files.items():
            target = self.state / name
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf8") as stream:
                    stream.write(content)
            except FileExistsError:
                pass
        self.config = json.loads(self.read("config.json"))
        self.tracker = TokenTracker()
        self.abort_signal: asyncio.Event | None = None
        self.last_streamed: bool = False
        self._last_discovered_models: list[dict[str, str]] = []
        self._settings_server = None
        self._settings_url = ""
        self.hooks = HookRunner(self.root, self.state)
        self.mcp = McpRegistry(self.state)
        effective_mode = self.config.get("mode") or default_mode
        if effective_mode not in MODES:
            effective_mode = default_mode
        self.tools = WorkspaceTools(
            self.root,
            approve,
            self.config.get("notifications", False),
            shell_mode=self.config.get("shell_mode", True),
            mode=effective_mode,
            sandbox_shell=self.config.get("sandbox_shell", False),
            hooks=self.hooks,
        )
        self.sessions = SessionStore(self.state)
        latest = self.sessions.latest()
        if latest is None:
            branch, _ = self.git_info()
            created = self.sessions.create(
                "default",
                model=self.model,
                branch=branch,
                summary=self.read("context_summary.md"),
                history=self._load_history(),
                pinned=self._load_pinned(),
            )
            self.active_session_id = created["id"]
        else:
            self.active_session_id = latest["id"]

    @property
    def approve(self):
        if hasattr(self, "tools") and self.tools.mode == "full-auto":
            return lambda _msg: True
        return self._approve

    @approve.setter
    def approve(self, fn):
        self._approve = fn
        if hasattr(self, "tools"):
            self.tools.approve = fn

    def read(self, name: str) -> str:
        return (self.state / name).read_text(encoding="utf8")

    def write(self, name: str, value: str) -> None:
        atomic_write(self.state / name, scrub(value), mode=0o600)

    def _load_history(self) -> list[dict[str, str]]:
        try:
            data = json.loads(self.read("session_history.json"))
            if isinstance(data, list):
                return [
                    t
                    for t in data
                    if isinstance(t, dict)
                    and t.get("role") in {"user", "model"}
                    and isinstance(t.get("text"), str)
                ]
        except (OSError, json.JSONDecodeError):
            pass
        return []

    def _save_history(self, turns: list[dict[str, str]]) -> None:
        # Keep the last 40 structured turns bounded on disk.
        self.write("session_history.json", json.dumps(turns[-40:], indent=2))
        self._sync_session()

    def _load_pinned(self) -> list[str]:
        try:
            data = json.loads(self.read("pinned.json"))
            if isinstance(data, list):
                return [str(p) for p in data if isinstance(p, str)]
        except (OSError, json.JSONDecodeError):
            pass
        return []

    def _save_pinned(self, pinned: list[str]) -> None:
        self.write("pinned.json", json.dumps(pinned, indent=2))
        self._sync_session()

    def _sync_session(self) -> None:
        if not hasattr(self, "sessions") or not getattr(self, "active_session_id", ""):
            return
        branch, _ = self.git_info()
        self.sessions.save(
            self.active_session_id,
            model=self.model,
            branch=branch,
            summary=self.read("context_summary.md"),
            history=self._load_history(),
            pinned=self._load_pinned(),
        )

    def resume_session(self, key: str | None = None) -> str:
        target = self.sessions.get(key) if key else self.sessions.latest()
        if not target:
            raise ValueError(f"Session not found: {key}" if key else "No saved sessions.")
        self.active_session_id = target["id"]
        self.write("context_summary.md", target["summary"])
        self.write("session_history.json", json.dumps(target["history"], indent=2))
        self.write("pinned.json", json.dumps(target["pinned"], indent=2))
        return (
            f"Resumed session `{target['name']}` ({target['id']}, "
            f"{len(target['history']) // 2} turns, {len(target['pinned'])} pinned files)."
        )

    def _pinned_context(self) -> str:
        blocks: list[str] = []
        budget = 24_000
        for rel in self._load_pinned():
            try:
                path = self.tools.path(rel)
                if not path.is_file():
                    continue
                content = scrub(path.read_text(encoding="utf8"))[: min(8000, budget)]
                blocks.append(f"### Pinned File: {rel}\n```\n{content}\n```")
                budget -= len(content)
                if budget <= 500:
                    break
            except (ValueError, OSError, UnicodeError):
                continue
        return "\n\n".join(blocks)

    @property
    def model(self) -> str:
        return self.config["model"]

    @property
    def mode(self) -> str:
        return self.tools.mode

    def set_mode(self, new_mode: str) -> str:
        cleaned = new_mode.strip().lower()
        if cleaned not in MODES:
            raise ValueError(f"Unknown mode {new_mode!r}. Use: {', '.join(MODES)}.")
        self.tools.mode = cleaned
        self.config["mode"] = cleaned
        self.write("config.json", json.dumps(self.config, indent=2))
        return cleaned

    def cycle_mode(self) -> str:
        idx = MODES.index(self.mode) if self.mode in MODES else 0
        return self.set_mode(MODES[(idx + 1) % len(MODES)])

    def load_project_rules(self) -> tuple[list[str], str]:
        """Auto-load AGENTS.md / CLAUDE.md / .agent/RULES.md instructions from the workspace."""
        loaded_names: list[str] = []
        sections: list[str] = []
        budget = 6000
        for rel in ("AGENTS.md", "CLAUDE.md", ".agent/RULES.md"):
            path = self.root / rel
            if not path.is_file():
                continue
            try:
                content = scrub(path.read_text(encoding="utf8").strip())
            except (OSError, UnicodeError):
                continue
            if not content:
                continue
            loaded_names.append(rel)
            snippet = content[:budget]
            sections.append(f"## Project Rules ({rel})\n{snippet}")
            budget -= len(snippet)
            if budget <= 200:
                break
        return loaded_names, "\n\n".join(sections)

    @property
    def context_window(self) -> int | None:
        val = self.config.get("context_window")
        return val if isinstance(val, int) and val > 0 else None

    def git_info(self) -> tuple[str, bool]:
        try:
            branch = self.git.run("branch", "--show-current")
            dirty = bool(
                branch
                and self.git.run("status", "--porcelain", "--", ".", ":(exclude).agent")
            )
            return branch, dirty
        except ValueError:
            return "", False

    def status(self) -> str:
        branch, dirty = self.git_info()
        rules_sources, _ = self.load_project_rules()
        return render_status(
            self.root,
            self.model,
            self.read("context_summary.md"),
            self.read("TASKS.md"),
            mode=self.mode,
            rules_sources=rules_sources,
            context_window=self.context_window,
            test_command=self.test_command(),
            tracker=self.tracker,
            branch=branch,
            dirty=dirty,
        )

    def reload_config(self) -> None:
        """Reload .agent/config.json and .env after in-REPL Config Studio edits."""
        from .config_ui import default_env_path, parse_env_file

        for key, val in parse_env_file(default_env_path()).items():
            os.environ[key] = val
        try:
            self.config = json.loads(self.read("config.json"))
        except (OSError, json.JSONDecodeError):
            return
        env_mode = (os.environ.get("ADK_CODER_MODE") or "").strip().lower()
        next_mode = self.config.get("mode") or (env_mode if env_mode in MODES else self.tools.mode)
        if next_mode in MODES:
            self.tools.mode = next_mode
        self.tools.notifications = bool(self.config.get("notifications", False))
        self.tools.shell_mode = bool(self.config.get("shell_mode", True))
        self.tools.sandbox_shell = bool(self.config.get("sandbox_shell", False))

    async def ask(
        self,
        role: str,
        prompt: str,
        tools=(),
        *,
        history: list[dict[str, str]] | None = None,
        attachments: list[tuple[bytes, str]] | None = None,
        show_tools: bool = False,
        stream_display: StreamDisplay | None = None,
        fast: bool = False,
    ) -> str:
        active_model = (
            self.config.get("fast_model") if fast and self.config.get("fast_model") else self.model
        )
        options: dict = {}
        if active_model.startswith("openai/"):
            api_base = (
                self.config.get("openai_api_base")
                or os.environ.get("OPENAI_API_BASE")
                or os.environ.get("OPENAI_BASE_URL")
            )
            if api_base:
                options["api_base"] = api_base
        elif active_model.startswith(("ollama/", "ollama_chat/")):
            ollama_base = (
                self.config.get("ollama_api_base")
                or os.environ.get("OLLAMA_API_BASE")
                or os.environ.get("OLLAMA_HOST")
            )
            if ollama_base:
                options["api_base"] = (
                    ollama_base
                    if ollama_base.startswith(("http://", "https://"))
                    else f"http://{ollama_base}"
                )
        if generate is _DEFAULT_GENERATE:
            if isinstance(self.config.get("max_tokens"), int):
                options["max_tokens"] = self.config["max_tokens"]
            if isinstance(self.config.get("request_timeout"), int):
                options["timeout"] = self.config["request_timeout"]
            if history:
                options["history"] = history
            if attachments:
                options["attachments"] = attachments

            def _on_usage(p_tok: int, o_tok: int) -> None:
                self.tracker.record(p_tok, o_tok)
                if stream_display is not None:
                    stream_display.record_usage(p_tok, o_tok)

            options["on_usage"] = _on_usage
            if self.abort_signal is not None:
                options["abort_signal"] = self.abort_signal
            if show_tools:

                def _on_tool(name: str, args: dict) -> None:
                    if stream_display is not None:
                        stream_display.before_tool_call()
                        stream_display.record_tool_call()
                    render_tool_call(name, args)

                def _on_tool_result(name: str, preview: str) -> None:
                    render_tool_result(name, preview)
                    if stream_display is not None:
                        stream_display.after_tool_result()

                options["on_tool_call"] = _on_tool
                options["on_tool_result"] = _on_tool_result
            if stream_display is not None:
                options["on_text"] = stream_display.on_text
                options["on_thought"] = stream_display.on_thought
        try:
            if options:
                return await generate(active_model, role, prompt, tools, **options)
            return await generate(active_model, role, prompt, tools)
        except Exception as exc:
            from .profiles import (
                apply_profile_to_env,
                get_fallback_profile,
                is_transient_provider_error,
            )

            if not is_transient_provider_error(exc):
                raise
            fb = get_fallback_profile(active_model)
            if fb is None:
                raise
            fb_name, fb_prof = fb
            fb_model = str(fb_prof.get("model") or "")
            if not fb_model:
                raise
            apply_profile_to_env(fb_prof)
            fb_options = dict(options)
            if fb_model.startswith("openai/"):
                if fb_prof.get("base_url"):
                    fb_options["api_base"] = fb_prof["base_url"]
                else:
                    fb_options.pop("api_base", None)
            elif fb_model.startswith(("ollama/", "ollama_chat/")):
                fb_options["api_base"] = fb_prof.get("base_url") or "http://127.0.0.1:11434"
            else:
                fb_options.pop("api_base", None)
            console.print(
                f"[yellow]Primary model `{active_model}` unreachable ({type(exc).__name__}); "
                f"failing over to profile `{fb_name}` (`{fb_model}`)...[/]"
            )
            if fb_options:
                return await generate(fb_model, role, prompt, tools, **fb_options)
            return await generate(fb_model, role, prompt, tools)

    async def compact(self) -> str:
        memory = self.read("context_summary.md")
        if memory.strip():
            with spinning("Compacting context"):
                if self.config.get("fast_model"):
                    summary = await self.ask(
                        "Summarize decisions, constraints and pending work.",
                        memory,
                        fast=True,
                    )
                else:
                    summary = await self.ask(
                        "Summarize decisions, constraints and pending work.",
                        memory,
                    )
            self.write("context_summary.md", summary + "\n")
            self._save_history(
                [
                    {"role": "user", "text": "Previous conversation summary:"},
                    {"role": "model", "text": summary},
                ]
            )
        return "Context compacted."

    def _extract_image_attachments(self, request: str) -> list[tuple[bytes, str]]:
        image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        mentions = re.findall(r"(?<!\S)@([\w./-]+)", request)
        attachments: list[tuple[bytes, str]] = []
        for token in mentions[:3]:
            try:
                path = self.tools.path(token)
                if path.is_file() and path.suffix.lower() in image_exts and path.stat().st_size <= 5_000_000:
                    mime, _ = mimetypes.guess_type(path.name)
                    attachments.append((path.read_bytes(), mime or "image/png"))
            except (ValueError, OSError):
                continue
        return attachments

    def _expand_file_mentions(self, request: str) -> str:
        mentions = re.findall(r"(?<!\S)@([\w./-]+)", request)
        resolved = []
        for token in mentions[:5]:
            try:
                path = self.tools.path(token)
                if path.is_file():
                    rel = path.relative_to(self.root).as_posix()
                    resolved.append(rel)
            except ValueError:
                continue
        if not resolved:
            return request
        return (
            request
            + "\n\nReferenced workspace files (inspect with read_file): "
            + ", ".join(resolved)
        )

    def _read_only_tools(self):
        return [
            self.tools.list_dir,
            self.tools.find_files,
            self.tools.search_files,
            self.tools.read_file,
        ]

    async def explore_codebase(self, question: str) -> str:
        """Delegate a focused read-only codebase question to an isolated sub-agent."""
        with spinning("Sub-agent exploring codebase"):
            return await self.ask(
                "You are an isolated read-only codebase explorer sub-agent. "
                "Use list_dir, find_files, search_files, and read_file to gather facts and return "
                "a concise, file-referenced summary answering the developer's question.",
                question,
                self._read_only_tools(),
                show_tools=True,
                fast=True,
            )

    def _agent_tools(self):
        base: list = [*self._read_only_tools(), self.tools.fetch_url, self.explore_codebase]
        mcp_tools, _ = self.mcp.build_adk_toolsets()
        if self.mode == "plan":
            return base
        return [
            *base,
            self.tools.write_file,
            self.tools.apply_diff,
            self.tools.apply_multi_diff,
            self.tools.delete_file,
            self.tools.run_shell_command,
            *mcp_tools,
        ]

    async def chat(self, request: str, *, record_memory: bool = True) -> str:
        self.last_streamed = False
        memory = self.read("context_summary.md")
        if needs_compaction(memory + request, self.model, self.context_window) and memory:
            await self.compact()
            memory = self.read("context_summary.md")

        history = self._load_history()
        enriched = self._expand_file_mentions(request)
        tasks = self.read("TASKS.md").strip()
        pinned_block = self._pinned_context()
        if history:
            prompt = "\n\n".join(part for part in (pinned_block, tasks, enriched) if part)
        else:
            prompt = "\n\n".join(part for part in (memory, pinned_block, tasks, enriched) if part)

        _, project_rules = self.load_project_rules()
        if self.mode == "plan":
            instruction = (
                "You are adk-coder in READ-ONLY PLAN MODE. "
                "Inspect the codebase with list_dir, find_files, search_files, and read_file, "
                "then present a clear, step-by-step implementation plan with exact file paths. "
                "Do not claim to have modified files."
            )
        else:
            instruction = (
                "You are adk-coder, an expert software engineering assistant operating inside a local workspace. "
                "Use list_dir, find_files, search_files, and read_file to inspect files. "
                "Use write_file to create new files and apply_diff to modify existing files. "
                "Execute tool calls directly rather than only describing code in text."
            )
        if project_rules:
            instruction = f"{instruction}\n\n{project_rules}"
        if self.config.get("repo_map", True):
            repo_symbols = build_repo_map(self.root)
            if repo_symbols:
                instruction = f"{instruction}\n\n## Repository Symbol Map\n{repo_symbols}"

        attachments = self._extract_image_attachments(request)
        streamer = StreamDisplay() if console.is_terminal else None
        answer = ""
        with spinning("Planning" if self.mode == "plan" else "Thinking"):
            try:
                answer = await self.ask(
                    instruction,
                    prompt,
                    self._agent_tools(),
                    history=history or None,
                    attachments=attachments or None,
                    show_tools=True,
                    stream_display=streamer,
                )
            finally:
                if streamer is not None:
                    streamer.finish(final_text=answer if streamer.streamed_text else "")
                    self.last_streamed = streamer.streamed_text
        if streamer is not None and answer:
            streamer.render_footer(fallback_output_text=answer)

        if record_memory:
            self.write(
                "context_summary.md",
                self.read("context_summary.md")
                + f"\nDeveloper: {request}\nAssistant: {answer}\n",
            )
            history.append({"role": "user", "text": scrub(request)})
            history.append({"role": "model", "text": scrub(answer)})
            self._save_history(history)
        return answer

    async def plan_mode(self, raw_goal: str) -> str:
        from .scaffolder import resolve_requirements

        goal = resolve_requirements(self, raw_goal)
        if not goal:
            raise ValueError("Usage: /plan <goal|@requirements.md>")
        prev_mode = self.mode
        self.tools.mode = "plan"
        try:
            plan_text = await self.chat(
                f"Produce a detailed, actionable implementation plan (files to create/edit, "
                f"SOLID boundaries, risks, and verification commands) for:\n\n{goal}",
                record_memory=True,
            )
        finally:
            self.tools.mode = prev_mode
        self.write("PLAN.md", f"# Implementation Plan\n\n{plan_text}\n")
        return plan_text

    def shell_escape(self, command: str) -> str:
        """Execute a direct `!cmd` shell command immediately and record output in context."""
        cleaned = command.strip()
        if not cleaned:
            raise ValueError("Usage: !<shell command>")
        exec_args = (
            ["cmd", "/c", cleaned]
            if sys.platform == "win32"
            else ["/bin/sh", "-c", cleaned]
        )
        record(self.state, "SHELL_ESCAPE", cleaned)
        with spinning(f"Running !{cleaned.split()[0]}"):
            proc = subprocess.run(
                exec_args,
                cwd=self.root,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=300,
                check=False,
            )
        combined = scrub((proc.stdout + proc.stderr)[-16000:]).rstrip()
        header = f"Exit {proc.returncode}"
        output = f"{header}\n{combined}".rstrip()
        # Inject concise command result into context so user can ask about it immediately.
        snippet = combined[-2500:]
        self.write(
            "context_summary.md",
            self.read("context_summary.md")
            + f"\nShell `!{cleaned}` ({header}):\n{snippet}\n",
        )
        history = self._load_history()
        history.append(
            {"role": "user", "text": f"I ran `{cleaned}` ({header}):\n{snippet}"}
        )
        self._save_history(history)
        return output

    async def commit_changes(self, message_hint: str = "") -> str:
        """Stage if needed, draft a Conventional Commit message, confirm, and commit."""
        staged = self.git.run("diff", "--cached", "--", ".", ":(exclude).agent")
        if not staged:
            status = self.git.run("status", "--porcelain", "--", ".", ":(exclude).agent")
            if not status:
                return "Working tree clean — nothing to commit."
            if not self.approve("No staged changes. Stage all workspace changes for commit?"):
                return "Cancelled."
            self.git.run("add", "-A", "--", ".", ":(exclude).agent")
            staged = self.git.run("diff", "--cached", "--", ".", ":(exclude).agent")
            if not staged:
                return "Nothing staged to commit."

        commit_msg = message_hint.strip()
        if not commit_msg:
            stat = self.git.run("diff", "--cached", "--stat", "--", ".", ":(exclude).agent")
            with spinning("Drafting commit message"):
                msg_role = (
                    "Write a single concise Conventional Commit subject line (e.g. `feat(api): add todo filter`, <= 72 chars) "
                    "describing the staged diff. Return ONLY the commit message line, no quotes or markdown."
                )
                msg_prompt = f"{stat}\n\n{staged[:12000]}"
                draft = (
                    await self.ask(msg_role, msg_prompt, fast=True)
                    if self.config.get("fast_model")
                    else await self.ask(msg_role, msg_prompt)
                )
            commit_msg = draft.strip().splitlines()[0].strip("`\"' ")
        if not commit_msg:
            raise ValueError("Commit message cannot be empty.")
        if not self.approve(f"Commit staged changes with message: {commit_msg!r}?"):
            return "Cancelled."
        return self.git.run("commit", "-m", commit_msg)

    def _session_command(self, args: list[str]) -> str:
        from rich.table import Table

        op = args[0].lower() if args else "list"
        rest = args[1:]
        if op == "list":
            rows = self.sessions.list_all()
            if not rows:
                return "No sessions."
            if console.is_terminal:
                table = Table(title="Workspace Sessions", border_style="cyan")
                for col in ("", "ID", "Name", "Model", "Turns", "Pinned", "Updated"):
                    table.add_column(col)
                for row in rows:
                    mark = "*" if row["id"] == self.active_session_id else ""
                    table.add_row(
                        mark,
                        row["id"],
                        row["name"],
                        row["model"],
                        str(len(row["history"]) // 2),
                        str(len(row["pinned"])),
                        row["updated_at"],
                    )
                console.print(table)
                return ""
            return "\n".join(
                f"{'* ' if r['id'] == self.active_session_id else '  '}{r['id']} {r['name']} ({len(r['history']) // 2} turns)"
                for r in rows
            )
        if op == "new":
            self._sync_session()
            label = " ".join(rest).strip()
            branch, _ = self.git_info()
            created = self.sessions.create(
                label, model=self.model, branch=branch, summary="", history=[], pinned=[]
            )
            self.active_session_id = created["id"]
            self.write("context_summary.md", "")
            self.write("session_history.json", "[]")
            self.write("pinned.json", "[]")
            return f"Started new session `{created['name']}` ({created['id']})."
        if op == "resume":
            self._sync_session()
            key = " ".join(rest).strip() or None
            return self.resume_session(key)
        if op == "fork":
            self._sync_session()
            label = " ".join(rest).strip()
            forked = self.sessions.fork(self.active_session_id, label)
            self.active_session_id = forked["id"]
            return f"Forked into session `{forked['name']}` ({forked['id']})."
        if op == "delete" and rest:
            target = self.sessions.get(" ".join(rest).strip())
            if not target:
                raise ValueError("Session not found.")
            if target["id"] == self.active_session_id:
                raise ValueError("Cannot delete the active session. Switch sessions first.")
            if not self.approve(f"Delete session {target['name']} ({target['id']})?"):
                return "Cancelled."
            removed_id = self.sessions.delete(target["id"])
            return f"Deleted session {removed_id}."
        raise ValueError(f"Usage: /session {COMMANDS['session']}")

    def _context_report(self) -> str:
        from rich.table import Table

        rules_names, rules_text = self.load_project_rules()
        pinned_files = self._load_pinned()
        pinned_text = self._pinned_context()
        tasks_text = self.read("TASKS.md")
        memory_text = self.read("context_summary.md")
        history_text = json.dumps(self._load_history())

        rows = [
            ("Project Rules", ", ".join(rules_names) or "(none)", estimate(rules_text)),
            ("Pinned Files", ", ".join(pinned_files) or "(none)", estimate(pinned_text)),
            ("Tasks (TASKS.md)", f"{len(tasks_text.splitlines())} lines", estimate(tasks_text)),
            ("Conversation Summary", "context_summary.md", estimate(memory_text)),
            ("Structured History", f"{len(self._load_history()) // 2} turns", estimate(history_text)),
        ]
        total = sum(tok for _, _, tok in rows)
        limit = capacity(self.model, self.context_window)
        pct = min(100, (100 * total) // max(limit, 1))

        if console.is_terminal:
            table = Table(
                title=f"Context Budget — ~{total:,} / {limit:,} tokens ({pct}%)",
                border_style="cyan",
            )
            table.add_column("Source", style="bold cyan")
            table.add_column("Details", style="dim")
            table.add_column("Est. Tokens", justify="right")
            for label, detail, tok in rows:
                table.add_row(label, detail, f"{tok:,}")
            console.print(table)
            return ""
        lines = [f"Context: ~{total} / {limit} tokens ({pct}%)"]
        lines.extend(f"- {label}: ~{tok} tok ({detail})" for label, detail, tok in rows)
        return "\n".join(lines)

    async def tdd_loop(self, max_attempts: int = 3) -> str:
        """Run test_command and autonomously diagnose/patch failures until green."""
        command = self.test_command()
        if not command:
            raise ValueError(
                "No test_command configured. Set one with `/config test_command <cmd>`."
            )
        attempts = min(max(int(max_attempts), 1), 5)
        for attempt in range(1, attempts + 1):
            result = await asyncio.to_thread(self.tools.run_shell_command, command, 600)
            if console.is_terminal:
                console.print(f"[bold cyan]TDD pass {attempt}/{attempts}:[/] {result.splitlines()[0]}")
            if result.split()[:2] == ["Exit", "0"]:
                return f"Tests passed on attempt {attempt}/{attempts}.\n{result}"
            if result == "Command declined.":
                return "TDD loop cancelled (test command declined)."
            if attempt == attempts:
                return f"Tests still failing after {attempts} attempts.\n{result}"
            await self.chat(
                f"Test command `{command}` failed (attempt {attempt}/{attempts}). "
                f"Inspect the relevant source/test files, fix the root cause with apply_diff/apply_multi_diff/write_file, "
                f"and keep changes minimal.\n\nFailure output:\n{result[-10000:]}"
            )
        return "TDD loop completed."

    def _model_command(self, tail: str) -> str:
        from rich.table import Table

        if not tail:
            return self.model
        if tail == "list":
            with spinning("Discovering available models"):
                models = discover_models(
                    api_base=self.config.get("openai_api_base"),
                    ollama_host=self.config.get("ollama_api_base"),
                )
            self._last_discovered_models = models
            if console.is_terminal:
                table = Table(title="Available Models (use `/model <#>` or `/model <id>`)", border_style="cyan")
                for col in ("#", "Source", "Model ID", "Endpoint / Note"):
                    table.add_column(col)
                for idx, item in enumerate(models, 1):
                    active = " *" if item["model"] == self.model else ""
                    table.add_row(str(idx), item["source"], item["model"] + active, item["note"])
                console.print(table)
                return ""
            return "\n".join(
                f"{idx}. [{m['source']}] {m['model']}" for idx, m in enumerate(models, 1)
            )
        selected = tail
        if tail.isdecimal():
            idx = int(tail)
            if not self._last_discovered_models:
                self._last_discovered_models = discover_models(
                    api_base=self.config.get("openai_api_base")
                )
            if not 1 <= idx <= len(self._last_discovered_models):
                raise ValueError("Model index out of range. Run `/model list` first.")
            selected = self._last_discovered_models[idx - 1]["model"]
        if not MODEL_PATTERN.fullmatch(selected):
            raise ValueError(
                "Use gemini-*, ollama/<model>, ollama_chat/<model>, openai/<model>, "
                "anthropic/<model>, vertex_ai/<model>, azure/<model>, groq/<model>, "
                "mistral/<model>, or openrouter/<model>."
            )
        self.config["model"] = selected
        self.write("config.json", json.dumps(self.config, indent=2))
        self._sync_session()
        record(self.state, "MODEL", selected)
        return self.model

    async def workflow(self, request: str) -> str:
        from google.adk import Event, Workflow
        from google.adk.workflow import JoinNode

        from .model_factory import run_agent

        read_tools = [
            self.tools.list_dir,
            self.tools.find_files,
            self.tools.search_files,
            self.tools.read_file,
        ]
        brief = request
        attempt = 0
        max_brief_chars = max(16_000, capacity(self.model, self.context_window) * 2)

        def _bounded(text: str) -> str:
            return text[-max_brief_chars:] if len(text) > max_brief_chars else text

        async def requirements():
            with spinning("Workflow [1/6]: Requirements"):
                return await self.ask(
                    "Produce requirements and acceptance criteria.", request, read_tools
                )

        async def architecture(node_input: str):
            with spinning("Workflow [2/6]: Architecture"):
                return (
                    node_input
                    + "\n\n## Architecture\n"
                    + await self.ask(
                        "Develop a high-level architecture.", _bounded(node_input), read_tools
                    )
                )

        async def interfaces(node_input: str):
            with spinning("Workflow [3/6]: Interfaces"):
                return (
                    node_input
                    + "\n\n## Interfaces\n"
                    + await self.ask(
                        "Specify interfaces and low-level design.",
                        _bounded(node_input),
                        read_tools,
                    )
                )

        def approval(node_input: str):
            nonlocal brief
            brief = _bounded(node_input)
            console.print(brief)
            allowed = self.approve("Approve this architecture for implementation?")
            return Event(route="approved" if allowed else "declined", output=brief)

        def declined():
            return Event(message="Workflow stopped at design review.")

        async def standards(node_input: str):
            with spinning("Workflow [4/6]: Coding standards"):
                return await self.ask("Define coding standards.", _bounded(node_input), read_tools)

        async def tests(node_input: str):
            with spinning("Workflow [4/6]: Test plan"):
                return await self.ask(
                    "Design acceptance tests and verification commands.",
                    _bounded(node_input),
                    read_tools,
                )

        def specification(node_input: dict):
            nonlocal brief
            sections = [
                f"## {key}\n{value}" for key, value in sorted(node_input.items())
            ]
            brief = _bounded(brief + "\n\n" + "\n\n".join(sections))
            return brief

        async def implement(node_input: str):
            return await self.chat(
                "Implement and test the design.\n" + _bounded(node_input),
                record_memory=False,
            )

        async def review(node_input: str):
            nonlocal attempt
            attempt += 1
            with spinning(f"Workflow [6/6]: Review pass {attempt}/3"):
                verdict = await self.ask(
                    "Review the actual changes and verify acceptance tests. "
                    "Begin with PASS or REVISE.",
                    _bounded(brief + "\n\n" + node_input),
                    [*read_tools, self.tools.run_shell_command],
                )
            command = self.test_command()
            if command and verdict.startswith("PASS"):
                verification = await asyncio.to_thread(
                    self.tools.run_shell_command, command, 600
                )
                if verification.split()[:2] != ["Exit", "0"]:
                    verdict = "REVISE verification incomplete:\n" + verification
            done = verdict.startswith("PASS") or attempt >= 3
            return Event(
                route="finish" if done else "revise",
                output=verdict if done else _bounded(brief + "\nReviewer feedback: " + verdict),
            )

        def finish(node_input: str):
            self.write(
                "context_summary.md",
                self.read("context_summary.md")
                + f"\nDeveloper: /workflow {request}\nAssistant: {node_input}\n",
            )
            return Event(message=node_input)

        join = JoinNode(name="join_plans")
        graph = Workflow(
            name="coding_workflow",
            edges=[
                ("START", requirements, architecture, interfaces, approval),
                (approval, {"approved": standards, "declined": declined}),
                (approval, {"approved": tests}),
                ((standards, tests), join, specification, implement, review),
                (review, {"finish": finish, "revise": implement}),
            ],
        )
        return await run_agent(graph, request, graph=True)

    def checkpoint(self, args: list[str]) -> str:
        if not args or args == ["list"]:
            entries = sorted(self.checkpoints.directory.glob("chk_*"))
            return (
                "\n".join(
                    p.name
                    + " "
                    + json.loads((p / "manifest.json").read_text(encoding="utf8"))["label"]
                    for p in entries
                )
                or "No checkpoints."
            )
        if args[0] == "create":
            return self.checkpoints.create(
                scrub(" ".join(args[1:])),
                self.model,
                estimate(self.read("context_summary.md")),
            )
        if len(args) == 2 and args[0] == "restore":
            return self.checkpoints.restore(args[1], self.approve)
        if len(args) == 2 and args[0] == "delete":
            return self.checkpoints.delete(args[1], self.approve)
        raise ValueError("Use /checkpoint list, create, restore, or delete.")

    def _task_command(self, tail: str) -> str:
        if not tail or tail == "list":
            return self._format_tasks()
        parts = tail.split(None, 1)
        sub = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if sub == "add" and rest:
            with (self.state / "TASKS.md").open("a", encoding="utf8") as stream:
                stream.write(f"- [ ] {scrub(rest)}\n")
            return self._format_tasks()
        if sub in {"done", "rm"} and rest.isdecimal():
            target_idx = int(rest)
            lines = self.read("TASKS.md").splitlines()
            task_indices = [
                i for i, line in enumerate(lines) if line.lstrip().startswith("- [")
            ]
            if not 1 <= target_idx <= len(task_indices):
                raise ValueError(f"Task #{target_idx} not found.")
            line_idx = task_indices[target_idx - 1]
            if sub == "done":
                line = lines[line_idx]
                if "- [ ]" in line:
                    lines[line_idx] = line.replace("- [ ]", "- [x]", 1)
                elif "- [x]" in line or "- [X]" in line:
                    lines[line_idx] = re.sub(r"- \[[xX]\]", "- [ ]", line, count=1)
            else:
                lines.pop(line_idx)
            self.write("TASKS.md", "\n".join(lines).rstrip() + "\n")
            return self._format_tasks()
        with (self.state / "TASKS.md").open("a", encoding="utf8") as stream:
            stream.write(f"- [ ] {scrub(tail)}\n")
        return self._format_tasks()

    def _format_tasks(self) -> str:
        text = self.read("TASKS.md")
        out = []
        idx = 0
        for line in text.splitlines():
            if line.lstrip().startswith("- ["):
                idx += 1
                out.append(f"{idx}. {line}")
            else:
                out.append(line)
        return "\n".join(out) + ("\n" if text.endswith("\n") else "")

    def _config_command(self, args: list[str]) -> str:
        if not args:
            return json.dumps(self.config, indent=2)
        key = args[0]
        if key not in CONFIG_KEYS:
            raise ValueError(
                f"Unknown config key {key!r}. Supported: {', '.join(sorted(CONFIG_KEYS))}."
            )
        if len(args) == 1:
            return json.dumps({key: self.config.get(key)}, indent=2)
        raw_val = " ".join(args[1:]).strip()
        if key in {"notifications", "shell_mode", "sandbox_shell", "repo_map"}:
            lowered = raw_val.lower()
            if lowered not in {"true", "false", "1", "0", "yes", "no"}:
                raise ValueError(f"{key} expects true or false.")
            val: object = lowered in {"true", "1", "yes"}
            self.config[key] = val
            if key == "notifications":
                self.tools.notifications = bool(val)
            elif key == "shell_mode":
                self.tools.shell_mode = bool(val)
            elif key == "sandbox_shell":
                self.tools.sandbox_shell = bool(val)
        elif key == "fast_model":
            if raw_val.lower() in {"none", "unset", "off", ""}:
                self.config.pop("fast_model", None)
            elif not MODEL_PATTERN.fullmatch(raw_val):
                raise ValueError("Unsupported fast_model identifier.")
            else:
                self.config["fast_model"] = raw_val
        elif key == "context_window":
            if raw_val.lower() in {"none", "auto", "default"}:
                self.config.pop("context_window", None)
            elif raw_val.isdecimal() and int(raw_val) >= 1024:
                self.config["context_window"] = int(raw_val)
            else:
                raise ValueError("context_window expects an integer >= 1024 or 'auto'.")
        elif key == "max_tokens":
            if raw_val.lower() in {"none", "auto", "default"}:
                self.config.pop("max_tokens", None)
            elif raw_val.isdecimal() and int(raw_val) >= 256:
                self.config["max_tokens"] = int(raw_val)
            else:
                raise ValueError("max_tokens expects an integer >= 256 or 'auto'.")
        elif key == "request_timeout":
            if raw_val.lower() in {"none", "auto", "default"}:
                self.config.pop("request_timeout", None)
            elif raw_val.isdecimal() and 5 <= int(raw_val) <= 600:
                self.config["request_timeout"] = int(raw_val)
            else:
                raise ValueError("request_timeout expects 5..600 seconds or 'auto'.")
        elif key == "model":
            if not MODEL_PATTERN.fullmatch(raw_val):
                raise ValueError("Unsupported model identifier.")
            self.config["model"] = raw_val
        elif key == "mode":
            self.set_mode(raw_val)
            return json.dumps(self.config, indent=2)
        elif raw_val.lower() in {"none", "unset", ""}:
            self.config.pop(key, None)
        else:
            self.config[key] = raw_val
        self.write("config.json", json.dumps(self.config, indent=2))
        return json.dumps(self.config, indent=2)

    def git_command(self, name: str, args: list[str]) -> str:
        if name == "diff":
            staged = "--staged" in args
            paths = [p for p in args if p != "--staged"]
            show_patch(
                scrub(
                    self.git.run(
                        "diff",
                        *(["--cached"] if staged else []),
                        "--",
                        *(paths or ["."]),
                        ":(exclude).agent",
                    )
                )
            )
            return ""
        if name == "branch":
            if not args:
                return self.git.run("branch", "-v")
            if len(args) == 2 and args[0] == "-d" and not args[1].startswith("-"):
                if self.approve(f"Delete branch {args[1]}?"):
                    return self.git.run("branch", "-d", args[1])
                return "Cancelled."
            if len(args) == 1 and not args[0].startswith("-"):
                target = args[0]
                try:
                    self.git.run("show-ref", "--verify", f"refs/heads/{target}")
                    exists = True
                except ValueError:
                    exists = False
                if exists:
                    return self.git.run("switch", target)
                return self.git.run("switch", "-c", target)
        if name == "merge":
            return self.merge(args)
        if name == "stash":
            op, *rest = args or ["list"]
            if op == "list" and not rest:
                return self.git.run("stash", "list")
            if op == "push":
                return self.git.run(
                    "stash",
                    "push",
                    "-u",
                    "-m",
                    " ".join(rest) or "adk-coder",
                    "--",
                    ".",
                    ":(exclude).agent",
                )
            if (
                op == "drop"
                and len(rest) == 1
                and re.fullmatch(r"stash@\{\d+\}", rest[0])
            ):
                return (
                    self.git.run("stash", "drop", *rest)
                    if self.approve(f"Drop {rest[0]}?")
                    else "Cancelled."
                )
            if op in {"show", "pop", "apply"} and (
                not rest or len(rest) == 1 and re.fullmatch(r"stash@\{\d+\}", rest[0])
            ):
                return self.git.run(
                    "stash", op, *(["-p"] if op == "show" else []), *rest
                )
        if name in {"undo", "revert"} and len(args) == 1:
            path = self.tools.path(args[0]).relative_to(self.root).as_posix()
            show_patch(
                scrub(self.git.run("--literal-pathspecs", "diff", "HEAD", "--", path))
            )
            if self.approve(f"Discard staged and unstaged edits to {path}?"):
                return self.git.run(
                    "--literal-pathspecs",
                    "restore",
                    "--source=HEAD",
                    "--staged",
                    "--worktree",
                    "--",
                    path,
                )
            return "Cancelled."
        raise ValueError(f"Usage: /{name} {COMMANDS[name]}")

    def test_command(self):
        command = self.config.get("test_command")
        if not command:
            for marker, cmd in [
                ("tests", "python -m pytest"),
                ("package.json", "npm test"),
                ("go.mod", "go test ./..."),
                ("pom.xml", "mvn -B test"),
                ("Cargo.toml", "cargo test"),
            ]:
                if (self.root / marker).exists():
                    command = cmd
                    break
        return command

    def merge(self, args: list[str]) -> str:
        modes = [arg for arg in args if arg.startswith("-")]
        targets = [arg for arg in args if not arg.startswith("-")]
        if len(targets) > 1 or modes not in ([], ["--squash"], ["--rebase"]):
            raise ValueError("Usage: /merge [target] [--squash|--rebase]")
        target = targets[0] if targets else (self.git.default_branch() or "main")
        source = self.git.run("branch", "--show-current")
        if not source or source == target:
            raise ValueError("Start on the feature branch and specify its target.")
        self.git.run("show-ref", "--verify", f"refs/heads/{target}")
        if self.git.run("status", "--porcelain", "--", ".", ":(exclude).agent"):
            raise ValueError("Commit or stash workspace changes before merging.")
        self.git.run("merge-tree", "--write-tree", target, source)
        command = self.test_command()
        if command:
            result = self.tools.run_shell_command(command, 600)
            console.print(result)
            if result.split()[:2] != ["Exit", "0"]:
                raise ValueError("Pre-merge verification did not pass.")
        if not self.approve(f"Integrate {source} into {target} {tuple(modes)}?"):
            return "Cancelled."
        if modes == ["--rebase"]:
            try:
                self.git.run("rebase", target)
            except ValueError as exc:
                try:
                    self.git.run("rebase", "--abort")
                except ValueError:
                    pass
                raise ValueError(
                    f"Rebase onto {target} conflicted and was safely aborted: {exc}"
                ) from exc
        self.git.run("switch", target)
        flags = ["--ff-only"] if modes == ["--rebase"] else modes
        result = self.git.run("merge", "--no-edit", *flags, "--", source)
        if modes == ["--squash"]:
            result = (result + "\n" if result else "") + (
                "Squashed changes are staged. Run `git commit` to finalize."
            )
        return result

    async def sync(self, args: list[str]) -> str:
        pr_title = ""
        if args and args[0] == "--title" and len(args) >= 2:
            pr_title = " ".join(args[1:]).strip()
            args = []
        if not args:
            current = self.git.run("branch", "--show-current")
            base = self.git.default_branch()
            branch_commits = ""
            branch_diff = ""
            if base and current and current != base:
                branch_commits = self.git.run("log", "--oneline", f"{base}..HEAD")
                branch_diff = self.git.run(
                    "diff", "--stat", f"{base}...HEAD", "--", ".", ":(exclude).agent"
                )
            working_diff = self.git.run(
                "diff", "--stat", "HEAD", "--", ".", ":(exclude).agent"
            )
            status_short = self.git.run(
                "status", "--short", "--", ".", ":(exclude).agent"
            )
            evidence = "\n\n".join(
                section
                for section in (
                    f"Branch: {current} (base: {base or 'HEAD'})",
                    f"Commits:\n{branch_commits}" if branch_commits else "",
                    f"Branch diff:\n{branch_diff}" if branch_diff else "",
                    f"Working tree diff:\n{working_diff}" if working_diff else "",
                    f"Status:\n{status_short}" if status_short else "",
                    self.read("TASKS.md"),
                    self.read("CHANGELOG.md"),
                )
                if section.strip()
            )
            with spinning("Drafting pull request description"):
                body = await self.ask(
                    "Draft a Markdown PR description. Cover changes, verification "
                    "and risks. Use only the supplied evidence.",
                    evidence,
                )
            self.write("PULL_REQUEST.md", body)
            destination = self.state / "PULL_REQUEST.md"
            if (pr_title or sys.stdin.isatty()) and self.approve(
                "Create a GitHub pull request using this draft?"
            ):
                if not shutil.which("gh"):
                    return (
                        f"Draft saved to {destination}. "
                        "Install and authenticate the GitHub CLI (`gh`) to open PRs directly."
                    )
                title = pr_title or input("PR title: ").strip()
                if title:
                    await asyncio.to_thread(
                        subprocess.run,
                        [
                            "gh",
                            "pr",
                            "create",
                            "--title",
                            title,
                            "--body-file",
                            str(destination),
                        ],
                        cwd=self.root,
                        check=True,
                    )
            return str(destination)
        if args[0] != "--release" or len(args) > 2:
            raise ValueError("Usage: /sync [--title <text>|--release [auto|vX.Y.Z]]")
        tags = self.git.run(
            "tag", "--merged", "HEAD", "--sort=-version:refname"
        ).splitlines()
        base = next((t for t in tags if re.fullmatch(r"v?\d+\.\d+\.\d+", t)), "")
        rev = f"{base}..HEAD" if base else "HEAD"
        messages = self.git.run("log", "--format=%B", rev)
        version = (
            next_version(base or "v0.0.0", messages)
            if args[1:] in ([], ["auto"])
            else args[1]
        )
        if not re.fullmatch(r"v?\d+\.\d+\.\d+", version):
            raise ValueError("Expected vX.Y.Z")
        self.write(
            "RELEASE_NOTES.md",
            f"# Release {version}\n\n"
            + self.git.run("log", "--format=- %s", rev)
            + "\n",
        )
        if self.approve(f"Create local Git tag {version}?"):
            self.git.run("tag", version)
        return str(self.state / "RELEASE_NOTES.md")

    def _read_clipboard(self) -> str:
        commands = (
            [["pbpaste"]]
            if sys.platform == "darwin"
            else (
                [["powershell", "-NoProfile", "-Command", "Get-Clipboard"]]
                if sys.platform == "win32"
                else [["wl-paste"], ["xclip", "-o", "-selection", "clipboard"], ["xsel", "-ob"]]
            )
        )
        for cmd in commands:
            if shutil.which(cmd[0]):
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, errors="replace", timeout=5, check=False
                )
                if proc.returncode == 0:
                    return proc.stdout.strip()
        raise ValueError("Clipboard utility not available on this system.")

    def _start_settings_ui(self, port: int = 8089) -> str:
        if self._settings_server is not None:
            webbrowser.open(self._settings_url)
            return f"Config Studio already running at {self._settings_url} (live reload enabled)."
        from .config_ui import create_server, default_env_path

        env_path = default_env_path()
        server = create_server(
            env_path,
            self.root,
            port=port,
            on_save=lambda _data: self.reload_config(),
        )
        actual_port = server.server_address[1]
        self._settings_server = server
        self._settings_url = f"http://127.0.0.1:{actual_port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        if console.is_terminal:
            webbrowser.open(self._settings_url)
        return f"Config Studio running at {self._settings_url} (changes auto-reload in this session)."

    def _export_session(self, out_arg: str = "") -> str:
        target = self.tools.path(out_arg) if out_arg else (self.root / "docs/SESSION_EXPORT.md")
        history = self._load_history()
        pinned = self._load_pinned()
        sections = [
            f"# ADK Coder Session Export — {self.root.name}",
            f"- **Model**: `{self.model}`\n- **Mode**: `{self.mode}`\n- **Pinned Files**: {', '.join(f'`{p}`' for p in pinned) or '(none)'}",
            "## Tasks\n" + self.read("TASKS.md").strip(),
        ]
        if history:
            transcript = []
            for turn in history:
                speaker = "Developer" if turn["role"] == "user" else "Assistant"
                transcript.append(f"### {speaker}\n{turn['text']}")
            sections.append("## Conversation\n\n" + "\n\n".join(transcript))
        else:
            sections.append("## Conversation\n\n" + (self.read("context_summary.md").strip() or "_(empty)_"))
        markdown = "\n\n".join(sections).rstrip() + "\n"
        if not self.approve(f"Export session transcript to {target.relative_to(self.root)}?"):
            return "Cancelled."
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target, markdown)
        record(self.state, "EXPORT", str(target.relative_to(self.root)))
        return f"Exported session to {target}"

    async def execute(self, line: str) -> str:
        self.last_streamed = False
        if line.startswith("!"):
            return self.shell_escape(line[1:])
        if not line.startswith("/"):
            return await self.chat(line)
        command, _, tail = line.partition(" ")
        name = command[1:]
        tail = tail.strip()
        if name == "help":
            return render_help(COMMANDS)
        if name == "init":
            return f"Initialized {self.state}"
        if name == "status":
            return self.status()
        if name == "doctor":
            from .doctor import run_doctor

            return run_doctor(self, offline="--offline" in tail.split())
        if name == "map":
            symbol_map = build_repo_map(self.root, tail or ".")
            return symbol_map or "No supported source files found to index."
        if name == "paste":
            clip = self._read_clipboard()
            if not clip:
                return "Clipboard is empty."
            combined = f"{tail}\n\n{clip}" if tail else clip
            return await self.chat(combined)
        if name == "settings":
            port = int(tail) if tail.isdecimal() else 8089
            return self._start_settings_ui(port)
        if name == "export":
            return self._export_session(tail)
        if name == "mode":
            if not tail:
                return f"Current mode: {self.mode} (options: {', '.join(MODES)})"
            updated = self.set_mode(tail)
            record(self.state, "MODE", updated)
            return f"Switched mode to {updated}"
        if name == "plan":
            output = await self.plan_mode(tail)
            record(self.state, "PLAN", tail or "requirements.md")
            return output
        if name == "commit":
            output = await self.commit_changes(tail)
            record(self.state, "COMMIT", tail or "auto")
            return output
        if name == "undo-edit":
            count = int(tail) if tail.isdecimal() else 1
            return self.tools.undo_last_edit(count)
        if name == "clear":
            console.clear()
            return ""
        if name == "reset":
            if self.approve("Clear conversation memory and reset session approvals?"):
                self.write("context_summary.md", "")
                self._save_history([])
                self.tools.always_scopes.clear()
                record(self.state, "RESET", "conversation memory")
                return "Conversation memory cleared."
            return "Cancelled."
        if name == "model":
            return self._model_command(tail)
        if name == "context":
            return self._context_report()
        if name == "pin":
            if not tail:
                pinned = self._load_pinned()
                return "\n".join(pinned) if pinned else "No pinned files. Use `/pin <file>`."
            rel = self.tools.path(tail).relative_to(self.root).as_posix()
            if not (self.root / rel).is_file():
                raise ValueError(f"File not found: {rel}")
            pinned = self._load_pinned()
            if rel not in pinned:
                pinned.append(rel)
                self._save_pinned(pinned)
                record(self.state, "PIN", rel)
            return f"Pinned {rel} ({len(pinned)} total)."
        if name == "unpin":
            if not tail:
                raise ValueError("Usage: /unpin <file|all>")
            if tail == "all":
                self._save_pinned([])
                return "Unpinned all files."
            rel = self.tools.path(tail).relative_to(self.root).as_posix()
            pinned = self._load_pinned()
            if rel not in pinned:
                return f"{rel} was not pinned."
            pinned.remove(rel)
            self._save_pinned(pinned)
            return f"Unpinned {rel} ({len(pinned)} remaining)."
        if name == "explore":
            if not tail:
                raise ValueError("Usage: /explore <question>")
            return await self.explore_codebase(tail)
        if name == "skills":
            found = list_skills(self.state)
            if not found:
                return "No workspace skills found in .agent/skills/<name>/SKILL.md."
            return "\n".join(
                f"{meta['name']} — {meta['description']}" for meta in found.values()
            )
        if name == "skill":
            parts = tail.split(None, 1)
            if not parts:
                raise ValueError("Usage: /skill <name> [request]")
            skill_name = parts[0]
            req = parts[1].strip() if len(parts) > 1 else ""
            found = list_skills(self.state)
            if skill_name not in found:
                raise ValueError(f"Unknown skill {skill_name!r}. Run `/skills` to list available skills.")
            body = found[skill_name]["body"]
            combined = f"Follow this workspace skill ({skill_name}):\n\n{body}\n\nUser request:\n{req or 'Apply this skill to the workspace.'}"
            return await self.chat(combined)
        if name in {"tdd", "test-fix"}:
            attempts = int(tail) if tail.isdecimal() else 3
            output = await self.tdd_loop(attempts)
            record(self.state, "TDD", str(attempts))
            return output
        if name == "task":
            output = self._task_command(tail)
            if tail and tail != "list":
                record(self.state, "TASK", tail)
            return output
        if name == "changelog":
            return "\n".join(self.read("CHANGELOG.md").splitlines()[-30:])
        if name == "compact":
            output = await self.compact()
            record(self.state, "COMPACT", "")
            return output
        if name == "workflow":
            from .scaffolder import resolve_requirements

            req_text = resolve_requirements(self, tail)
            if not req_text:
                raise ValueError(
                    "Usage: /workflow <requirement|@requirements.md> "
                    "(or create requirements.md in the workspace)."
                )
            output = await self.workflow(req_text)
            record(self.state, "WORKFLOW", tail or "requirements.md")
            return output
        if name == "review":
            patch = self.git.run("diff", "HEAD", "--", ".", ":(exclude).agent")
            if not patch:
                return "No tracked changes."
            with spinning("Reviewing tracked changes"):
                return await self.ask(
                    "Review the changes for defects and missing tests.", patch
                )
        args = shlex.split(tail)
        if name == "commands":
            if not args or args == ["list"]:
                custom = list_custom_commands(self.state)
                return (
                    "\n".join(f"/{k} -> {p.relative_to(self.root)}" for k, p in custom.items())
                    if custom
                    else "No custom commands. Create one with `/commands new <name>` in .agent/commands/."
                )
            if len(args) == 2 and args[0] == "new":
                dest = create_custom_command(self.state, args[1])
                return f"Created custom command template {dest}"
            raise ValueError("Usage: /commands [list|new <name>]")
        if name == "hooks":
            if not args or args == ["list"]:
                loaded = self.hooks.load()
                return (
                    json.dumps(loaded, indent=2)
                    if loaded
                    else "No hooks configured. Run `/hooks init` to create .agent/hooks.json."
                )
            if args == ["init"]:
                dest = self.hooks.init_template()
                return f"Initialized hooks config at {dest}"
            raise ValueError("Usage: /hooks [list|init]")
        if name == "mcp":
            if not args or args == ["list"]:
                servers = self.mcp.load()
                _, notes = self.mcp.build_adk_toolsets()
                body = (
                    json.dumps({"mcpServers": servers}, indent=2)
                    if servers
                    else "No MCP servers configured. Use `/mcp add <name> <command> [args...]`."
                )
                return body + ("\n" + "\n".join(notes) if notes else "")
            if args[0] == "add" and len(args) >= 3:
                entry = self.mcp.add(args[1], args[2], args[3:])
                record(self.state, "MCP_ADD", args[1])
                return f"Added MCP server `{args[1]}`: {json.dumps(entry)}"
            if args[0] == "remove" and len(args) == 2:
                if not self.mcp.remove(args[1]):
                    raise ValueError(f"MCP server not found: {args[1]}")
                record(self.state, "MCP_REMOVE", args[1])
                return f"Removed MCP server `{args[1]}`."
            raise ValueError(f"Usage: /mcp {COMMANDS['mcp']}")
        if name == "session":
            output = self._session_command(args)
            if args and args[0] != "list":
                record(self.state, "SESSION", tail)
            return output
        if name == "config":
            output = self._config_command(args)
            if len(args) > 1:
                record(self.state, "CONFIG", tail)
            return output
        if name in {"audit", "scan", "fix"}:
            if len(args) > 1:
                raise ValueError(f"Usage: /{name} {COMMANDS[name]}")
            if name == "fix":
                from .fixer import fix

                output = await fix(self, args[0] if args else "")
                record(self.state, "FIX", tail or "all")
                return output
            from .audit import audit

            issues = await audit(self, args[0] if args else ".")
            record(self.state, "AUDIT", tail or ".")
            if (
                sys.stdin.isatty()
                and issues
                and self.approve(
                    f"Found {len(issues)} issue(s). Would you like to fix them now?"
                )
            ):
                from .fixer import fix

                return await fix(self, "all")
            return ""
        if name in {"analyze", "analyse"}:
            from .analyzer import analyze

            output = await analyze(self, args)
            record(self.state, "ANALYZE", tail or ".")
            return output
        if name == "debug":
            from .debugger import debug

            output = await debug(self, args)
            record(self.state, "DEBUG", tail or "auto")
            return output
        if name == "profile":
            from .profiles import handle_profile_command

            output = handle_profile_command(self, args)
            if args and args[0] != "list":
                record(self.state, "PROFILE", tail)
            return output
        if name == "testgen":
            from .testgen import generate_tests

            return await generate_tests(self, args)
        if name == "pr-review":
            from .pr_review import pr_review

            return await pr_review(self, args)
        if name == "diagram":
            from .diagram import generate_diagram

            return generate_diagram(self, args)
        if name == "resolve-conflicts":
            from .conflicts import resolve_conflicts

            return await resolve_conflicts(self, args)
        if name == "watch":
            from .watcher import watch

            return await watch(self, args)
        if name == "feature":
            from .analyzer import add_feature

            return await add_feature(self, args)
        if name == "security":
            if len(args) > 1:
                raise ValueError(f"Usage: /security {COMMANDS['security']}")
            from .security import security_scan

            report = security_scan(self, args[0] if args else ".")
            record(self.state, "SECURITY", tail or ".")
            if (
                sys.stdin.isatty()
                and report["findings"]
                and self.approve(
                    f"Found {len(report['findings'])} issue(s). Would you like to fix them now?"
                )
            ):
                from .fixer import fix

                return await fix(self, "all")
            return (
                ""
                if console.is_terminal
                else (
                    f"Scanned {report['files_scanned']} files: "
                    f"{len(report['security_findings'])} security, "
                    f"{len(report['solid_findings'])} SOLID findings."
                )
            )
        if name == "sbom":
            from .sbom import generate_sbom

            output = generate_sbom(self, args)
            record(self.state, "SBOM", tail or "cyclonedx")
            return output
        if name == "create":
            from .scaffolder import create_app

            output = await create_app(self, args)
            record(self.state, "CREATE", tail or "interactive")
            return output
        if name == "blueprint":
            from .scaffolder import blueprint

            output = await blueprint(self, args)
            record(self.state, "BLUEPRINT", tail)
            return output
        if name in {"scaffold", "new"}:
            from .scaffolder import scaffold

            output = scaffold(self, args)
            if args:
                record(self.state, "SCAFFOLD", tail)
            return output
        if name == "checkpoint":
            output = self.checkpoint(args)
        elif name == "sync":
            output = await self.sync(args)
        elif name in {"branch", "merge", "stash", "undo", "revert", "diff"}:
            output = self.git_command(name, args)
        elif name == "exit":
            return ""
        else:
            custom = list_custom_commands(self.state)
            if name in custom:
                rendered = render_custom_command(custom[name], tail)
                record(self.state, f"CMD:{name.upper()}", tail)
                return await self.chat(rendered)
            candidates = list(COMMANDS.keys()) + list(custom.keys())
            matches = difflib.get_close_matches(name, candidates, n=2, cutoff=0.6)
            if matches:
                suggestion = " or ".join(f"/{m}" for m in matches)
                raise ValueError(
                    f"Unknown command '/{name}'. Did you mean {suggestion}? (Use /help to list all commands.)"
                )
            raise ValueError(f"Unknown command '/{name}'. Use /help to see available commands.")
        if name not in {"diff"}:
            record(self.state, name.upper(), tail)
        return output
