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
    load_global_env,
)
from .config_ui import (
    dispatch_request as dispatch_config_request,
)
from .diagram import analyze_structure, render_diagram_markdown
from .harness import AgentHarness
from .profiles import masked_profiles_summary
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
        self._active_abort: asyncio.Event | None = None
        self._active_loop: asyncio.AbstractEventLoop | None = None
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
        with self._lock:
            dead: list[queue.Queue[dict[str, Any]]] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=400)
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
        with self._lock:
            self._pending_approvals[req_id] = (ev, holder)
        self.emit({"type": "approval_request", "id": req_id, "message": scrub(message)})
        ev.wait(timeout=300)
        with self._lock:
            self._pending_approvals.pop(req_id, None)
        return holder["decision"]

    def resolve_approval(self, req_id: str, decision: str) -> bool:
        with self._lock:
            entry = self._pending_approvals.get(req_id)
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

    def get_harness(self, workspace: str | None = None) -> AgentHarness:
        key = str(Path(workspace or self._active_ws).expanduser().resolve())
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
            return h

    def select_workspace(self, workspace: str) -> dict[str, Any]:
        h = self.get_harness(workspace)
        self._active_ws = str(h.root)
        return self.bootstrap_state()

    def remove_workspace(self, workspace: str) -> dict[str, Any]:
        target = str(Path(workspace).expanduser().resolve())
        with self._lock:
            self._workspaces = [w for w in self._workspaces if w != target]
            if not self._workspaces:
                self._workspaces = [self._active_ws]
            elif self._active_ws == target:
                self._active_ws = self._workspaces[0]
            self._save_workspaces(self._workspaces)
        return self.bootstrap_state()

    def pick_workspace_dialog(self) -> dict[str, Any]:
        if self._window is None:
            return {"ok": False, "error": "Native folder picker is only available inside the desktop window."}
        try:
            import webview

            dialog_type = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or getattr(
                webview, "FOLDER_DIALOG", 20
            )
            chosen = self._window.create_file_dialog(dialog_type)
            if chosen and len(chosen) > 0:
                return {"ok": True, "state": self.select_workspace(str(chosen[0]))}
            return {"ok": True, "cancelled": True}
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
                        "path": str(p),
                        "name": p.name,
                        "branch": branch,
                        "dirty": dirty,
                        "added": added,
                        "deleted": deleted,
                        "active": str(p) == self._active_ws,
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
        with self._lock:
            if self._busy:
                raise ValueError("Agent is currently working. Wait or click Stop.")
            self._busy = True

        h = self.get_harness(workspace)
        self._active_ws = str(h.root)
        worker = threading.Thread(
            target=self._run_turn_worker,
            args=(h, cleaned),
            daemon=True,
        )
        worker.start()
        return {"ok": True, "started": True}

    def abort_active_turn(self) -> dict[str, Any]:
        with self._lock:
            ev = self._active_abort
            loop = self._active_loop
            for _, (pe, holder) in list(self._pending_approvals.items()):
                holder["decision"] = False
                pe.set()
        if ev is not None and loop is not None:
            loop.call_soon_threadsafe(ev.set)
        return {"ok": True}

    def _run_turn_worker(self, h: AgentHarness, message: str) -> None:
        started_at = monotonic()
        edits_before = h.tools.edit_history_count()
        steps: list[dict[str, Any]] = []
        self.emit(
            {
                "type": "turn_start",
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
            proxy = _DesktopStreamProxy(self.emit)
            # Wrap workspace tools briefly? Better: record step via proxy & model_factory callbacks
            return await orig_ask(
                role,
                prompt,
                tools,
                history=history,
                attachments=attachments,
                show_tools=False,
                stream_display=proxy,
                fast=fast,
            )

        # Also listen to tool execution by wrapping WorkspaceTools methods' approval/records isn't needed;
        # we capture tool calls cleanly via a monkey-safe wrapper on ui.render_tool_call/result during the turn.
        from . import harness as harness_mod

        orig_render_call = harness_mod.render_tool_call
        orig_render_res = harness_mod.render_tool_result

        def _tap_tool_call(name: str, args: dict | None = None) -> None:
            entry = {"name": name, "args": {k: scrub(str(v))[:120] for k, v in (args or {}).items()}}
            steps.append(entry)
            self.emit({"type": "tool_call", **entry})

        def _tap_tool_res(name: str, preview: str = "") -> None:
            self.emit({"type": "tool_result", "name": name, "preview": scrub(preview)[:180]})

        loop = asyncio.new_event_loop()
        abort_ev = asyncio.Event()
        with self._lock:
            self._active_loop = loop
            self._active_abort = abort_ev
        h.abort_signal = abort_ev

        try:
            h.ask = _instrumented_ask  # type: ignore[method-assign]
            harness_mod.render_tool_call = _tap_tool_call
            harness_mod.render_tool_result = _tap_tool_res
            with console.capture() as captured:
                result = loop.run_until_complete(h.execute(message))
            captured_txt = captured.get().strip()
            final_text = (result or "").strip() or captured_txt or "Done."
            edits_delta = max(0, h.tools.edit_history_count() - edits_before)
            elapsed = round(monotonic() - started_at, 1)
            self.emit(
                {
                    "type": "turn_done",
                    "text": scrub(final_text),
                    "elapsed_s": elapsed,
                    "steps_count": len(steps),
                    "steps": steps,
                    "edits_delta": edits_delta,
                    "model": h.model,
                    "state": self.bootstrap_state(),
                }
            )
        except Exception as exc:  # noqa: BLE001
            self.emit(
                {
                    "type": "turn_error",
                    "error": scrub(str(exc)) or type(exc).__name__,
                    "state": self.bootstrap_state(),
                }
            )
        finally:
            h.ask = orig_ask  # type: ignore[method-assign]
            harness_mod.render_tool_call = orig_render_call
            harness_mod.render_tool_result = orig_render_res
            h.abort_signal = None
            with self._lock:
                self._busy = False
                self._active_loop = None
                self._active_abort = None
            loop.close()


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
                h.reload_config()

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
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
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
