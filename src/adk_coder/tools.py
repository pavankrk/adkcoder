"""Workspace-scoped coding tools with path guards, diff previews, and session approvals."""

from __future__ import annotations

import base64
import fnmatch
import html
import ipaddress
import json
import os
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from contextlib import suppress
from pathlib import Path

from .changelog import record
from .diff_viewer import preview
from .notifier import notify
from .result_formatter import format_result
from .scrubber import scrub
from .spinner import spinning
from .ui import pause_live

PROTECTED_PARTS = {
    ".git",
    ".agent",
    ".ssh",
    ".aws",
    ".kube",
    ".docker",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "credentials.json",
}
PROTECTED_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
PROTECTED_PREFIXES = (".env", "id_rsa", "id_ed25519")
IGNORED_DIRS = {
    ".git",
    ".agent",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "target",
    "vendor",
}
SHELL_META = re.compile(r"[|&;><*?$`~\n]|^\s*[A-Za-z_][A-Za-z0-9_]*=")
MAX_READ_LINES = 500
MAX_READ_BYTES = 64_000


def atomic_write(path: Path, content: str, *, mode: int | None = None) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        try:
            temp.write(content.encode("utf8"))
            temp.close()
            if mode is not None:
                os.chmod(temp.name, mode)
            elif path.exists():
                os.chmod(temp.name, path.stat().st_mode & 0o777)
            os.replace(temp.name, path)
        finally:
            Path(temp.name).unlink(missing_ok=True)


def _is_protected_part(part: str) -> bool:
    lowered = part.lower()
    if lowered in PROTECTED_PARTS:
        return True
    if lowered.startswith(PROTECTED_PREFIXES):
        return True
    if lowered.endswith(PROTECTED_SUFFIXES):
        return True
    return "service-account" in lowered and lowered.endswith(".json")


MODES = ("ask", "auto-edit", "plan", "full-auto")


