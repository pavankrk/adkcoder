"""Native standalone Desktop Agent IDE (`adk-coder --app` / `adk-coder-app`) powered by pywebview.

Uses macOS WKWebView / Windows Edge WebView2 (not a browser tab) backed by an
in-process localhost HTTP+SSE bridge directly attached to `AgentHarness`.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import uuid
from contextlib import suppress
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic, sleep
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
from .extensions import (
    HOOK_EVENTS,
    create_custom_command,
    list_custom_commands,
    list_skills,
)
from .harness import COMMANDS, AgentHarness
from .profiles import apply_profile_to_env, load_profiles, masked_profiles_summary
from .sbom import build_cyclonedx, build_spdx, discover_components
from .scaffolder import STACKS, scaffold
from .scrubber import scrub
from .security import security_scan
from .token_manager import capacity, estimate
from .tools import IGNORED_DIRS, MODES, _is_protected_part, atomic_write
from .ui import StreamDisplay, console
from .watcher import diff_snapshots, snapshot_workspace


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


def _auto_title_from_prompt(prompt: str) -> str:
    cleaned = " ".join((prompt or "").split()).strip()
    if not cleaned:
        return ""
    if len(cleaned) <= 44:
        return cleaned
    return cleaned[:41].rstrip() + "..."


def _parse_git_file_diffs(root: Path, status_txt: str, diff_txt: str) -> list[dict[str, Any]]:
    """Split unified git diff and status into per-file records for interactive review & revert."""
    file_diffs: dict[str, str] = {}
    current_path = ""
    buf: list[str] = []
    for line in diff_txt.splitlines():
        if line.startswith("diff --git "):
            if current_path and buf:
                file_diffs[current_path] = "\n".join(buf)
            parts = line.split(" b/", 1)
            current_path = parts[1].strip() if len(parts) == 2 else ""
            buf = [line]
        elif current_path:
            buf.append(line)
    if current_path and buf:
        file_diffs[current_path] = "\n".join(buf)

    status_map: dict[str, str] = {}
    for raw in status_txt.splitlines():
        if len(raw) < 4:
            continue
        code = raw[:2].strip() or "M"
        rel = raw[3:].strip()
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1].strip()
        rel = rel.strip('"')
        if rel:
            status_map[rel] = code

    all_paths = sorted(set(file_diffs.keys()) | set(status_map.keys()))
    results: list[dict[str, Any]] = []
    for rel in all_paths[:80]:
        fdiff = file_diffs.get(rel, "")
        added = sum(1 for ln in fdiff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        deleted = sum(1 for ln in fdiff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
        st = status_map.get(rel, "M")
        if st == "??" and not fdiff:
            target = root / rel
            if target.is_file():
                try:
                    preview_lines = target.read_text(encoding="utf8", errors="replace").splitlines()[:120]
                    added = len(preview_lines)
                    fdiff = "\n".join(f"+{ln}" for ln in preview_lines)
                except OSError:
                    pass
        results.append(
            {
                "path": rel,
                "status": st,
                "added": added,
                "deleted": deleted,
                "diff": scrub(fdiff)[:20000],
            }
        )
    return results


def _list_checkpoints(h: AgentHarness) -> dict[str, Any]:
    snaps: list[dict[str, Any]] = []
    chk_dir = h.checkpoints.directory
    if chk_dir.is_dir():
        for entry in sorted(chk_dir.iterdir(), reverse=True):
            manifest = entry / "manifest.json"
            if not entry.is_dir() or not manifest.is_file():
                continue
            try:
                meta = json.loads(manifest.read_text(encoding="utf8"))
                snaps.append(
                    {
                        "id": entry.name,
                        "label": str(meta.get("label") or entry.name),
                        "branch": str(meta.get("branch") or ""),
                        "model": str(meta.get("model") or ""),
                        "commit": str(meta.get("commit") or "")[:8],
                    }
                )
            except (OSError, json.JSONDecodeError):
                continue
    return {
        "edits_count": h.tools.edit_history_count(),
        "is_git_repo": h.git.is_repo(),
        "checkpoints": snaps[:30],
    }


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
        self._queue: list[dict[str, str]] = []
        self._turn_snapshot: dict[str, Any] = {
            "turn_id": 0,
            "done_turn_id": 0,
            "status": "idle",
            "text": "",
            "thought": "",
            "steps": [],
            "error": "",
            "pending_approval": None,
            "debug_pipeline": None,
        }
        self._active_abort: asyncio.Event | None = None
        self._active_loop: asyncio.AbstractEventLoop | None = None
        self._active_task: asyncio.Task[Any] | None = None
        self._pending_approvals: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
        self._term_proc: subprocess.Popen[str] | None = None
        self._term_state: dict[str, Any] = {
            "running": False,
            "command": "",
            "workspace": "",
            "output": "",
            "exit_code": None,
        }
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._watch_state: dict[str, Any] = {
            "running": False,
            "command": "",
            "workspace": "",
            "interval": 1.5,
            "last_status": "idle",
            "last_output": "",
            "changed_files": [],
            "run_count": 0,
        }
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
                "debug_pipeline": self._turn_snapshot.get("debug_pipeline"),
                "queued_messages": list(self._queue),
            }

    def queue_message(self, message: str, workspace: str | None = None) -> dict[str, Any]:
        cleaned = (message or "").strip()
        if not cleaned:
            raise ValueError("Queued message cannot be empty.")
        with self._lock:
            if not self._busy:
                should_start_now = True
            else:
                should_start_now = False
                self._queue.append(
                    {
                        "id": uuid.uuid4().hex[:8],
                        "message": cleaned,
                        "workspace": workspace or self._active_ws,
                    }
                )
        if should_start_now:
            return self.start_chat_turn(cleaned, workspace)
        return {"ok": True, "queued": True, "queued_messages": list(self._queue)}

    def remove_queued_message(self, index: int) -> dict[str, Any]:
        with self._lock:
            if 0 <= index < len(self._queue):
                self._queue.pop(index)
            q_copy = list(self._queue)
        return {"ok": True, "queued_messages": q_copy}

    def clear_queue(self) -> dict[str, Any]:
        with self._lock:
            self._queue.clear()
        return {"ok": True, "queued_messages": []}

    def upload_image(self, data_url: str, filename: str = "paste.png", workspace: str | None = None) -> dict[str, Any]:
        h = self.get_harness(workspace)
        raw_url = (data_url or "").strip()
        match = re.match(r"^data:image/(png|jpeg|jpg|webp|gif);base64,(.+)$", raw_url, re.IGNORECASE | re.DOTALL)
        if not match:
            raise ValueError("Unsupported image format. Paste PNG, JPEG, WebP, or GIF.")
        ext = match.group(1).lower().replace("jpeg", "jpg")
        b64 = match.group(2).strip()
        try:
            raw_bytes = base64.b64decode(b64, validate=True)
        except Exception as exc:
            raise ValueError("Invalid base64 image payload.") from exc
        if len(raw_bytes) > 6 * 1024 * 1024:
            raise ValueError("Image exceeds 6 MB limit.")
        uploads_dir = h.state / "uploads"
        uploads_dir.mkdir(mode=0o700, exist_ok=True)
        safe_stem = re.sub(r"[^A-Za-z0-9_-]", "_", Path(filename or "paste").stem)[:24] or "paste"
        dest = uploads_dir / f"{safe_stem}_{uuid.uuid4().hex[:6]}.{ext}"
        dest.write_bytes(raw_bytes)
        rel = dest.relative_to(h.root).as_posix()
        return {"ok": True, "path": rel, "bytes": len(raw_bytes)}

    # --- Streaming Embedded Terminal ---
    def terminal_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._term_state)

    def stop_terminal(self) -> dict[str, Any]:
        with self._lock:
            proc = self._term_proc
        if proc is not None and proc.poll() is None:
            with suppress(OSError):
                proc.terminate()
        return {"ok": True, "terminal": self.terminal_status()}

    def start_terminal(self, command: str, workspace: str | None = None) -> dict[str, Any]:
        cmd = (command or "").strip()
        if not cmd:
            raise ValueError("Command cannot be empty.")
        h = self.get_harness(workspace)
        self.stop_terminal()
        exec_args = ["cmd", "/c", cmd] if sys.platform == "win32" else ["/bin/sh", "-c", cmd]
        proc = subprocess.Popen(
            exec_args,
            cwd=h.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )
        with self._lock:
            self._term_proc = proc
            self._term_state = {
                "running": True,
                "command": cmd,
                "workspace": str(h.root),
                "output": "",
                "exit_code": None,
            }

        def _reader() -> None:
            chunks: list[str] = []
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    clean = scrub(line)
                    chunks.append(clean)
                    joined = "".join(chunks)[-32000:]
                    with self._lock:
                        self._term_state["output"] = joined
                    self.emit({"type": "terminal_chunk", "chunk": clean})
            except OSError:
                pass
            finally:
                rc = proc.wait()
                with self._lock:
                    self._term_state["running"] = False
                    self._term_state["exit_code"] = rc
                    if self._term_proc is proc:
                        self._term_proc = None
                self.emit({"type": "terminal_done", "exit_code": rc})

        threading.Thread(target=_reader, daemon=True).start()
        return {"ok": True, "terminal": self.terminal_status()}

    # --- Real-Time Watch Mode ---
    def watch_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._watch_state)

    def stop_watch(self) -> dict[str, Any]:
        self._watch_stop.set()
        with self._lock:
            self._watch_state["running"] = False
        self.emit({"type": "watch_update", "watch": self.watch_status()})
        return {"ok": True, "watch": self.watch_status()}

    def start_watch(
        self, command: str | None = None, workspace: str | None = None, interval: float = 1.5
    ) -> dict[str, Any]:
        h = self.get_harness(workspace)
        cmd = (command or "").strip() or (h.test_command() or "").strip()
        if not cmd:
            raise ValueError("Set a test command first (e.g. pytest -q or mvn -B test).")
        self.stop_watch()
        self._watch_stop = threading.Event()
        iv = max(0.4, min(float(interval or 1.5), 30.0))
        with self._lock:
            self._watch_state = {
                "running": True,
                "command": cmd,
                "workspace": str(h.root),
                "interval": iv,
                "last_status": "watching",
                "last_output": "",
                "changed_files": [],
                "run_count": 0,
            }
        self.emit({"type": "watch_update", "watch": self.watch_status()})

        def _watch_loop() -> None:
            snap = snapshot_workspace(h.root)
            exec_args = ["cmd", "/c", cmd] if sys.platform == "win32" else ["/bin/sh", "-c", cmd]
            while not self._watch_stop.is_set():
                sleep(iv)
                if self._watch_stop.is_set():
                    break
                latest = snapshot_workspace(h.root)
                changed = diff_snapshots(snap, latest)
                if not changed:
                    continue
                with self._lock:
                    self._watch_state["last_status"] = "running"
                    self._watch_state["changed_files"] = changed[:10]
                self.emit({"type": "watch_update", "watch": self.watch_status()})
                try:
                    proc = subprocess.run(
                        exec_args,
                        cwd=h.root,
                        capture_output=True,
                        text=True,
                        errors="replace",
                        timeout=180,
                        check=False,
                    )
                    passed = proc.returncode == 0
                    out = scrub((proc.stdout + proc.stderr)[-12000:])
                except (OSError, subprocess.TimeoutExpired) as exc:
                    passed = False
                    out = scrub(str(exc))
                with self._lock:
                    self._watch_state["last_status"] = "pass" if passed else "fail"
                    self._watch_state["last_output"] = out
                    self._watch_state["run_count"] = int(self._watch_state.get("run_count", 0)) + 1
                self.emit({"type": "watch_update", "watch": self.watch_status()})
                snap = snapshot_workspace(h.root)

        self._watch_thread = threading.Thread(target=_watch_loop, daemon=True)
        self._watch_thread.start()
        return {"ok": True, "watch": self.watch_status()}

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

        custom_cmds = sorted(list_custom_commands(h.state).keys())
        all_commands = [
            {"name": f"/{name}", "args": usage, "custom": False}
            for name, usage in sorted(COMMANDS.items())
        ] + [{"name": f"/{c}", "args": "[arguments]", "custom": True} for c in custom_cmds]

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
                "prompt_tokens": h.tracker.prompt_tokens,
                "output_tokens": h.tracker.output_tokens,
                "total_tokens": h.tracker.total_tokens,
                "calls": h.tracker.turns,
            },
            "active_session_id": h.active_session_id,
            "history": h._load_history(),
            "pinned": h._load_pinned(),
            "edits_count": h.tools.edit_history_count(),
            "workspaces": ws_entries,
            "profiles": masked_profiles_summary(self.env_path),
            "stacks": sorted(STACKS.keys()),
            "commands": all_commands,
            "queued_messages": list(self._queue),
            "watch": self.watch_status(),
            "debug_report_exists": (h.root / "docs" / "DEBUG_REPORT.md").is_file(),
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
            is_debug_cmd = cleaned.lower().startswith("/debug")
            self._turn_snapshot = {
                "turn_id": turn_id,
                "done_turn_id": self._done_turn_id,
                "status": "running",
                "text": "",
                "thought": "",
                "steps": [],
                "error": "",
                "pending_approval": None,
                "debug_pipeline": (
                    {
                        "active": True,
                        "stage": 0,
                        "stages": ["Log Analyst", "Root Cause", "Fix Engineer", "Verifier"],
                        "report_exists": (h.root / "docs" / "DEBUG_REPORT.md").is_file(),
                    }
                    if is_debug_cmd
                    else None
                ),
            }

        # Auto-title default / auto-named sessions from the first user prompt
        try:
            cur_sess = h.sessions.get(h.active_session_id)
            if cur_sess and (
                cur_sess["name"] == "default" or cur_sess["name"].startswith("session-")
            ):
                auto_name = _auto_title_from_prompt(cleaned)
                if auto_name:
                    h.sessions.rename(cur_sess["id"], auto_name)
        except (OSError, ValueError):
            pass

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
                dp = self._turn_snapshot.get("debug_pipeline")
                if isinstance(dp, dict):
                    if name in {"read_file", "search_files", "find_files", "list_dir"}:
                        dp["stage"] = max(int(dp.get("stage", 0)), 1)
                    elif name in {"write_file", "apply_diff", "apply_multi_diff"}:
                        dp["stage"] = max(int(dp.get("stage", 0)), 2)
                    elif name == "run_shell_command" and int(dp.get("stage", 0)) >= 2:
                        dp["stage"] = 3
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
                dp = self._turn_snapshot.get("debug_pipeline")
                if isinstance(dp, dict):
                    dp["active"] = False
                    if turn_status == "done":
                        dp["stage"] = 4
                    dp["report_exists"] = (h.root / "docs" / "DEBUG_REPORT.md").is_file()
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
                next_queued = self._queue.pop(0) if (turn_status == "done" and self._queue) else None
            loop.close()

        if outcome_event is not None:
            outcome_event["state"] = self.bootstrap_state()
            self.emit(outcome_event)

        if next_queued is not None:
            with suppress(Exception):
                self.start_chat_turn(next_queued["message"], next_queued.get("workspace"))


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
            if path == "/api/sessions/search":
                h = ctrl.get_harness(ws_param or None)
                q = (qs.get("q") or [""])[0]
                hits = [
                    {
                        "id": s["id"],
                        "name": s["name"],
                        "model": s.get("model", ""),
                        "updated_at": s.get("updated_at", ""),
                    }
                    for s in h.sessions.search(q)[:25]
                ]
                return HTTPStatus.OK, "application/json; charset=utf-8", json.dumps({"sessions": hits}).encode("utf8")
            if path == "/api/terminal/status":
                return HTTPStatus.OK, "application/json; charset=utf-8", json.dumps(ctrl.terminal_status()).encode("utf8")
            if path == "/api/watch/status":
                return HTTPStatus.OK, "application/json; charset=utf-8", json.dumps(ctrl.watch_status()).encode("utf8")
            if path == "/api/checkpoints":
                h = ctrl.get_harness(ws_param or None)
                return HTTPStatus.OK, "application/json; charset=utf-8", json.dumps(_list_checkpoints(h)).encode("utf8")
            if path == "/api/extensions":
                h = ctrl.get_harness(ws_param or None)
                skills = [
                    {"name": v["name"], "description": v["description"], "path": v["path"]}
                    for v in list_skills(h.state).values()
                ]
                cmds = [
                    {"name": k, "path": str(p), "body": scrub(p.read_text(encoding="utf8", errors="replace"))[:2000]}
                    for k, p in list_custom_commands(h.state).items()
                ]
                return HTTPStatus.OK, "application/json; charset=utf-8", json.dumps(
                    {
                        "skills": skills,
                        "commands": cmds,
                        "hooks": h.hooks.load(),
                        "hook_events": list(HOOK_EVENTS),
                        "mcp_servers": h.mcp.load(),
                    }
                ).encode("utf8")
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
                        "files": _parse_git_file_diffs(h.root, status_txt, diff_txt),
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
            elif path == "/api/session/rename":
                h = ctrl.get_harness(ws_param or None)
                sid = str(payload.get("session_id") or h.active_session_id)
                new_name = str(payload.get("name") or "").strip()
                h.sessions.rename(sid, new_name)
                data = {"ok": True, "state": ctrl.bootstrap_state()}
            elif path == "/api/session/fork":
                h = ctrl.get_harness(ws_param or None)
                sid = str(payload.get("session_id") or h.active_session_id)
                new_name = str(payload.get("name") or "").strip()
                forked = h.sessions.fork(sid, new_name)
                h.resume_session(forked["id"])
                data = {"ok": True, "session": forked, "state": ctrl.bootstrap_state()}
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
            elif path == "/api/chat/queue":
                data = ctrl.queue_message(str(payload.get("message") or ""), ws_param or None)
            elif path == "/api/chat/queue/remove":
                data = ctrl.remove_queued_message(int(payload.get("index", -1)))
            elif path == "/api/chat/queue/clear":
                data = ctrl.clear_queue()
            elif path == "/api/chat/upload-image":
                data = ctrl.upload_image(
                    str(payload.get("data_url") or ""),
                    str(payload.get("filename") or "paste.png"),
                    ws_param or None,
                )
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
            elif path == "/api/checkpoints/create":
                h = ctrl.get_harness(ws_param or None)
                label = str(payload.get("label") or "").strip() or "desktop-checkpoint"
                cid = h.checkpoints.create(label, h.model, h.tracker.total_tokens)
                data = {"ok": True, "id": cid, "checkpoints": _list_checkpoints(h)}
            elif path == "/api/checkpoints/restore":
                h = ctrl.get_harness(ws_param or None)
                cid = str(payload.get("id") or "").strip()
                msg = h.checkpoints.restore(cid, lambda _: True)
                data = {"ok": True, "message": msg, "state": ctrl.bootstrap_state()}
            elif path == "/api/checkpoints/delete":
                h = ctrl.get_harness(ws_param or None)
                cid = str(payload.get("id") or "").strip()
                msg = h.checkpoints.delete(cid, lambda _: True)
                data = {"ok": True, "message": msg, "checkpoints": _list_checkpoints(h)}
            elif path == "/api/git/revert-file":
                h = ctrl.get_harness(ws_param or None)
                rel = str(payload.get("path") or "").strip()
                if not rel:
                    raise ValueError("File path is required.")
                target = h.tools.path(rel)
                try:
                    h.git.run("checkout", "--", rel)
                except ValueError:
                    if target.is_file():
                        target.unlink()
                    else:
                        raise
                data = {"ok": True, "state": ctrl.bootstrap_state()}
            elif path == "/api/git/commit":
                h = ctrl.get_harness(ws_param or None)
                msg = str(payload.get("message") or "").strip()
                if not msg:
                    data = ctrl.start_chat_turn("/commit", ws_param or None)
                else:
                    h.git.run("add", "-A", "--", ".", ":(exclude).agent")
                    out = h.git.run("commit", "-m", msg)
                    data = {"ok": True, "output": scrub(out), "state": ctrl.bootstrap_state()}
            elif path == "/api/context/pin":
                h = ctrl.get_harness(ws_param or None)
                rel = str(payload.get("path") or "").strip()
                target = h.tools.path(rel)
                if not target.is_file():
                    raise ValueError(f"File not found: {rel}")
                norm = target.relative_to(h.root).as_posix()
                pinned = h._load_pinned()
                if norm not in pinned:
                    pinned.append(norm)
                    h._save_pinned(pinned)
                data = {"ok": True, "state": ctrl.bootstrap_state()}
            elif path == "/api/context/unpin":
                h = ctrl.get_harness(ws_param or None)
                rel = str(payload.get("path") or "").strip()
                pinned = [p for p in h._load_pinned() if p != rel] if rel != "all" else []
                h._save_pinned(pinned)
                data = {"ok": True, "state": ctrl.bootstrap_state()}
            elif path == "/api/context/compact":
                data = ctrl.start_chat_turn("/compact", ws_param or None)
            elif path == "/api/extensions/command":
                h = ctrl.get_harness(ws_param or None)
                dest = create_custom_command(
                    h.state,
                    str(payload.get("name") or ""),
                    str(payload.get("body") or ""),
                )
                data = {"ok": True, "path": str(dest), "state": ctrl.bootstrap_state()}
            elif path == "/api/extensions/hooks":
                h = ctrl.get_harness(ws_param or None)
                raw_hooks = payload.get("hooks") if isinstance(payload.get("hooks"), dict) else {}
                clean_hooks = {
                    ev: [str(c).strip() for c in (raw_hooks.get(ev) or []) if str(c).strip()]
                    for ev in HOOK_EVENTS
                }
                atomic_write(h.hooks.config_path, json.dumps(clean_hooks, indent=2) + "\n", mode=0o600)
                data = {"ok": True, "hooks": clean_hooks}
            elif path == "/api/extensions/mcp/add":
                h = ctrl.get_harness(ws_param or None)
                raw_args = payload.get("args")
                args_list = (
                    [str(a) for a in raw_args if str(a).strip()]
                    if isinstance(raw_args, list)
                    else str(raw_args or "").split()
                )
                entry = h.mcp.add(str(payload.get("name") or ""), str(payload.get("command") or ""), args_list)
                data = {"ok": True, "server": entry}
            elif path == "/api/extensions/mcp/remove":
                h = ctrl.get_harness(ws_param or None)
                removed = h.mcp.remove(str(payload.get("name") or ""))
                data = {"ok": removed}
            elif path == "/api/terminal/start":
                data = ctrl.start_terminal(str(payload.get("command") or ""), ws_param or None)
            elif path == "/api/terminal/stop":
                data = ctrl.stop_terminal()
            elif path == "/api/watch/start":
                data = ctrl.start_watch(
                    str(payload.get("command") or ""),
                    ws_param or None,
                    float(payload.get("interval") or 1.5),
                )
            elif path == "/api/watch/stop":
                data = ctrl.stop_watch()
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
                parent_dir = str(payload.get("parent_dir") or "").strip()
                reqs_text = str(payload.get("requirements") or "").strip()

                target_h = h
                cli_args = [tpl, name]
                if parent_dir:
                    p_expanded = Path(parent_dir).expanduser()
                    if p_expanded.is_absolute():
                        p_resolved = p_expanded.resolve()
                        if not p_resolved.is_dir():
                            raise ValueError(f"Parent directory not found: {p_resolved}")
                        target_h = AgentHarness(p_resolved, approve=lambda _: True)
                    else:
                        cli_args.append(parent_dir)
                if payload.get("minimal"):
                    cli_args.append("--minimal")
                overrides = (
                    {"requirements_md": f"# Requirements — {name}\n\n{scrub(reqs_text)}\n"}
                    if reqs_text
                    else None
                )
                prev_approve = target_h.approve
                target_h.approve = lambda _: True
                try:
                    msg = scaffold(target_h, cli_args, overrides=overrides)
                finally:
                    target_h.approve = prev_approve
                created_dir = (
                    (target_h.root / name)
                    if (not parent_dir or Path(parent_dir).expanduser().is_absolute())
                    else (target_h.root / parent_dir / name)
                ).resolve()
                new_state = None
                if payload.get("open_after") and created_dir.is_dir():
                    new_state = ctrl.select_workspace(str(created_dir))
                data = {
                    "ok": True,
                    "message": msg,
                    "created_path": str(created_dir) if created_dir.is_dir() else "",
                    "state": new_state,
                }
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
            length = min(int(self.headers.get("Content-Length", "0") or 0), 10_000_000)
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
        ctrl.stop_watch()
        ctrl.stop_terminal()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
