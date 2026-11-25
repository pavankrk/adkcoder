"""Native standalone Desktop Agent IDE (`adk-coder --app` / `adk-coder-app`) powered by pywebview.

Uses macOS WKWebView / Windows Edge WebView2 (not a browser tab) backed by an
in-process localhost HTTP+SSE bridge directly attached to `AgentHarness`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import uuid
from contextlib import suppress
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config_ui import (
    _http_post_json,
    default_env_path,
    detect_provider,
    load_global_env,
    parse_env_file,
)
from .config_ui import (
    dispatch_request as dispatch_config_request,
)
from .diagram import analyze_structure, render_diagram_markdown
from .harness import AgentHarness
from .profiles import apply_profile_to_env, load_profiles, masked_profiles_summary
from .sbom import build_cyclonedx, build_spdx, discover_components
from .scaffolder import STACKS, scaffold
from .scrubber import scrub
from .security import security_scan
from .token_manager import capacity, estimate
from .tools import IGNORED_DIRS, MODES, _is_protected_part, atomic_write
from .ui import StreamDisplay, console


def _workspaces_registry_path(env_path: Path | None = None) -> Path:
    base = (env_path or default_env_path()).parent
    return base / "desktop_workspaces.json"


def _git_numstat(root: Path) -> tuple[int, int]:
    """Return (added_lines, deleted_lines) in working tree vs HEAD."""
    try:
        proc = subprocess.run(
            ["git", "diff", "--numstat", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=2.0,
            check=False,
        )
        if proc.returncode != 0:
            proc = subprocess.run(
                ["git", "diff", "--numstat"],
                cwd=root,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=2.0,
                check=False,
            )
        added = 0
        deleted = 0
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                if parts[0].isdigit():
                    added += int(parts[0])
                if parts[1].isdigit():
                    deleted += int(parts[1])
        return added, deleted
    except (OSError, subprocess.TimeoutExpired):
        return 0, 0


def _iter_tree(root: Path, limit: int = 350) -> list[str]:
    found: list[str] = []
    for parent, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS and not _is_protected_part(d))
        for name in sorted(files):
            if _is_protected_part(name):
                continue
            found.append(Path(parent, name).relative_to(root).as_posix())
            if len(found) >= limit:
                return found
    return found


def _build_step_entry(step_id: int, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    """Build a structured, scrubbed UI step card payload for a tool call."""
    raw = args or {}
    file_path = scrub(
        str(raw.get("file_path") or raw.get("path") or raw.get("dir_path") or "")
    ).strip()
    entry: dict[str, Any] = {
        "id": step_id,
        "name": name,
        "status": "running",
        "kind": "other",
        "file_path": file_path,
        "summary": name,
        "result_preview": "",
    }
    if name == "write_file":
        content = scrub(str(raw.get("content") or ""))
        entry.update(
            {
                "kind": "write",
                "summary": f"Writing {file_path}" if file_path else "Writing file",
                "content": content[:6000],
                "truncated": len(content) > 6000,
                "lines_added": len(content.splitlines()) if content else 0,
                "lines_removed": 0,
            }
        )
    elif name == "apply_diff":
        old_blk = scrub(str(raw.get("search_block") or ""))
        new_blk = scrub(str(raw.get("replace_block") or ""))
        entry.update(
            {
                "kind": "diff",
                "summary": f"Editing {file_path}" if file_path else "Editing file",
                "search_block": old_blk[:3000],
                "replace_block": new_blk[:3000],
                "lines_removed": len(old_blk.splitlines()) if old_blk else 0,
                "lines_added": len(new_blk.splitlines()) if new_blk else 0,
            }
        )
    elif name == "apply_multi_diff":
        edits = raw.get("edits") if isinstance(raw.get("edits"), list) else []
        old_parts = [scrub(str(e.get("search_block") or "")) for e in edits if isinstance(e, dict)]
        new_parts = [scrub(str(e.get("replace_block") or "")) for e in edits if isinstance(e, dict)]
        old_blk = "\n---\n".join(p for p in old_parts if p)
        new_blk = "\n---\n".join(p for p in new_parts if p)
        entry.update(
            {
                "kind": "diff",
                "summary": (
                    f"Editing {file_path} ({len(edits)} hunks)" if file_path else "Editing file"
                ),
                "search_block": old_blk[:3000],
                "replace_block": new_blk[:3000],
                "lines_removed": sum(len(p.splitlines()) for p in old_parts if p),
                "lines_added": sum(len(p.splitlines()) for p in new_parts if p),
            }
        )
    elif name == "delete_file":
        entry.update(
            {
                "kind": "delete",
                "summary": f"Deleting {file_path}" if file_path else "Deleting file",
            }
        )
    elif name == "run_shell_command":
        cmd = scrub(str(raw.get("command") or ""))[:300]
        entry.update(
            {
                "kind": "shell",
                "command": cmd,
                "summary": f"Running {cmd[:72]}" if cmd else "Running command",
            }
        )
    elif name == "read_file":
        entry.update(
            {
                "kind": "read",
                "summary": f"Reading {file_path}" if file_path else "Reading file",
            }
        )
    elif name == "search_files":
        q = scrub(str(raw.get("query") or ""))[:80]
        entry.update({"kind": "search", "summary": f'Searching "{q}"' if q else "Searching files"})
    elif name == "find_files":
        pat = scrub(str(raw.get("pattern") or ""))[:80]
        entry.update({"kind": "search", "summary": f'Finding "{pat}"' if pat else "Finding files"})
    elif name == "list_dir":
        entry.update({"kind": "read", "summary": f"Listing {file_path or '.'}"})
    else:
        entry["args"] = {k: scrub(str(v))[:120] for k, v in raw.items()}
    return entry


def load_desktop_html() -> str:
    override = os.environ.get("ADK_CODER_DESKTOP_HTML", "").strip()
    if override and Path(override).is_file():
        return Path(override).read_text(encoding="utf8")
    asset = Path(__file__).resolve().parent / "desktop_assets" / "app.html"
    if asset.is_file():
        return asset.read_text(encoding="utf8")
    return "<!doctype html><html><body><h2>ADK Coder Desktop UI asset missing.</h2></body></html>"


class _DesktopStreamProxy(StreamDisplay):
    """StreamDisplay subclass that pushes live token, thought, and tool events to SSE."""

    def __init__(self, emit) -> None:
        super().__init__()
        self._emit = emit
        self._enabled = True

    def on_thought(self, chunk: str) -> None:
        if not chunk:
            return
        self._emit({"type": "thought", "text": scrub(chunk)})

    def on_text(self, chunk: str) -> None:
        if not chunk:
            return
        if not self.started_text and not chunk.strip():
            return
        self.started_text = True
        self.streamed_text = True
        self._buffer.append(chunk)
        self._emit({"type": "text", "text": scrub(chunk)})

    def before_tool_call(self) -> None:
        return

    def after_tool_result(self) -> None:
        return

    def finish(self, *, final_text: str = "") -> None:
        return

    def render_footer(self, *, fallback_output_text: str = "") -> None:
        return


class DesktopController:
    """Thread-safe controller owning workspace harnesses, SSE queues, approvals, and abort."""

    def __init__(self, env_path: Path, initial_workspace: Path) -> None:
        self.env_path = env_path
        self._lock = threading.Lock()
        self._harnesses: dict[str, AgentHarness] = {}
        self._active_ws: str = str(initial_workspace.resolve())
        self._workspaces: list[str] = self._load_saved_workspaces(self._active_ws)
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []
        self._busy = False
        self._abort_requested = False
        self._turn_seq = 0
        self._done_turn_id = 0
        self._turn_snapshot: dict[str, Any] = {
            "turn_id": 0,
            "done_turn_id": 0,
            "status": "idle",
            "text": "",
            "thought": "",
            "steps": [],
            "error": "",
            "pending_approval": None,
        }
        self._active_abort: asyncio.Event | None = None
        self._active_loop: asyncio.AbstractEventLoop | None = None
        self._active_task: asyncio.Task[Any] | None = None
        self._pending_approvals: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
        self._window: Any = None  # pywebview Window reference when running natively

    def attach_window(self, window: Any) -> None:
        self._window = window

    def _load_saved_workspaces(self, initial: str) -> list[str]:
        reg = _workspaces_registry_path(self.env_path)
        items: list[str] = []
        if reg.is_file():
            try:
                raw = json.loads(reg.read_text(encoding="utf8"))
                if isinstance(raw, list):
                    for p in raw:
                        if isinstance(p, str) and Path(p).is_dir() and p not in items:
                            items.append(str(Path(p).resolve()))
            except (OSError, json.JSONDecodeError):
                pass
        if initial not in items and Path(initial).is_dir():
            items.insert(0, initial)
        self._save_workspaces(items)
        return items

    def _save_workspaces(self, items: list[str]) -> None:
        reg = _workspaces_registry_path(self.env_path)
        try:
            reg.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(reg, json.dumps(items[:20], indent=2) + "\n", mode=0o600)
        except OSError:
            pass

    def emit(self, event: dict[str, Any]) -> None:
        critical = event.get("type") in {"turn_done", "turn_error", "approval_request", "turn_start"}
        with self._lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait(event)
                except queue.Full:
                    if critical:
                        with suppress(queue.Empty, queue.Full):
                            q.get_nowait()
                            q.put_nowait(event)

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=4000)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _interactive_approve(self, message: str) -> str | bool:
        """Pause the worker thread and ask the desktop UI to Allow / Always / Deny."""
        req_id = uuid.uuid4().hex[:10]
        ev = threading.Event()
        holder: dict[str, Any] = {"decision": False}
        clean_msg = scrub(message)
        with self._lock:
            self._pending_approvals[req_id] = (ev, holder)
            self._turn_snapshot["pending_approval"] = {"id": req_id, "message": clean_msg}
        self.emit({"type": "approval_request", "id": req_id, "message": clean_msg})
        ev.wait(timeout=300)
        with self._lock:
            self._pending_approvals.pop(req_id, None)
            if (self._turn_snapshot.get("pending_approval") or {}).get("id") == req_id:
                self._turn_snapshot["pending_approval"] = None
        return holder["decision"]

    def resolve_approval(self, req_id: str, decision: str) -> bool:
        with self._lock:
            entry = self._pending_approvals.get(req_id)
            if (self._turn_snapshot.get("pending_approval") or {}).get("id") == req_id:
                self._turn_snapshot["pending_approval"] = None
        if not entry:
            return False
        ev, holder = entry
        clean = decision.strip().lower()
        if clean == "always":
            holder["decision"] = "always"
        else:
            holder["decision"] = clean in {"allow", "yes", "y", "true"}
        ev.set()
        return True

    def chat_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "busy": self._busy,
                "turn_id": self._turn_seq,
                "done_turn_id": self._done_turn_id,
                "status": self._turn_snapshot.get("status", "idle"),
                "text": self._turn_snapshot.get("text", ""),
                "thought": self._turn_snapshot.get("thought", ""),
                "steps": list(self._turn_snapshot.get("steps") or []),
                "error": self._turn_snapshot.get("error", ""),
                "pending_approval": self._turn_snapshot.get("pending_approval"),
            }

    def _clean_workspace_path(self, workspace: str | None) -> str:
        raw = (workspace or self._active_ws or "").strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
            raw = raw[1:-1].strip()
        if not raw:
            raise ValueError("Workspace path cannot be empty.")
        return str(Path(raw).expanduser().resolve())

    def _sync_harness_with_global_config(self, h: AgentHarness, *, apply_mode: bool = False) -> None:
        """Keep a workspace harness's model & endpoint in sync with active profile / global .env."""
        env_vals = parse_env_file(self.env_path)
        for key, val in env_vals.items():
            if val:
                os.environ[key] = val

        store = load_profiles(self.env_path)
        active_name = store.get("active") or ""
        active_prof = (store.get("profiles") or {}).get(active_name) if active_name else None

        target_model = ""
        provider = ""
        base_url = ""
        target_mode = ""

        # Prefer global .env model when present (saved by /api/save or /api/profiles/use),
        # falling back to the active profile if .env does not specify one.
        if active_prof and (
            not env_vals.get("ADK_CODER_MODEL")
            or env_vals.get("ADK_CODER_MODEL") == active_prof.get("model")
        ):
            apply_profile_to_env(active_prof)
            target_model = str(active_prof.get("model") or "").strip()
            provider = str(active_prof.get("provider") or detect_provider(target_model)).strip()
            base_url = str(active_prof.get("base_url") or "").strip()
            target_mode = str(active_prof.get("mode") or "").strip().lower()
        elif env_vals.get("ADK_CODER_MODEL"):
            target_model = env_vals["ADK_CODER_MODEL"].strip()
            provider = detect_provider(target_model)
            if provider == "openai-compatible":
                base_url = (
                    env_vals.get("OPENAI_API_BASE") or env_vals.get("OPENAI_BASE_URL") or ""
                ).strip()
            elif provider == "ollama":
                base_url = (
                    env_vals.get("OLLAMA_API_BASE") or env_vals.get("OLLAMA_HOST") or ""
                ).strip()
            target_mode = (env_vals.get("ADK_CODER_MODE") or "").strip().lower()

        if not target_model:
            return

        changed = False
        if h.config.get("model") != target_model:
            h.config["model"] = target_model
            changed = True

        if provider == "openai-compatible":
            if base_url and h.config.get("openai_api_base") != base_url:
                h.config["openai_api_base"] = base_url
                changed = True
            if "ollama_api_base" in h.config:
                h.config.pop("ollama_api_base", None)
                changed = True
        elif provider == "ollama":
            if base_url and h.config.get("ollama_api_base") != base_url:
                h.config["ollama_api_base"] = base_url
                changed = True
            if "openai_api_base" in h.config:
                h.config.pop("openai_api_base", None)
                changed = True
        else:
            for stale_key in ("openai_api_base", "ollama_api_base"):
                if stale_key in h.config:
                    h.config.pop(stale_key, None)
                    changed = True

        if apply_mode and target_mode in MODES and h.mode != target_mode:
            h.tools.mode = target_mode
            h.config["mode"] = target_mode
            changed = True

        if changed:
            h.write("config.json", json.dumps(h.config, indent=2) + "\n")

    def get_harness(self, workspace: str | None = None) -> AgentHarness:
        key = self._clean_workspace_path(workspace)
        if not Path(key).is_dir():
            raise ValueError(f"Workspace not found: {key}")
        with self._lock:
            if key not in self._workspaces:
                self._workspaces.insert(0, key)
                self._save_workspaces(self._workspaces)
            h = self._harnesses.get(key)
            if h is None:
                h = AgentHarness(key, approve=self._interactive_approve)
                self._harnesses[key] = h
        self._sync_harness_with_global_config(h)
        return h

    def select_workspace(self, workspace: str) -> dict[str, Any]:
        h = self.get_harness(workspace)
        self._sync_harness_with_global_config(h)
        resolved = str(h.root)
        with self._lock:
            self._active_ws = resolved
            self._workspaces = [resolved] + [w for w in self._workspaces if w != resolved]
            self._save_workspaces(self._workspaces)
        if self._window is not None and hasattr(self._window, "set_title"):
            try:
                self._window.set_title(f"ADK Coder — {h.root.name}")
            except (AttributeError, RuntimeError, OSError):
                return {"ok": True, **self.bootstrap_state()}
        return {"ok": True, **self.bootstrap_state()}

    def remove_workspace(self, workspace: str) -> dict[str, Any]:
        raw = (workspace or "").strip().strip('"').strip("'")
        target = str(Path(raw).expanduser().resolve()) if raw else ""
        with self._lock:
            self._workspaces = [w for w in self._workspaces if w != target and w != raw]
            if not self._workspaces:
                self._workspaces = [self._active_ws]
            elif self._active_ws in {target, raw}:
                self._active_ws = self._workspaces[0]
            self._save_workspaces(self._workspaces)
        return {"ok": True, **self.bootstrap_state()}

    def _os_folder_picker_fallback(self) -> tuple[bool, str | None, str]:
        """Fallback native folder picker using OS scripting if pywebview dialog fails."""
        try:
            if sys.platform == "darwin" and shutil.which("osascript"):
                proc = subprocess.run(
                    [
                        "osascript",
                        "-e",
                        'POSIX path of (choose folder with prompt "Select Workspace Folder")',
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    return True, proc.stdout.strip(), ""
                if "User canceled" in proc.stderr or "(-128)" in proc.stderr:
                    return True, None, ""
                return False, None, proc.stderr.strip() or "Folder picker failed."
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, None, str(exc)
        return False, None, "Native folder picker unavailable."

    def pick_workspace_dialog(self) -> dict[str, Any]:
        picked = ""
        err_msg = ""
        if self._window is not None:
            try:
                import webview

                dialog_type = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or getattr(
                    webview, "FOLDER_DIALOG", 20
                )
                chosen = self._window.create_file_dialog(dialog_type, directory=self._active_ws)
                if chosen is None or chosen == () or chosen == []:
                    return {"ok": True, "cancelled": True}
                picked = chosen if isinstance(chosen, str) else str(chosen[0])
            except Exception as exc:  # noqa: BLE001
                err_msg = scrub(str(exc))

        if not picked:
            ok, fb_path, fb_err = self._os_folder_picker_fallback()
            if ok:
                if not fb_path:
                    return {"ok": True, "cancelled": True}
                picked = fb_path
            else:
                return {
                    "ok": False,
                    "error": err_msg or fb_err or "Native folder picker is unavailable. Enter a folder path instead.",
                }

        try:
            return {"ok": True, "state": self.select_workspace(picked)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": scrub(str(exc))}

    def bootstrap_state(self) -> dict[str, Any]:
        h = self.get_harness(self._active_ws)
        ws_entries: list[dict[str, Any]] = []
        for ws_path in list(self._workspaces):
            p = Path(ws_path)
            if not p.is_dir():
                continue
            try:
                wh = self.get_harness(ws_path)
                branch, dirty = wh.git_info()
                added, deleted = _git_numstat(p)
                sessions = wh.sessions.list_all()[:12]
                ws_entries.append(
                    {
                        "path": str(wh.root),
                        "name": wh.root.name,
                        "branch": branch,
                        "dirty": dirty,
                        "added": added,
                        "deleted": deleted,
                        "active": str(wh.root) == self._active_ws,
                        "active_session_id": wh.active_session_id,
                        "sessions": [
                            {
                                "id": s["id"],
                                "name": s["name"],
                                "model": s.get("model", ""),
                                "updated_at": s.get("updated_at", ""),
                            }
                            for s in sessions
                        ],
                    }
                )
            except (OSError, ValueError):
                continue

        memory = h.read("context_summary.md")
        used_tok = estimate(memory)
        limit_tok = capacity(h.model, h.context_window)
        pct = min(100, (100 * used_tok) // max(limit_tok, 1))
        branch, dirty = h.git_info()
        added, deleted = _git_numstat(h.root)

        return {
            "version": __version__,
            "busy": self._busy,
            "workspace": str(h.root),
            "workspace_name": h.root.name,
            "branch": branch,
            "dirty": dirty,
            "added": added,
            "deleted": deleted,
            "model": h.model,
            "mode": h.mode,
            "modes": list(MODES),
            "test_command": h.test_command() or "",
            "context": {
                "used": used_tok,
                "limit": limit_tok,
                "percent": pct,
                "label": "Low" if pct < 40 else ("Medium" if pct < 75 else "High"),
            },
            "active_session_id": h.active_session_id,
            "history": h._load_history(),
            "pinned": h._load_pinned(),
            "edits_count": h.tools.edit_history_count(),
            "workspaces": ws_entries,
            "profiles": masked_profiles_summary(self.env_path),
            "stacks": sorted(STACKS.keys()),
        }

    def start_chat_turn(self, message: str, workspace: str | None = None) -> dict[str, Any]:
        cleaned = (message or "").strip()
        if not cleaned:
            raise ValueError("Message cannot be empty.")
        h = self.get_harness(workspace)
        with self._lock:
            if self._busy:
                raise ValueError("Agent is currently working. Wait or click Stop.")
            self._busy = True
            self._abort_requested = False
            self._active_ws = str(h.root)
            self._turn_seq += 1
            turn_id = self._turn_seq
            self._turn_snapshot = {
                "turn_id": turn_id,
                "done_turn_id": self._done_turn_id,
                "status": "running",
                "text": "",
                "thought": "",
                "steps": [],
                "error": "",
                "pending_approval": None,
            }

        worker = threading.Thread(
            target=self._run_turn_worker,
            args=(h, cleaned, turn_id),
            daemon=True,
        )
        worker.start()
        return {"ok": True, "started": True, "turn_id": turn_id}

    def abort_active_turn(self) -> dict[str, Any]:
        with self._lock:
            self._abort_requested = True
            ev = self._active_abort
            loop = self._active_loop
            task = self._active_task
            self._turn_snapshot["pending_approval"] = None
            for _, (pe, holder) in list(self._pending_approvals.items()):
                holder["decision"] = False
                pe.set()
        if loop is not None and not loop.is_closed():
            if ev is not None:
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(ev.set)
            if task is not None:
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(task.cancel)
        return {"ok": True, "busy": self._busy, "state": self.bootstrap_state()}

    def _run_turn_worker(self, h: AgentHarness, message: str, turn_id: int) -> None:
        started_at = monotonic()
        edits_before = h.tools.edit_history_count()
        history_len_before = len(h._load_history())
        steps: list[dict[str, Any]] = []
        streamed_text_chunks: list[str] = []

        def _emit_and_record(ev: dict[str, Any]) -> None:
            etype = ev.get("type")
            if etype == "text":
                chunk = str(ev.get("text") or "")
                streamed_text_chunks.append(chunk)
                with self._lock:
                    self._turn_snapshot["text"] = str(self._turn_snapshot.get("text") or "") + chunk
            elif etype == "thought":
                chunk = str(ev.get("text") or "")
                with self._lock:
                    self._turn_snapshot["thought"] = (
                        str(self._turn_snapshot.get("thought") or "") + chunk
                    )
            self.emit(ev)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        abort_ev = asyncio.Event()
        with self._lock:
            self._active_loop = loop
            self._active_abort = abort_ev
            if self._abort_requested:
                abort_ev.set()
        h.abort_signal = abort_ev

        self.emit(
            {
                "type": "turn_start",
                "turn_id": turn_id,
                "workspace": str(h.root),
                "message": scrub(message),
                "model": h.model,
                "mode": h.mode,
            }
        )

        # Hook into harness.ask to capture live tool calls & stream tokens
        orig_ask = h.ask

        async def _instrumented_ask(
            role: str,
            prompt: str,
            tools=(),
            *,
            history=None,
            attachments=None,
            show_tools: bool = False,
            stream_display=None,
            fast: bool = False,
        ) -> str:
            proxy = _DesktopStreamProxy(_emit_and_record)
            return await orig_ask(
                role,
                prompt,
                tools,
                history=history,
                attachments=attachments,
                show_tools=True,
                stream_display=proxy,
                fast=fast,
            )

        from . import harness as harness_mod

        orig_render_call = harness_mod.render_tool_call
        orig_render_res = harness_mod.render_tool_result

        def _tap_tool_call(name: str, args: dict | None = None) -> None:
            raw_args = args or {}
            incoming_path = scrub(
                str(raw_args.get("file_path") or raw_args.get("path") or raw_args.get("dir_path") or "")
            ).strip()
            # Coalesce streaming partial updates of the same running tool call into one live card
            if (
                steps
                and steps[-1]["status"] == "running"
                and steps[-1]["name"] == name
                and (
                    not incoming_path
                    or not steps[-1].get("file_path")
                    or steps[-1].get("file_path") == incoming_path
                )
            ):
                entry = _build_step_entry(steps[-1]["id"], name, raw_args)
                steps[-1] = entry
            else:
                entry = _build_step_entry(len(steps) + 1, name, raw_args)
                steps.append(entry)
            with self._lock:
                self._turn_snapshot["steps"] = [dict(s) for s in steps]
            self.emit({"type": "tool_call", "turn_id": turn_id, "step": entry})

        def _tap_tool_res(name: str, preview: str = "") -> None:
            clean_prev = scrub(preview)[:400]
            updated_step: dict[str, Any] | None = None
            for step in reversed(steps):
                if step["name"] == name and step["status"] == "running":
                    step["status"] = (
                        "error" if clean_prev.lower().startswith(("error", "denied", "blocked")) else "done"
                    )
                    step["result_preview"] = clean_prev
                    updated_step = dict(step)
                    break
            with self._lock:
                self._turn_snapshot["steps"] = [dict(s) for s in steps]
            self.emit(
                {
                    "type": "tool_result",
                    "turn_id": turn_id,
                    "name": name,
                    "preview": clean_prev,
                    "step": updated_step,
                }
            )

        outcome_event: dict[str, Any] | None = None
        turn_status = "done"
        turn_error = ""
        saved_reply = ""

        async def _run_execute() -> str:
            if self._abort_requested or abort_ev.is_set():
                raise asyncio.CancelledError
            task = asyncio.create_task(h.execute(message))
            with self._lock:
                self._active_task = task
                already_aborted = self._abort_requested
            if already_aborted or abort_ev.is_set():
                task.cancel()
            return await task

        try:
            h.ask = _instrumented_ask  # type: ignore[method-assign]
            harness_mod.render_tool_call = _tap_tool_call
            harness_mod.render_tool_result = _tap_tool_res
            with console.capture() as captured:
                result = loop.run_until_complete(_run_execute())
            captured_txt = captured.get().strip()
            final_text = (result or "").strip() or captured_txt or "".join(streamed_text_chunks).strip() or "Done."
            saved_reply = scrub(final_text)
            edits_delta = max(0, h.tools.edit_history_count() - edits_before)
            elapsed = round(monotonic() - started_at, 1)
            outcome_event = {
                "type": "turn_done",
                "turn_id": turn_id,
                "text": saved_reply,
                "elapsed_s": elapsed,
                "steps_count": len(steps),
                "steps": steps,
                "edits_delta": edits_delta,
                "model": h.model,
            }
        except asyncio.CancelledError:
            turn_status = "aborted"
            partial = "".join(streamed_text_chunks).strip()
            saved_reply = scrub(f"{partial}\n\n*(Stopped by user)*" if partial else "*(Stopped by user)*")
            outcome_event = {
                "type": "turn_done",
                "turn_id": turn_id,
                "text": saved_reply,
                "elapsed_s": round(monotonic() - started_at, 1),
                "steps_count": len(steps),
                "steps": steps,
                "edits_delta": max(0, h.tools.edit_history_count() - edits_before),
                "model": h.model,
                "aborted": True,
            }
        except Exception as exc:  # noqa: BLE001
            turn_status = "error"
            turn_error = scrub(str(exc)) or type(exc).__name__
            saved_reply = f"**Error:** {turn_error}"
            outcome_event = {
                "type": "turn_error",
                "turn_id": turn_id,
                "error": turn_error,
            }
        finally:
            h.ask = orig_ask  # type: ignore[method-assign]
            harness_mod.render_tool_call = orig_render_call
            harness_mod.render_tool_result = orig_render_res
            h.abort_signal = None
            # Mark any leftover running steps as done and persist compact step cards on the model turn
            elapsed_total = round(monotonic() - started_at, 1)
            for s in steps:
                if s.get("status") == "running":
                    s["status"] = "done" if turn_status == "done" else turn_status
            saved_steps = [
                {
                    k: v
                    for k, v in s.items()
                    if k
                    in {
                        "id",
                        "name",
                        "status",
                        "kind",
                        "file_path",
                        "summary",
                        "content",
                        "truncated",
                        "search_block",
                        "replace_block",
                        "command",
                        "lines_added",
                        "lines_removed",
                        "result_preview",
                    }
                }
                for s in steps[-25:]
            ]
            # Ensure slash commands, stopped partial turns, errors, and step cards remain in session history
            try:
                cur_history = h._load_history()
                if len(cur_history) <= history_len_before and saved_reply:
                    cur_history.append({"role": "user", "text": scrub(message)})
                    cur_history.append(
                        {
                            "role": "model",
                            "text": saved_reply,
                            "steps": saved_steps,
                            "elapsed_s": elapsed_total,
                        }
                    )
                    h._save_history(cur_history)
                elif saved_steps and cur_history and cur_history[-1].get("role") == "model":
                    cur_history[-1]["steps"] = saved_steps
                    cur_history[-1]["elapsed_s"] = elapsed_total
                    h._save_history(cur_history)
            except OSError:
                pass
            with self._lock:
                self._busy = False
                self._abort_requested = False
                self._done_turn_id = turn_id
                self._active_loop = None
                self._active_abort = None
                self._active_task = None
                self._turn_snapshot.update(
                    {
                        "turn_id": turn_id,
                        "done_turn_id": turn_id,
                        "status": turn_status,
                        "text": saved_reply or self._turn_snapshot.get("text", ""),
                        "error": turn_error,
                        "pending_approval": None,
                    }
                )
            loop.close()

        if outcome_event is not None:
            outcome_event["state"] = self.bootstrap_state()
            self.emit(outcome_event)


def dispatch_desktop_request(
    ctrl: DesktopController,
    method: str,
    raw_path: str,
    payload: dict[str, Any],
    *,
    poster=_http_post_json,
) -> tuple[int, str, bytes]:
    """Pure HTTP router for the native Desktop App (unit-testable without sockets or GUI)."""
    parsed = urlparse(raw_path)
    path = parsed.path
    qs = parse_qs(parsed.query)
    ws_param = (qs.get("workspace") or [""])[0] or str(payload.get("workspace") or "")

    # Delegate settings & profile routes directly to config_ui (and reload harness on save)
    if path in {"/api/state", "/api/save", "/api/test", "/api/discover"} or path.startswith("/api/profiles"):
        active_ws_path = Path(ws_param or ctrl._active_ws)

        def _on_cfg_save(_data: Any) -> None:
            for h in list(ctrl._harnesses.values()):
                h.reload_config(ctrl.env_path)
                ctrl._sync_harness_with_global_config(h, apply_mode=True)

        return dispatch_config_request(
            method,
            path,
            payload,
            ctrl.env_path,
            active_ws_path,
            poster=poster,
            on_save=_on_cfg_save,
        )

    if method == "GET":
        if path in {"/", "/index.html"}:
            return HTTPStatus.OK, "text/html; charset=utf-8", load_desktop_html().encode("utf8")
        try:
            if path == "/api/bootstrap":
                return HTTPStatus.OK, "application/json; charset=utf-8", json.dumps(ctrl.bootstrap_state()).encode("utf8")
            if path == "/api/chat/status":
                return HTTPStatus.OK, "application/json; charset=utf-8", json.dumps(ctrl.chat_status()).encode("utf8")
            if path == "/api/files":
                h = ctrl.get_harness(ws_param or None)
                return HTTPStatus.OK, "application/json; charset=utf-8", json.dumps({"files": _iter_tree(h.root)}).encode("utf8")
            if path == "/api/file":
                h = ctrl.get_harness(ws_param or None)
                rel = (qs.get("path") or [""])[0]
                target = h.tools.path(rel)
                text = scrub(target.read_text(encoding="utf8", errors="replace"))[:60000]
                return HTTPStatus.OK, "application/json; charset=utf-8", json.dumps({"path": rel, "content": text}).encode("utf8")
            if path == "/api/diff":
                h = ctrl.get_harness(ws_param or None)
                try:
                    diff_txt = h.git.run("diff", "HEAD")
                    status_txt = h.git.run("status", "--short")
                except ValueError:
                    diff_txt, status_txt = "", ""
                added, deleted = _git_numstat(h.root)
                return HTTPStatus.OK, "application/json; charset=utf-8", json.dumps(
                    {
                        "diff": scrub(diff_txt)[:80000],
                        "status": scrub(status_txt),
                        "added": added,
                        "deleted": deleted,
                    }
                ).encode("utf8")
        except Exception as exc:  # noqa: BLE001
            return HTTPStatus.BAD_REQUEST, "application/json; charset=utf-8", json.dumps({"ok": False, "error": scrub(str(exc))}).encode("utf8")
        return HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", b'{"error":"Not found"}'

    if method == "POST":
        try:
            if path == "/api/workspace/select":
                data = ctrl.select_workspace(str(payload.get("workspace") or ""))
            elif path == "/api/workspace/remove":
                data = ctrl.remove_workspace(str(payload.get("workspace") or ""))
            elif path == "/api/workspace/pick":
                data = ctrl.pick_workspace_dialog()
            elif path == "/api/session/new":
                h = ctrl.get_harness(ws_param or None)
                name = str(payload.get("name") or "").strip() or f"session-{uuid.uuid4().hex[:6]}"
                h._session_command(["new", name])
                data = {"ok": True, "state": ctrl.bootstrap_state()}
            elif path == "/api/session/select":
                h = ctrl.get_harness(ws_param or None)
                h.resume_session(str(payload.get("session_id") or ""))
                data = {"ok": True, "state": ctrl.bootstrap_state()}
            elif path == "/api/session/delete":
                h = ctrl.get_harness(ws_param or None)
                h._session_command(["delete", str(payload.get("session_id") or "")])
                data = {"ok": True, "state": ctrl.bootstrap_state()}
            elif path == "/api/mode":
                h = ctrl.get_harness(ws_param or None)
                h.set_mode(str(payload.get("mode") or "ask"))
                data = {"ok": True, "state": ctrl.bootstrap_state()}
            elif path == "/api/chat/send":
                data = ctrl.start_chat_turn(str(payload.get("message") or ""), ws_param or None)
            elif path == "/api/chat/abort":
                data = ctrl.abort_active_turn()
            elif path == "/api/chat/approve":
                ok = ctrl.resolve_approval(
                    str(payload.get("id") or ""), str(payload.get("decision") or "deny")
                )
                data = {"ok": ok}
            elif path == "/api/checkpoint/undo":
                h = ctrl.get_harness(ws_param or None)
                count = max(1, int(payload.get("count") or 1))
                msg = h.tools.undo_last_edit(count)
                data = {"ok": True, "message": msg, "state": ctrl.bootstrap_state()}
            elif path == "/api/terminal/run":
                h = ctrl.get_harness(ws_param or None)
                cmd = str(payload.get("command") or "").strip()
                if not cmd:
                    raise ValueError("Command cannot be empty.")
                exec_args = ["cmd", "/c", cmd] if sys.platform == "win32" else ["/bin/sh", "-c", cmd]
                proc = subprocess.run(
                    exec_args,
                    cwd=h.root,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=180,
                    check=False,
                )
                data = {
                    "ok": proc.returncode == 0,
                    "exit_code": proc.returncode,
                    "output": scrub((proc.stdout + proc.stderr)[-20000:]),
                }
            elif path == "/api/tools/security":
                h = ctrl.get_harness(ws_param or None)
                data = {"ok": True, "security": security_scan(h, ".")}
            elif path == "/api/tools/sbom":
                h = ctrl.get_harness(ws_param or None)
                fmt = "spdx" if str(payload.get("fmt") or "").lower() == "spdx" else "cyclonedx"
                comps = discover_components(h.root)
                doc = build_spdx(h.root, comps) if fmt == "spdx" else build_cyclonedx(h.root, comps)
                data = {"ok": True, "components": comps[:150], "total": len(comps), "format": doc.get("bomFormat") or fmt}
            elif path == "/api/tools/diagram":
                h = ctrl.get_harness(ws_param or None)
                d = analyze_structure(h.root, ".")
                data = {
                    "ok": True,
                    "files_count": len(d["files"]),
                    "cycles": d["cycles"],
                    "routes": d["routes"][:100],
                    "mermaid": render_diagram_markdown(h.root, ".", d)[:14000],
                }
            elif path == "/api/tools/scaffold":
                h = ctrl.get_harness(ws_param or None)
                name = str(payload.get("name") or "").strip()
                tpl = str(payload.get("template") or "fastapi-service").strip()
                cli_args = [tpl, name]
                if payload.get("minimal"):
                    cli_args.append("--minimal")
                prev_approve = h.approve
                h.approve = lambda _: True
                try:
                    msg = scaffold(h, cli_args)
                finally:
                    h.approve = prev_approve
                data = {"ok": True, "message": msg}
            elif path == "/api/tools/open-external":
                h = ctrl.get_harness(ws_param or None)
                target = str(payload.get("target") or "folder")
                if target == "vscode" and shutil.which("code"):
                    subprocess.Popen(["code", str(h.root)])
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(h.root)])
                elif sys.platform == "win32":
                    os.startfile(str(h.root))  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["xdg-open", str(h.root)])
                data = {"ok": True}
            else:
                return HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", b'{"error":"Not found"}'
            return HTTPStatus.OK, "application/json; charset=utf-8", json.dumps(data).encode("utf8")
        except Exception as exc:  # noqa: BLE001
            return (
                HTTPStatus.BAD_REQUEST,
                "application/json; charset=utf-8",
                json.dumps({"ok": False, "error": scrub(str(exc)) or type(exc).__name__}).encode("utf8"),
            )

    return HTTPStatus.METHOD_NOT_ALLOWED, "application/json; charset=utf-8", b'{"error":"Method not allowed"}'


def create_desktop_server(ctrl: DesktopController, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    class DesktopHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            return

        def _respond(self, status: int, content_type: str, raw: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if urlparse(self.path).path == "/api/events":
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache, no-transform")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                q = ctrl.subscribe()
                try:
                    self.wfile.write(b": connected\n\n")
                    self.wfile.flush()
                    while True:
                        try:
                            ev = q.get(timeout=15)
                            chunk = f"data: {json.dumps(ev)}\n\n".encode()
                        except queue.Empty:
                            chunk = b": keepalive\n\n"
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    ctrl.unsubscribe(q)
                return

            status, ctype, raw = dispatch_desktop_request(ctrl, "GET", self.path, {})
            self._respond(status, ctype, raw)

        def do_POST(self) -> None:
            length = min(int(self.headers.get("Content-Length", "0") or 0), 512_000)
            body = self.rfile.read(length).decode("utf8", errors="replace") if length else "{}"
            try:
                payload = json.loads(body or "{}")
            except json.JSONDecodeError:
                payload = {}
            status, ctype, raw = dispatch_desktop_request(ctrl, "POST", self.path, payload)
            self._respond(status, ctype, raw)

    return ThreadingHTTPServer((host, port), DesktopHandler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="adk-coder-app",
        description="Launch the standalone native ADK Coder Desktop Agent IDE (macOS WKWebView / Windows WebView2).",
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Initial project workspace.")
    parser.add_argument("--env", type=Path, default=None, help="Optional .env file override.")
    parser.add_argument("--width", type=int, default=1340)
    parser.add_argument("--height", type=int, default=860)
    parser.add_argument("--debug", action="store_true", help="Enable WebView DevTools inspector.")
    args = parser.parse_args(argv)

    os.environ["ADK_CODER_DESKTOP"] = "1"
    env_path = (args.env or default_env_path()).expanduser().resolve()
    load_global_env(env_path)
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        print(f"Error: Workspace directory does not exist: {workspace}", file=sys.stderr)
        return 1

    try:
        import webview
    except ImportError:
        print(
            "Error: pywebview is required for the native desktop app.\n"
            "Install with: pip install 'pywebview>=5.3' (or pip install 'adk-coder[desktop]').",
            file=sys.stderr,
        )
        return 1

    ctrl = DesktopController(env_path, workspace)
    server = create_desktop_server(ctrl, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{port}/"
    try:
        window = webview.create_window(
            f"ADK Coder — {workspace.name}",
            url,
            width=args.width,
            height=args.height,
            min_size=(940, 620),
            background_color="#FFFFFF",
        )
        ctrl.attach_window(window)
        webview.start(debug=args.debug)
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