class WorkspaceTools:
    def __init__(
        self,
        root,
        approve,
        notifications: bool = False,
        shell_mode: bool = True,
        mode: str = "ask",
        sandbox_shell: bool = False,
        hooks=None,
    ):
        self.root = Path(root)
        self._approve = approve
        self.notifications = notifications
        self.shell_mode = shell_mode
        self.mode = mode if mode in MODES else "ask"
        self.sandbox_shell = sandbox_shell
        self.hooks = hooks
        self.always_scopes: set[str] = set()
        self._edit_history_path = self.root / ".agent" / "edit_history.json"
        self._active_proc: subprocess.Popen[str] | None = None
        self._abort_flag = False

    def abort_running(self) -> None:
        """Immediately terminate any active shell subprocess spawned by this workspace."""
        self._abort_flag = True
        proc = self._active_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                with suppress(OSError):
                    proc.kill()

    def reset_abort(self) -> None:
        self._abort_flag = False

    @property
    def approve(self):
        return self._approve

    @approve.setter
    def approve(self, fn):
        self._approve = fn
        self.always_scopes.clear()

    def _confirm(self, message: str, scope: str | None = None) -> bool:
        if self.mode == "plan":
            return False
        if self.mode == "full-auto":
            return True
        if self.mode == "auto-edit" and scope == "edit":
            return True
        if scope and scope in self.always_scopes:
            return True
        with pause_live():
            decision = self._approve(message)
        if isinstance(decision, str):
            lowered = decision.strip().lower()
            if lowered in {"a", "always"}:
                if scope:
                    self.always_scopes.add(scope)
                return True
            return lowered in {"y", "yes"}
        return bool(decision)

    def _record_undo(self, path: Path, existed: bool, raw_before: bytes) -> None:
        """Persist a bounded pre-edit snapshot so /undo-edit works even outside Git."""
        try:
            history = (
                json.loads(self._edit_history_path.read_text(encoding="utf8"))
                if self._edit_history_path.is_file()
                else []
            )
            if not isinstance(history, list):
                history = []
        except (OSError, json.JSONDecodeError):
            history = []
        history.append(
            {
                "file": path.relative_to(self.root).as_posix(),
                "existed": existed,
                "content_b64": base64.b64encode(raw_before).decode("ascii"),
            }
        )
        # Keep the last 30 edit snapshots (skip oversized >256KB files).
        if len(raw_before) <= 256_000:
            self._edit_history_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(
                self._edit_history_path,
                json.dumps(history[-30:], indent=2),
                mode=0o600,
            )

    def edit_history_count(self) -> int:
        """Return number of recorded agent edits currently in the rollback stack."""
        if not self._edit_history_path.is_file():
            return 0
        try:
            history = json.loads(self._edit_history_path.read_text(encoding="utf8"))
            return len(history) if isinstance(history, list) else 0
        except (OSError, json.JSONDecodeError):
            return 0

    def undo_last_edit(self, count: int = 1) -> str:
        """Revert the last N agent file edits/creations/deletions."""
        if not self._edit_history_path.is_file():
            return "No recent agent edits to undo."
        try:
            history = json.loads(self._edit_history_path.read_text(encoding="utf8"))
        except (OSError, json.JSONDecodeError):
            return "Edit history unreadable."
        if not history:
            return "No recent agent edits to undo."
        steps = min(max(int(count), 1), len(history))
        reverted: list[str] = []
        for _ in range(steps):
            entry = history.pop()
            rel = entry["file"]
            target = self.path(rel)
            if entry["existed"]:
                raw = base64.b64decode(entry["content_b64"].encode("ascii"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
                reverted.append(f"restored {rel}")
            else:
                target.unlink(missing_ok=True)
                reverted.append(f"removed {rel}")
        atomic_write(
            self._edit_history_path, json.dumps(history, indent=2), mode=0o600
        )
        record(self.root / ".agent", "UNDO_EDIT", ", ".join(reverted))
        return "Undid: " + ", ".join(reverted)

    def path(self, name: str | Path, *, allow_root: bool = False) -> Path:
        path = (self.root / name).resolve()
        parts = path.relative_to(self.root).parts
        if not parts:
            if allow_root:
                return path
            raise ValueError("Protected workspace path")
        if any(_is_protected_part(p) for p in parts):
            raise ValueError("Protected workspace path")
        return path

    def list_dir(self, dir_path: str = ".", max_depth: int = 2) -> str:
        """List files and subdirectories inside a workspace directory."""
        target = self.path(dir_path, allow_root=True)
        if not target.exists():
            return f"Directory not found: {dir_path}"
        if not target.is_dir():
            return f"Not a directory: {dir_path}"
        depth_limit = min(max(int(max_depth), 1), 5)
        lines: list[str] = []

        def walk(current: Path, depth: int, prefix: str) -> None:
            if len(lines) >= 250:
                return
            try:
                entries = sorted(
                    current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
                )
            except OSError:
                return
            for entry in entries:
                if len(lines) >= 250:
                    return
                if entry.name in IGNORED_DIRS or _is_protected_part(entry.name):
                    continue
                suffix = "/" if entry.is_dir() else ""
                lines.append(f"{prefix}{entry.name}{suffix}")
                if entry.is_dir() and depth < depth_limit and not entry.is_symlink():
                    walk(entry, depth + 1, prefix + "  ")

        walk(target, 1, "")
        if not lines:
            return "(empty directory)"
        if len(lines) >= 250:
            lines.append("... (truncated at 250 entries)")
        return "\n".join(lines)

    def find_files(self, pattern: str, dir_path: str = ".") -> str:
        """Find workspace files matching a glob pattern (e.g. '*.py' or '*test*')."""
        target = self.path(dir_path, allow_root=True)
        if not target.is_dir():
            return f"Directory not found: {dir_path}"
        matches: list[str] = []
        for parent, dirs, files in os.walk(target):
            dirs[:] = sorted(
                d for d in dirs if d not in IGNORED_DIRS and not _is_protected_part(d)
            )
            for name in sorted(files):
                if _is_protected_part(name):
                    continue
                full = Path(parent, name)
                rel = full.relative_to(self.root).as_posix()
                if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern):
                    matches.append(rel)
                    if len(matches) >= 200:
                        matches.append("... (truncated at 200 files)")
                        return "\n".join(matches)
        return "\n".join(matches) or "No matching files."

    def search_files(
        self, query: str, dir_path: str = ".", use_regex: bool = False
    ) -> str:
        """Search workspace source files for a literal string or regular expression."""
        if not query:
            return "Empty search query."
        target = self.path(dir_path, allow_root=True)
        if not target.exists():
            return f"Search path not found: {dir_path}"

        rg = shutil.which("rg")
        if rg:
            cmd = [
                rg,
                "--line-number",
                "--no-heading",
                "--color=never",
                "--max-count=20",
                "--glob=!.git/*",
                "--glob=!.agent/*",
                "--glob=!.env*",
                "--glob=!node_modules/*",
                "--glob=!.venv/*",
            ]
            if not use_regex:
                cmd.append("--fixed-strings")
            cmd.extend(["--", query, str(target)])
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=30,
                    check=False,
                )
                lines = proc.stdout.splitlines()
                if not lines:
                    return "No matches found."
                rel_lines = []
                root_prefix = str(self.root) + os.sep
                for line in lines[:100]:
                    line = line.removeprefix(root_prefix)
                    rel_lines.append(scrub(line))
                if len(lines) > 100:
                    rel_lines.append(f"... ({len(lines) - 100} more matches truncated)")
                return "\n".join(rel_lines)
            except (OSError, subprocess.TimeoutExpired):
                pass

        try:
            matcher = re.compile(query if use_regex else re.escape(query))
        except re.error as exc:
            return f"Invalid regex: {exc}"

        hits: list[str] = []
        scan_files = [target] if target.is_file() else []
        if target.is_dir():
            for parent, dirs, files in os.walk(target):
                dirs[:] = sorted(
                    d for d in dirs if d not in IGNORED_DIRS and not _is_protected_part(d)
                )
                for name in sorted(files):
                    if not _is_protected_part(name):
                        scan_files.append(Path(parent, name))

        for path in scan_files:
            try:
                if path.is_symlink() or path.stat().st_size > 512_000:
                    continue
                raw = path.read_bytes()
                if b"\x00" in raw[:4096]:
                    continue
                text = raw.decode("utf8", errors="replace")
            except OSError:
                continue
            rel = path.relative_to(self.root).as_posix()
            for lineno, line in enumerate(text.splitlines(), 1):
                if matcher.search(line):
                    hits.append(f"{rel}:{lineno}:{scrub(line)}")
                    if len(hits) >= 100:
                        hits.append("... (truncated at 100 matches)")
                        return "\n".join(hits)
        return "\n".join(hits) or "No matches found."

    def read_file(
        self, file_path: str, start_line: int = 1, end_line: int = 0
    ) -> str:
        """Read a UTF-8 workspace file (with optional 1-based line range) and mask credentials."""
        path = self.path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")
        raw = path.read_bytes()
        if b"\x00" in raw[:8192]:
            return f"Binary file skipped ({len(raw)} bytes): {file_path}"
        try:
            text = raw.decode("utf8")
        except UnicodeDecodeError:
            return f"Non-UTF-8 file skipped ({len(raw)} bytes): {file_path}"

        lines = text.splitlines()
        total = len(lines)
        start = max(1, int(start_line))
        stop = min(total, int(end_line)) if end_line and end_line > 0 else total
        if start > total > 0:
            return f"Start line {start} is past end of file ({total} lines)."

        selected = lines[start - 1 : stop]
        truncated = False
        if len(selected) > MAX_READ_LINES:
            selected = selected[:MAX_READ_LINES]
            truncated = True

        out_lines = []
        byte_budget = 0
        last_line = start - 1
        for offset, line in enumerate(selected):
            lineno = start + offset
            formatted = f"{lineno:>4} | {scrub(line)}"
            byte_budget += len(formatted.encode("utf8")) + 1
            if byte_budget > MAX_READ_BYTES and out_lines:
                truncated = True
                break
            out_lines.append(formatted)
            last_line = lineno

        body = "\n".join(out_lines)
        if truncated or last_line < total:
            body += (
                f"\n... (showing lines {start}-{last_line} of {total}; "
                f"call read_file({file_path!r}, start_line={last_line + 1}) for more)"
            )
        return body

    def apply_diff(
        self,
        file_path: str,
        search_block: str,
        replace_block: str,
        start_line: int = 0,
    ) -> str:
        """Replace one exact block (or create a new file when search_block is empty)."""
        path = self.path(file_path)
        exists = path.exists()
        try:
            old = path.read_bytes().decode("utf8") if exists else ""
        except UnicodeDecodeError:
            return "Cannot edit non-UTF-8 file."

        if exists:
            if not search_block:
                return "Expected exactly one matching block."
            count = old.count(search_block)
            if count == 0:
                return "Expected exactly one matching block."
            if count == 1:
                new = old.replace(search_block, replace_block, 1)
            else:
                positions = []
                idx = old.find(search_block)
                while idx != -1:
                    lineno = old[:idx].count("\n") + 1
                    positions.append((idx, lineno))
                    idx = old.find(search_block, idx + 1)
                if start_line > 0:
                    chosen_idx, _ = min(
                        positions, key=lambda pair: abs(pair[1] - start_line)
                    )
                    new = (
                        old[:chosen_idx]
                        + replace_block
                        + old[chosen_idx + len(search_block) :]
                    )
                else:
                    found_lines = ", ".join(str(ln) for _, ln in positions)
                    return (
                        f"Expected exactly one matching block "
                        f"(found {count} matches near lines {found_lines}; "
                        f"include more context or pass start_line)."
                    )
        else:
            if search_block:
                return "New files require an empty search block."
            new = replace_block

        preview(file_path, scrub(old), scrub(new))
        if not self._confirm(f"Write {file_path}?", scope="edit"):
            return "Edit declined."
        if path.exists() != exists or (
            exists and path.read_bytes().decode("utf8") != old
        ):
            return "File changed during review."
        if self.hooks is not None:
            ok, hook_msg = self.hooks.run("pre_edit", file_path=file_path)
            if not ok:
                return f"Blocked by pre_edit hook: {hook_msg}"
        self._record_undo(path, exists, old.encode("utf8") if exists else b"")
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, new)
        record(self.root / ".agent", "EDIT", file_path)
        if self.hooks is not None:
            self.hooks.run("post_edit", file_path=file_path)
        return "Saved."

    def apply_multi_diff(self, file_path: str, edits: list[dict[str, str]]) -> str:
        """Apply multiple exact search/replace hunks atomically to one existing file."""
        if not edits or not isinstance(edits, list):
            return "Expected a non-empty list of {search_block, replace_block} edits."
        path = self.path(file_path)
        if not path.is_file():
            return f"File not found: {file_path}"
        try:
            old_bytes = path.read_bytes()
            old = old_bytes.decode("utf8")
        except UnicodeDecodeError:
            return "Cannot edit non-UTF-8 file."

        new = old
        for idx, hunk in enumerate(edits, 1):
            if not isinstance(hunk, dict):
                return f"Edit #{idx} must be an object with search_block and replace_block."
            search = hunk.get("search_block", "")
            replace = hunk.get("replace_block", "")
            if not isinstance(search, str) or not isinstance(replace, str) or not search:
                return f"Edit #{idx} requires non-empty search_block and string replace_block."
            count = new.count(search)
            if count != 1:
                return (
                    f"Edit #{idx} expected exactly 1 match for search_block (found {count}). "
                    "No changes were written."
                )
            new = new.replace(search, replace, 1)

        if new == old:
            return "No changes produced."

        preview(file_path, scrub(old), scrub(new))
        if not self._confirm(
            f"Apply {len(edits)} edits to {file_path}?", scope="edit"
        ):
            return "Edit declined."
        if path.read_bytes() != old_bytes:
            return "File changed during review."
        if self.hooks is not None:
            ok, hook_msg = self.hooks.run("pre_edit", file_path=file_path)
            if not ok:
                return f"Blocked by pre_edit hook: {hook_msg}"
        self._record_undo(path, True, old_bytes)
        atomic_write(path, new)
        record(self.root / ".agent", "EDIT", f"{file_path} ({len(edits)} hunks)")
        if self.hooks is not None:
            self.hooks.run("post_edit", file_path=file_path)
        return f"Saved ({len(edits)} edits)."

    def write_file(self, file_path: str, content: str) -> str:
        """Create a new workspace file or overwrite an existing one after approval."""
        path = self.path(file_path)
        exists = path.exists()
        try:
            old_bytes = path.read_bytes() if exists else b""
            old = old_bytes.decode("utf8") if exists else ""
        except UnicodeDecodeError:
            return "Cannot overwrite non-UTF-8 file."
        preview(file_path, scrub(old), scrub(content))
        action = "Overwrite" if exists else "Create"
        if not self._confirm(f"{action} {file_path}?", scope="edit"):
            return "Edit declined."
        if self.hooks is not None:
            ok, hook_msg = self.hooks.run("pre_edit", file_path=file_path)
            if not ok:
                return f"Blocked by pre_edit hook: {hook_msg}"
        self._record_undo(path, exists, old_bytes)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, content)
        record(self.root / ".agent", "EDIT", file_path)
        if self.hooks is not None:
            self.hooks.run("post_edit", file_path=file_path)
        return "Saved."

    def delete_file(self, file_path: str) -> str:
        """Delete a workspace file after explicit confirmation."""
        path = self.path(file_path)
        if not path.exists():
            return f"File not found: {file_path}"
        if path.is_dir():
            return "Cannot delete directories with delete_file."
        if not self._confirm(f"Delete {file_path}?", scope="delete"):
            return "Delete declined."
        self._record_undo(path, True, path.read_bytes())
        path.unlink()
        record(self.root / ".agent", "DELETE", file_path)
        return f"Deleted {file_path}."

    def run_process(
        self,
        args: list[str],
        timeout_seconds: int = 60,
        *,
        require_approval: bool = True,
        scope: str | None = None,
    ) -> tuple[int, str, str]:
        """Run an argv command with separated stdout and stderr."""
        if not args:
            return -1, "", "Empty command."
        display = shlex.join(args)
        if require_approval and not self._confirm(
            f"Execute {display!r} in {self.root}?",
            scope=scope or f"shell:{Path(args[0]).name}",
        ):
            return -2, "", "Command declined."
        record(self.root / ".agent", "EXECUTE", display)
        try:
            proc = subprocess.run(
                args,
                cwd=self.root,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=min(max(int(timeout_seconds), 1), 600),
                check=False,
            )
            record(
                self.root / ".agent", "RESULT", f"{display} (exit {proc.returncode})"
            )
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            return -3, "", "Command timed out."
        except OSError as exc:
            return -4, "", f"Command failed to start: {exc}"
        finally:
            notify(f"Finished: {args[0]}", self.notifications)

    def _seatbelt_wrap(self, exec_args: list[str]) -> list[str]:
        """Optionally confine shell commands on macOS to workspace + /tmp writes (no network)."""
        if not self.sandbox_shell or sys.platform != "darwin" or not shutil.which("sandbox-exec"):
            return exec_args
        ws = str(self.root).replace('"', '\\"')
        profile = (
            "(version 1)"
            "(allow default)"
            "(deny network*)"
            "(deny file-write*)"
            f'(allow file-write* (subpath "{ws}") (subpath "/tmp") (subpath "/private/tmp") (literal "/dev/null"))'
        )
        return ["sandbox-exec", "-p", profile, *exec_args]

    def fetch_url(self, url: str) -> str:
        """Fetch an approved public http(s) URL (docs/API reference) and return readable text."""
        parsed = urllib.parse.urlparse((url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "Only valid http:// or https:// URLs are supported."
        host = parsed.hostname.lower()
        if host in {"localhost", "169.254.169.254", "metadata.google.internal"}:
            return "Blocked internal/metadata host."
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_link_local or ip.is_loopback:
                return "Blocked loopback/link-local address."
        except ValueError:
            pass
        if not self._confirm(f"Fetch URL {url}?", scope="web"):
            return "Fetch declined."
        req = urllib.request.Request(url, headers={"User-Agent": "adk-coder/0.1"})
        ctx = None
        if parsed.scheme == "https":
            try:
                import certifi

                ctx = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                raw = resp.read(200_000).decode("utf8", errors="replace")
        except (OSError, urllib.error.URLError) as exc:
            return f"Fetch failed: {exc}"
        cleaned = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", " ", raw, flags=re.IGNORECASE)
        cleaned = html.unescape(re.sub(r"<[^>]+>", " ", cleaned))
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return scrub(cleaned[:24_000])

    def run_shell_command(self, command: str, timeout_seconds: int = 60) -> str:
        """Run an explicitly approved command in the workspace."""
        cleaned = (command or "").strip()
        if not cleaned:
            return "Command declined."
        use_shell = bool(self.shell_mode and SHELL_META.search(cleaned))
        try:
            tokens = shlex.split(cleaned)
        except ValueError:
            tokens = cleaned.split()
        if not tokens:
            return "Command declined."
        scope = f"shell:{Path(tokens[0]).name}"
        if not self._confirm(f"Execute {cleaned!r} in {self.root}?", scope=scope):
            return "Command declined."
        if self.hooks is not None:
            ok, hook_msg = self.hooks.run("pre_shell", command=cleaned)
            if not ok:
                return f"Blocked by pre_shell hook: {hook_msg}"
        record(self.root / ".agent", "EXECUTE", cleaned)
        try:
            if use_shell:
                exec_args = (
                    ["cmd", "/c", cleaned]
                    if sys.platform == "win32"
                    else ["/bin/sh", "-c", cleaned]
                )
            else:
                exec_args = tokens
            exec_args = self._seatbelt_wrap(exec_args)
            self._abort_flag = False
            timeout_lim = min(max(int(timeout_seconds), 1), 600)
            with spinning(f"Running {Path(tokens[0]).name}"):
                proc = subprocess.Popen(
                    exec_args,
                    cwd=self.root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    errors="replace",
                )
                self._active_proc = proc
                try:
                    stdout, stderr = proc.communicate(timeout=timeout_lim)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                    raise
                finally:
                    if self._active_proc is proc:
                        self._active_proc = None
            if self._abort_flag:
                return "Command aborted by user."
            output = scrub((stdout + stderr)[-32000:])
            record(
                self.root / ".agent",
                "RESULT",
                f"{cleaned} (exit {proc.returncode})",
            )
            if self.hooks is not None:
                self.hooks.run("post_shell", command=cleaned)
            return format_result(cleaned, proc.returncode, output)
        except subprocess.TimeoutExpired:
            return "Command timed out."
        except OSError as exc:
            return f"Command failed to start: {exc}"
        finally:
            notify(f"Command finished: {tokens[0]}", self.notifications)
