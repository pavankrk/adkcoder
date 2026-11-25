"""Multi-sub-agent Debug Orchestrator (ADK Workflow + specialized sub-agents).

Delegates log analysis, root-cause investigation, surgical patching, and
verification across specialized ADK sub-agents:
  1. log_analyst             — parses/triages scrubbed logs & exception chains
  2. root_cause_investigator — correlates frames with workspace code & git history
  3. fix_engineer            — applies minimal verified patch (guarded by approval)
  4. verifier                — runs repro/test command & triggers retry or rollback
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel

from .audit import response_json
from .changelog import record
from .scrubber import scrub
from .spinner import spinning
from .tools import IGNORED_DIRS, _is_protected_part, atomic_write
from .ui import console

DEBUG_REPORT_FILE = "DEBUG_REPORT.md"
DEFAULT_TAIL_LINES = 400
DEFAULT_MAX_RETRIES = 2

# Patterns for extracting file:line references across common runtimes
_PY_FRAME_RE = re.compile(r'File\s+"([^"]+)",\s+line\s+(\d+)(?:,\s+in\s+(\S+))?')
_NODE_FRAME_RE = re.compile(
    r"at\s+(?:[\w$.<>]+\s+\()?((?:[A-Za-z]:)?[^():\s]+\.(?:[jt]sx?|mjs|cjs)):(\d+)(?::\d+)?\)?"
)
_JAVA_FRAME_RE = re.compile(
    r"at\s+([\w.$]+)\(([\w$]+\.(?:java|kt)):(\d+)\)"
)
_GO_FRAME_RE = re.compile(r"([A-Za-z0-9_./\\-]+\.go):(\d+)")
_GENERIC_LOC_RE = re.compile(
    r"(?<![\w/\\])([A-Za-z0-9_./\\-]+\.(?:py|js|ts|tsx|jsx|java|kt|go|rs|rb|php|cs|cpp|c|h)):(\d+)"
)
_ERROR_LINE_RE = re.compile(
    r"(?i)\b(traceback|exception|error|fatal|critical|panic|caused by|assertionerror|"
    r"typeerror|valueerror|keyerror|attributeerror|indexerror|runtimeerror|"
    r"nullpointerexception|syntaxerror|importerror|modulenotfounderror|"
    r"filenotfounderror|permissionerror|connectionerror|timeout|failed|segfault)\b"
)


@dataclass
class FrameRef:
    file: str
    line: int
    symbol: str = ""
    workspace_path: str | None = None


@dataclass
class ParsedLog:
    total_lines: int
    tail_lines: int
    languages: list[str] = field(default_factory=list)
    error_lines: list[tuple[str, int]] = field(default_factory=list)
    frames: list[FrameRef] = field(default_factory=list)
    stack_blocks: list[str] = field(default_factory=list)
    tail_excerpt: str = ""
    source_label: str = "inline"

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": self.source_label,
            "total_lines": self.total_lines,
            "inspected_lines": self.tail_lines,
            "detected_runtimes": self.languages,
            "top_errors": [
                {"message": msg, "count": count} for msg, count in self.error_lines[:25]
            ],
            "stack_frames": [
                {
                    "file": f.file,
                    "line": f.line,
                    "symbol": f.symbol,
                    "workspace_path": f.workspace_path,
                }
                for f in self.frames[:25]
            ],
            "stack_traces": self.stack_blocks[:6],
            "recent_tail": self.tail_excerpt,
        }


def _resolve_workspace_file(root: Path | None, raw_path: str) -> str | None:
    """Map an absolute, relative, or bare filename from a stack frame to a workspace path."""
    if root is None or not raw_path:
        return None
    cleaned = raw_path.replace("\\", "/").strip()
    if not cleaned or cleaned.startswith("<"):
        return None
    root_resolved = root.resolve()

    # 1. Direct relative or absolute path match inside workspace
    candidate = Path(cleaned)
    try:
        resolved = candidate.resolve() if candidate.is_absolute() else (root_resolved / candidate).resolve()
        if resolved.is_file():
            rel = resolved.relative_to(root_resolved)
            return rel.as_posix()
    except (OSError, ValueError):
        pass

    # 2. Strip leading directories from container/build paths (e.g. /app/src/foo.py -> src/foo.py)
    parts = [p for p in cleaned.split("/") if p and p != "."]
    for start in range(1, len(parts)):
        sub = Path(*parts[start:])
        candidate = (root_resolved / sub).resolve()
        try:
            if candidate.is_file():
                return candidate.relative_to(root_resolved).as_posix()
        except (OSError, ValueError):
            continue

    # 3. Basename lookup for Java/Kotlin frames (e.g. OrderService.java)
    basename = Path(cleaned).name
    if basename:
        matches: list[Path] = []
        for found in root_resolved.rglob(basename):
            if any(
                part in {".git", ".venv", "venv", "node_modules", "__pycache__", "target", "build", "dist"}
                for part in found.parts
            ):
                continue
            if found.is_file():
                matches.append(found)
                if len(matches) > 1:
                    break
        if len(matches) == 1:
            try:
                return matches[0].relative_to(root_resolved).as_posix()
            except ValueError:
                pass
    return None


def _extract_json_log_line(line: str) -> str | None:
    stripped = line.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        obj = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    level = str(obj.get("level") or obj.get("severity") or obj.get("lvl") or "").upper()
    msg = str(obj.get("message") or obj.get("msg") or obj.get("error") or obj.get("err") or "")
    err = str(obj.get("stack") or obj.get("traceback") or obj.get("exception") or "")
    if level in {"ERROR", "FATAL", "CRITICAL", "PANIC", "SEVERE"} or err or "error" in obj:
        parts = [p for p in (f"[{level}]" if level else "", msg, err) if p]
        return " ".join(parts).strip()
    return None


def parse_logs(
    raw_text: str,
    root: Path | None = None,
    *,
    tail: int = DEFAULT_TAIL_LINES,
    source_label: str = "inline",
) -> ParsedLog:
    """Deterministically parse, deduplicate, and scrub multi-runtime logs."""
    scrubbed = scrub(raw_text or "")
    all_lines = scrubbed.splitlines()
    total = len(all_lines)
    lines = all_lines[-tail:] if tail > 0 and total > tail else all_lines

    runtimes: set[str] = set()
    error_counts: Counter[str] = Counter()
    frames: list[FrameRef] = []
    seen_frames: set[tuple[str, int]] = set()
    stack_blocks: list[str] = []

    def _add_frame(file_path: str, line_no: int, symbol: str = "") -> None:
        key = (file_path, line_no)
        if key in seen_frames:
            return
        seen_frames.add(key)
        ws = _resolve_workspace_file(root, file_path)
        frames.append(FrameRef(file=file_path, line=line_no, symbol=symbol, workspace_path=ws))

    idx = 0
    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()
        if not stripped:
            idx += 1
            continue

        # JSON structured log support
        json_err = _extract_json_log_line(stripped)
        if json_err:
            runtimes.add("JSON-log")
            error_counts[json_err[:240]] += 1
            for m in _GENERIC_LOC_RE.finditer(json_err):
                _add_frame(m.group(1), int(m.group(2)))
            idx += 1
            continue

        # Python Traceback block
        if stripped.startswith("Traceback (most recent call last):"):
            runtimes.add("Python")
            block = [line]
            idx += 1
            while idx < len(lines) and (
                lines[idx].startswith((" ", "\t")) or not lines[idx].strip()
            ):
                block.append(lines[idx])
                m = _PY_FRAME_RE.search(lines[idx])
                if m:
                    _add_frame(m.group(1), int(m.group(2)), m.group(3) or "")
                idx += 1
            if idx < len(lines):
                block.append(lines[idx])
                error_counts[lines[idx].strip()[:240]] += 1
                idx += 1
            stack_blocks.append("\n".join(block[:40]))
            continue

        # Java / Kotlin stack frame
        m_java = _JAVA_FRAME_RE.search(stripped)
        if m_java:
            runtimes.add("Java/JVM")
            _add_frame(m_java.group(2), int(m_java.group(3)), m_java.group(1))
            idx += 1
            continue

        # Python standalone frame
        m_py = _PY_FRAME_RE.search(stripped)
        if m_py:
            runtimes.add("Python")
            _add_frame(m_py.group(1), int(m_py.group(2)), m_py.group(3) or "")
            idx += 1
            continue

        # Node.js / TypeScript frame
        m_node = _NODE_FRAME_RE.search(stripped)
        if m_node:
            runtimes.add("Node.js/TypeScript")
            _add_frame(m_node.group(1), int(m_node.group(2)))
            idx += 1
            continue

        # Go panic/frame
        m_go = _GO_FRAME_RE.search(stripped)
        if m_go and (".go:" in stripped):
            runtimes.add("Go")
            _add_frame(m_go.group(1), int(m_go.group(2)))

        # Capture Java/Node exception blocks when followed by indented `at ...` lines
        if _ERROR_LINE_RE.search(stripped):
            normalized = re.sub(
                r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?Z?\b", "<TS>", stripped
            )
            error_counts[normalized[:240]] += 1
            if idx + 1 < len(lines) and re.match(r"^\s+at\s+", lines[idx + 1]):
                block = [line]
                j = idx + 1
                while j < len(lines) and (
                    re.match(r"^\s+at\s+", lines[j])
                    or lines[j].lstrip().startswith("Caused by:")
                    or re.match(r"^\s+\.\.\.\s+\d+\s+more", lines[j])
                ):
                    block.append(lines[j])
                    mj = _JAVA_FRAME_RE.search(lines[j])
                    if mj:
                        runtimes.add("Java/JVM")
                        _add_frame(mj.group(2), int(mj.group(3)), mj.group(1))
                    mn = _NODE_FRAME_RE.search(lines[j])
                    if mn:
                        runtimes.add("Node.js/TypeScript")
                        _add_frame(mn.group(1), int(mn.group(2)))
                    j += 1
                stack_blocks.append("\n".join(block[:40]))
                idx = j
                continue

        # Generic file:line in error lines
        if _ERROR_LINE_RE.search(stripped):
            for mg in _GENERIC_LOC_RE.finditer(stripped):
                _add_frame(mg.group(1), int(mg.group(2)))
        idx += 1

    # Prioritize workspace-matched frames first
    frames.sort(key=lambda f: (0 if f.workspace_path else 1, f.file, f.line))
    tail_excerpt = "\n".join(lines[-80:])[-6000:]

    return ParsedLog(
        total_lines=total,
        tail_lines=len(lines),
        languages=sorted(runtimes) or ["Generic"],
        error_lines=error_counts.most_common(25),
        frames=frames,
        stack_blocks=stack_blocks[:6],
        tail_excerpt=tail_excerpt,
        source_label=source_label,
    )


def _recent_git_context(root: Path) -> str:
    """Gather concise recent commits + uncommitted diff stat for regression detection."""
    parts: list[str] = []
    for label, cmd in (
        ("Recent commits", ["git", "log", "-n", "5", "--oneline"]),
        ("Working tree diff", ["git", "diff", "--stat"]),
    ):
        try:
            proc = subprocess.run(
                cmd,
                cwd=root,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=5,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                parts.append(f"### {label}\n{scrub(proc.stdout.strip())[:1500]}")
        except (OSError, subprocess.TimeoutExpired):
            continue
    return "\n\n".join(parts)


_IGNORED_SEARCH_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "target",
    "build",
    "dist",
    ".agent",
}


def _discover_companion_files(root: Path, frames: list[FrameRef], limit: int = 4) -> list[str]:
    """Discover production source files corresponding to failing test files."""
    root_resolved = root.resolve()
    seen: set[str] = {f.workspace_path for f in frames if f.workspace_path}
    companions: list[str] = []

    def _find_unique(filename: str) -> str | None:
        matches: list[Path] = []
        for found in root_resolved.rglob(filename):
            if any(part in _IGNORED_SEARCH_DIRS for part in found.parts):
                continue
            if found.is_file():
                matches.append(found)
                if len(matches) > 2:
                    break
        for m in matches:
            rel = m.relative_to(root_resolved).as_posix()
            if rel not in seen:
                return rel
        return None

    for frame in frames:
        ws = frame.workspace_path
        if not ws:
            continue
        p = Path(ws)
        stem = p.stem
        suffix = p.suffix
        candidates: list[str] = []

        # Java / Kotlin: FooTest.java, FooTests.java, FooIT.java -> Foo.java
        if suffix in {".java", ".kt"}:
            for tag in ("Tests", "Test", "IT", "Spec"):
                if stem.endswith(tag) and len(stem) > len(tag):
                    candidates.append(stem[: -len(tag)] + suffix)
        # Python: test_foo.py / foo_test.py -> foo.py
        elif suffix == ".py":
            if stem.startswith("test_") and len(stem) > 5:
                candidates.append(stem[5:] + ".py")
            if stem.endswith("_test") and len(stem) > 5:
                candidates.append(stem[:-5] + ".py")
        # JS / TS: foo.test.ts / foo.spec.tsx -> foo.ts / foo.tsx
        elif suffix in {".js", ".ts", ".jsx", ".tsx"}:
            for tag in (".test", ".spec"):
                if stem.endswith(tag):
                    candidates.append(stem[: -len(tag)] + suffix)
        # Go: foo_test.go -> foo.go
        elif suffix == ".go" and stem.endswith("_test"):
            candidates.append(stem[:-5] + ".go")

        for cand in candidates:
            rel = _find_unique(cand)
            if rel and rel not in seen:
                seen.add(rel)
                companions.append(rel)
                if len(companions) >= limit:
                    return companions
    return companions


def _load_suspect_files(
    root: Path,
    frames: list[FrameRef],
    *,
    max_files: int = 4,
    max_chars_per_file: int = 5500,
) -> dict[str, str]:
    """Load bounded, line-numbered suspect files (failing frames + companion prod files)."""
    ordered: list[str] = []
    for f in frames:
        if f.workspace_path and f.workspace_path not in ordered:
            ordered.append(f.workspace_path)
    for comp in _discover_companion_files(root, frames):
        if comp not in ordered:
            ordered.append(comp)

    loaded: dict[str, str] = {}
    for rel in ordered[:max_files]:
        path = root / rel
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf8", errors="replace").splitlines()
        except OSError:
            continue
        numbered = [f"{i:4d}: {line}" for i, line in enumerate(lines[:220], 1)]
        if len(lines) > 220:
            numbered.append(f"... ({len(lines) - 220} more lines truncated)")
        loaded[rel] = scrub("\n".join(numbered))[:max_chars_per_file]
    return loaded


def _sanitize_subagent_output(text: str, *, max_chars: int = 4500) -> str:
    """Trim runaway thinking-model monologues so downstream sub-agents get concise signal."""
    cleaned = scrub((text or "").strip())
    if not cleaned:
        return ""

    # If a structured Markdown section exists after preamble/thinking, start from it.
    heading_match = re.search(
        r"(?m)^#{1,3}\s+(Root Cause|Failure Brief|Primary Error|Evidence|Fix Plan|Summary)\b",
        cleaned,
    )
    if heading_match and heading_match.start() > 400:
        cleaned = cleaned[heading_match.start() :]

    # Detect repetitive reasoning-loop monologues ("Let me reconsider..." repeated many times)
    loop_hits = len(
        re.findall(r"(?i)\b(let me reconsider|let me recompute|actually, wait|hmm,? wait)\b", cleaned)
    )
    if loop_hits >= 5 and len(cleaned) > 1600:
        # Keep only structured/code/bullet lines or concise head+tail
        useful = [
            line
            for line in cleaned.splitlines()
            if line.lstrip().startswith(("#", "-", "*", "|", "```"))
            and not re.search(r"(?i)\b(let me reconsider|let me recompute)\b", line)
        ]
        if useful:
            condensed = "\n".join(useful[:60])[:max_chars]
            return condensed + "\n\n_(Condensed repetitive reasoning trace.)_"
        return cleaned[:1200] + "\n...\n" + cleaned[-800:]

    if len(cleaned) > max_chars:
        return cleaned[:max_chars] + "\n_(truncated)_"
    return cleaned


def _locate_fuzzy_block(content: str, search_block: str) -> str | None:
    """Return exact search_block if present, else match ignoring trailing line whitespace."""
    if not search_block:
        return None
    if search_block in content:
        return search_block
    target_lines = [ln.rstrip() for ln in search_block.splitlines()]
    if not target_lines:
        return None
    content_lines = content.splitlines(keepends=True)
    window = len(target_lines)
    for idx in range(len(content_lines) - window + 1):
        chunk = content_lines[idx : idx + window]
        if [ln.rstrip("\r\n").rstrip() for ln in chunk] == target_lines:
            matched = "".join(chunk)
            if not search_block.endswith("\n") and matched.endswith("\n"):
                matched = matched.rstrip("\r\n")
            return matched
    return None


async def _apply_structured_patch_fallback(
    h,
    suspect_files: dict[str, str],
    log_brief: str,
    rca_brief: str,
    feedback: str,
) -> tuple[int, str]:
    """Fallback when fix_engineer makes 0 tool edits: request strict JSON edits and apply them."""
    if not suspect_files:
        return 0, "No suspect workspace files available for structured patch fallback."

    payload = {
        "instruction": "Return ONLY JSON with minimal exact edits to fix the failure.",
        "log_brief": log_brief[:2500],
        "root_cause": rca_brief[:2500],
        "feedback": feedback[:2000],
        "files": suspect_files,
    }
    with spinning("Debug [3/4] Fallback: generating structured JSON patch"):
        raw = await h.ask(
            "You are a surgical patch generator. Do NOT write prose or do manual arithmetic loops. "
            "Return ONLY a JSON object of the form:\n"
            '{"edits": [{"file": "relative/path", "search_block": "exact lines from file (without line numbers)", '
            '"replace_block": "replacement lines", "explanation": "short reason"}]}\n'
            "Use exact verbatim lines from the files (strip the leading `NNNN: ` line-number prefixes).",
            json.dumps(payload)[:32000],
        )

    try:
        parsed_patch = response_json(raw)
    except ValueError:
        return 0, "Structured patch fallback did not return valid JSON."

    edits = parsed_patch.get("edits") if isinstance(parsed_patch, dict) else None
    if not isinstance(edits, list) or not edits:
        # Also tolerate single-edit top-level object
        if isinstance(parsed_patch, dict) and parsed_patch.get("file") and parsed_patch.get("search_block"):
            edits = [parsed_patch]
        else:
            return 0, "Structured patch fallback returned 0 edits."

    applied = 0
    summaries: list[str] = []
    for item in edits[:8]:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("file") or "").strip()
        old = item.get("search_block")
        new = item.get("replace_block")
        expl = scrub(str(item.get("explanation") or "")).strip()
        if not rel or not isinstance(old, str) or not isinstance(new, str) or old == new:
            continue
        try:
            target = h.tools.path(rel)
            if not target.is_file():
                continue
            current = target.read_text(encoding="utf8", errors="replace")
        except (ValueError, OSError):
            continue
        matched_old = _locate_fuzzy_block(current, old)
        if not matched_old:
            summaries.append(f"Skipped `{rel}`: search_block not found.")
            continue
        res = h.tools.apply_diff(rel, matched_old, new)
        if res == "Saved.":
            applied += 1
            summaries.append(f"Patched `{rel}`" + (f": {expl}" if expl else "."))
        else:
            summaries.append(f"Skipped `{rel}`: {res}")

    return applied, "\n".join(summaries) or "No edits applied."


def _workspace_diff_summary(root: Path) -> str:
    """Return concise git diff stat for modified files if inside a git repository."""
    try:
        proc = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=root,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return scrub(proc.stdout.strip())[:1200]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def _code_excerpts_for_frames(root: Path, frames: list[FrameRef], limit: int = 8) -> dict[str, str]:
    """Read focused +/-20-line code windows around workspace stack frames."""
    excerpts: dict[str, str] = {}
    for frame in frames:
        if not frame.workspace_path:
            continue
        key = f"{frame.workspace_path}:{frame.line}"
        if key in excerpts:
            continue
        path = root / frame.workspace_path
        if not path.is_file():
            continue
        try:
            file_lines = path.read_text(encoding="utf8", errors="replace").splitlines()
        except OSError:
            continue
        start = max(1, frame.line - 20)
        end = min(len(file_lines), frame.line + 20)
        numbered = [
            f"{'>>' if ln == frame.line else '  '} {ln:4d}: {file_lines[ln - 1]}"
            for ln in range(start, end + 1)
        ]
        excerpts[key] = scrub("\n".join(numbered))[:2400]
        if len(excerpts) >= limit:
            break
    return excerpts


def _parse_debug_args(args: list[str]) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "cmd": None,
        "analyze_only": False,
        "tail": DEFAULT_TAIL_LINES,
        "out": "docs/DEBUG_REPORT.md",
        "max_retries": DEFAULT_MAX_RETRIES,
        "targets": [],
    }
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--cmd" and i + 1 < len(args):
            opts["cmd"] = args[i + 1]
            i += 2
        elif tok in {"--analyze-only", "--no-fix"}:
            opts["analyze_only"] = True
            i += 1
        elif tok == "--tail" and i + 1 < len(args):
            try:
                opts["tail"] = max(20, int(args[i + 1]))
            except ValueError as exc:
                raise ValueError("--tail requires a positive integer.") from exc
            i += 2
        elif tok == "--out" and i + 1 < len(args):
            opts["out"] = args[i + 1]
            i += 2
        elif tok == "--max-retries" and i + 1 < len(args):
            try:
                opts["max_retries"] = max(1, min(int(args[i + 1]), 5))
            except ValueError as exc:
                raise ValueError("--max-retries requires an integer between 1 and 5.") from exc
            i += 2
        elif tok.startswith("--"):
            raise ValueError(
                "Usage: /debug [logfile|@file|error-text] [--cmd <command>] "
                "[--tail N] [--analyze-only] [--out docs/DEBUG_REPORT.md] [--max-retries N]"
            )
        else:
            opts["targets"].append(tok)
            i += 1
    return opts


def _autodetect_debug_command(root: Path) -> str:
    """Infer a sensible build/test command when workspace test_command is not configured."""
    if (root / "pom.xml").is_file():
        return "mvn -B test"
    if (root / "gradlew").is_file():
        return "./gradlew test"
    if (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
        return "gradle test"
    pkg_json = root / "package.json"
    if pkg_json.is_file():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf8"))
            if isinstance(data, dict) and isinstance(data.get("scripts"), dict) and data["scripts"].get("test"):
                return "npm test"
        except (OSError, json.JSONDecodeError):
            pass
    if (root / "go.mod").is_file():
        return "go test ./..."
    if (root / "Cargo.toml").is_file():
        return "cargo test"
    if (
        (root / "pyproject.toml").is_file()
        or (root / "pytest.ini").is_file()
        or (root / "tests").is_dir()
    ):
        return "pytest -q"
    return ""


def _autodetect_latest_log(root: Path) -> Path | None:
    """Return the most recently modified .log file in the workspace (excluding ignored dirs)."""
    candidates: list[tuple[float, Path]] = []
    for parent, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not _is_protected_part(d)]
        for name in files:
            if not name.lower().endswith(".log") or _is_protected_part(name):
                continue
            p = Path(parent, name)
            try:
                st = p.stat()
                if st.st_size > 0:
                    candidates.append((st.st_mtime, p))
            except OSError:
                continue
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _collect_log_input(h, opts: dict[str, Any]) -> tuple[str, str, int | None]:
    """Return (raw_log_text, source_label, command_exit_code)."""
    if opts["cmd"]:
        with spinning(f"Running command to capture logs: {opts['cmd']}"):
            output = h.tools.run_shell_command(opts["cmd"], 600)
        parts = output.split(None, 2)
        exit_code = int(parts[1]) if len(parts) >= 2 and parts[0] == "Exit" and parts[1].lstrip("-").isdigit() else None
        return output, f"cmd:{opts['cmd']}", exit_code

    targets: list[str] = opts["targets"]
    if targets:
        combined_chunks: list[str] = []
        labels: list[str] = []
        for raw_target in targets:
            candidate_str = raw_target.removeprefix("@")
            candidate_path: Path | None = None
            try:
                p = h.tools.path(candidate_str)
                if p.is_file():
                    candidate_path = p
            except ValueError:
                p_abs = Path(candidate_str).expanduser()
                if p_abs.is_file():
                    candidate_path = p_abs
            if candidate_path is not None:
                data = candidate_path.read_text(encoding="utf8", errors="replace")
                combined_chunks.append(f"=== {candidate_path.name} ===\n{data}")
                labels.append(candidate_path.name)
            else:
                combined_chunks.append(raw_target)
                labels.append("inline")
        return "\n\n".join(combined_chunks), ", ".join(dict.fromkeys(labels)), None

    # No explicit file or --cmd: try configured test_command or auto-detected project test command
    test_cmd = h.test_command() or _autodetect_debug_command(h.root)
    if test_cmd:
        with spinning(f"Running command to capture failure: {test_cmd}"):
            output = h.tools.run_shell_command(test_cmd, 600)
        parts = output.split(None, 2)
        exit_code = int(parts[1]) if len(parts) >= 2 and parts[0] == "Exit" and parts[1].lstrip("-").isdigit() else None
        opts["cmd"] = test_cmd
        return output, f"cmd:{test_cmd}", exit_code

    # Fall back to the most recent workspace .log file if present
    latest_log = _autodetect_latest_log(h.root)
    if latest_log is not None:
        data = latest_log.read_text(encoding="utf8", errors="replace")
        rel = latest_log.relative_to(h.root).as_posix()
        return f"=== {rel} ===\n{data}", rel, None

    raise ValueError(
        "Provide a log file, pasted error text, or --cmd <command>. "
        "Example: `/debug logs/app.log`, `/debug --cmd \"pytest\"`, or `/debug \"NullPointerException...\"`"
    )


def _format_debug_report(
    root: Path,
    parsed: ParsedLog,
    log_analysis: str,
    root_cause_report: str,
    fix_outcome: str = "",
) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    frame_lines = [
        f"- `{f.workspace_path or f.file}:{f.line}`" + (f" ({f.symbol})" if f.symbol else "")
        for f in parsed.frames[:15]
    ]
    err_lines = [
        f"- ({cnt}x) `{msg}`" for msg, cnt in parsed.error_lines[:15]
    ]
    sections = [
        f"# Debug & Root-Cause Report — {root.name}",
        (
            f"_Generated: {stamp} | Source: `{parsed.source_label}` | "
            f"Lines inspected: {parsed.tail_lines}/{parsed.total_lines} | "
            f"Runtimes: {', '.join(parsed.languages)}_"
        ),
        "## 1. Parsed Signals (Deterministic)",
        "### Top Error Signatures\n" + ("\n".join(err_lines) or "- _(no explicit error keywords matched)_"),
        "### Mapped Stack Frames\n" + ("\n".join(frame_lines) or "- _(no file:line frames extracted)_"),
        "## 2. Sub-Agent 1 — Log Analyst\n" + scrub(log_analysis.strip() or "_(skipped)_"),
        "## 3. Sub-Agent 2 — Root-Cause Investigator\n" + scrub(root_cause_report.strip() or "_(skipped)_"),
    ]
    if fix_outcome.strip():
        sections.append("## 4. Sub-Agent 3 & 4 — Fix & Verification\n" + scrub(fix_outcome.strip()))
    return "\n\n".join(sections).rstrip() + "\n"


def _model_api_base(h, model_name: str) -> str | None:
    if model_name.startswith("openai/"):
        return h.config.get("openai_api_base")
    if model_name.startswith(("ollama/", "ollama_chat/")):
        return h.config.get("ollama_api_base")
    return None


def build_debug_sub_agents(h) -> dict[str, Any]:
    """Construct standalone ADK Agent definitions for the debug sub-agent team.

    Useful both for inspection/introspection and for ADK AgentTool delegation.
    """
    from google.adk.agents import Agent

    from .model_factory import resolve_model

    fast_model_name = h.config.get("fast_model") or h.model
    max_tokens = h.config.get("max_tokens") if isinstance(h.config.get("max_tokens"), int) else None
    req_timeout = (
        h.config.get("request_timeout")
        if isinstance(h.config.get("request_timeout"), int)
        else None
    )
    fast_resolved = resolve_model(
        fast_model_name,
        api_base=_model_api_base(h, fast_model_name),
        max_tokens=max_tokens,
        timeout=req_timeout,
    )
    main_resolved = resolve_model(
        h.model,
        api_base=_model_api_base(h, h.model),
        max_tokens=max_tokens,
        timeout=req_timeout,
    )

    read_tools = [
        h.tools.list_dir,
        h.tools.find_files,
        h.tools.search_files,
        h.tools.read_file,
    ]
    write_tools = [
        *read_tools,
        h.tools.apply_diff,
        h.tools.apply_multi_diff,
        h.tools.write_file,
        h.tools.run_shell_command,
    ]
    verify_tools = [
        *read_tools,
        h.tools.run_shell_command,
    ]

    log_analyst = Agent(
        name="log_analyst",
        model=fast_resolved,
        description="Triages raw/parsed application logs and stack traces into a concise failure brief.",
        instruction=(
            "You are the Log Analyst sub-agent. Analyze the supplied scrubbed log summary, "
            "stack traces, and error clusters. Identify the primary failure signature, exact "
            "exception chain, frequency/trigger pattern, and suspect files/symbols. "
            "Do not guess code you haven't seen; stay factual and concise."
        ),
        tools=[],
        output_key="log_analysis",
    )

    root_cause_investigator = Agent(
        name="root_cause_investigator",
        model=main_resolved,
        description="Correlates log errors with workspace source files and git history to pinpoint root cause.",
        instruction=(
            "You are the Root-Cause Investigator sub-agent. Using the Log Analyst's brief and "
            "read-only workspace tools, inspect the relevant source files, callers, config, and "
            "recent changes. Output a structured Root-Cause Analysis with: "
            "(1) Exact Root Cause with file:line citations, "
            "(2) Why it happens, "
            "(3) Minimal Fix Plan listing exact files and changes needed, "
            "(4) Suggested verification command or regression test."
        ),
        tools=read_tools,
        output_key="root_cause_report",
    )

    fix_engineer = Agent(
        name="fix_engineer",
        model=main_resolved,
        description="Applies a minimal surgical fix for a confirmed root cause.",
        instruction=(
            "You are the Fix Engineer sub-agent. Apply the minimal, clean, surgical fix "
            "for the confirmed root cause using apply_diff / apply_multi_diff / write_file. "
            "Preserve existing architecture and coding standards. Add or update a focused "
            "regression test when appropriate. Summarize every file modified."
        ),
        tools=write_tools,
        output_key="fix_summary",
    )

    verifier = Agent(
        name="verifier",
        model=fast_resolved,
        description="Verifies a fix by running reproduction/test commands and checking for regressions.",
        instruction=(
            "You are the Verifier sub-agent. Inspect the fix and verification command output. "
            "Start your verdict with PASS or RETRY, followed by concise evidence."
        ),
        tools=verify_tools,
        output_key="verification_verdict",
    )

    return {
        "log_analyst": log_analyst,
        "root_cause_investigator": root_cause_investigator,
        "fix_engineer": fix_engineer,
        "verifier": verifier,
    }


def build_debug_agent_tool(h):
    """Wrap the read-only Root-Cause Investigator sub-agent as an ADK AgentTool."""
    from google.adk.tools import AgentTool

    agents = build_debug_sub_agents(h)
    return AgentTool(agent=agents["root_cause_investigator"])


async def debug(h, args: list[str]) -> str:
    """Run the ADK multi-sub-agent debugging workflow: parse -> triage -> RCA -> fix -> verify."""
    from google.adk import Event, Workflow

    from .model_factory import run_agent

    opts = _parse_debug_args(args)
    raw_log, source_label, initial_exit = _collect_log_input(h, opts)
    if initial_exit == 0 and opts["cmd"] and not _ERROR_LINE_RE.search(raw_log):
        return f"Command `{opts['cmd']}` exited 0 with no detected errors."

    parsed = parse_logs(raw_log, h.root, tail=opts["tail"], source_label=source_label)
    git_ctx = _recent_git_context(h.root)
    frame_excerpts = _code_excerpts_for_frames(h.root, parsed.frames)
    suspect_files = _load_suspect_files(h.root, parsed.frames)

    read_tools = [
        h.tools.list_dir,
        h.tools.find_files,
        h.tools.search_files,
        h.tools.read_file,
    ]
    write_tools = [
        *read_tools,
        h.tools.apply_diff,
        h.tools.apply_multi_diff,
        h.tools.write_file,
        h.tools.run_shell_command,
    ]

    log_analysis_text = ""
    rca_text = ""
    fix_notes: list[str] = []
    attempt = 0
    last_attempt_edits = 0
    max_retries: int = opts["max_retries"]
    verify_cmd: str | None = opts["cmd"] or h.test_command()
    edits_before = h.tools.edit_history_count()
    saved_report_path: Path = h.state / DEBUG_REPORT_FILE

    def _run_step_banner(step_label: str) -> None:
        if console.is_terminal:
            console.print(f"[dim]  -> Delegating to sub-agent: [bold cyan]{step_label}[/bold cyan][/]")

    async def log_analyst_node():
        nonlocal log_analysis_text
        _run_step_banner("log_analyst")
        payload = json.dumps(parsed.to_payload(), indent=2)[:28000]
        with spinning("Debug [1/4] Sub-agent: log_analyst"):
            raw = await h.ask(
                "You are the Log Analyst sub-agent. Triage the scrubbed log data and stack traces. "
                "Be concise (under 25 lines). Return Markdown with: ### Primary Error Signature, "
                "### Exception Chain, ### Suspect Files/Lines, and ### Trigger Pattern. Stay strictly factual.",
                payload,
                fast=True,
            )
        log_analysis_text = _sanitize_subagent_output(raw, max_chars=3500)
        return log_analysis_text

    async def root_cause_investigator_node(node_input: str):
        nonlocal rca_text
        _run_step_banner("root_cause_investigator")
        brief = {
            "log_analyst_brief": node_input,
            "workspace_frame_excerpts": frame_excerpts,
            "suspect_source_files": suspect_files,
            "recent_git_context": git_ctx,
        }
        with spinning("Debug [2/4] Sub-agent: root_cause_investigator"):
            raw = await h.ask(
                "You are the Root-Cause Investigator sub-agent. Inspect the provided suspect source "
                "files (and use read-only tools if you need other files) to confirm the exact root cause. "
                "CRITICAL RULES: Keep your response concise (under 35 lines). Do NOT perform long "
                "hand calculations or loop over possibilities; state concrete code defects directly. "
                "Return Markdown with sections: ### Root Cause, ### Evidence (file:line), "
                "### Fix Plan, and ### Verification.",
                json.dumps(brief)[:36000],
                read_tools,
            )
        rca_text = _sanitize_subagent_output(raw, max_chars=4500)
        return rca_text

    def report_and_gate_node(node_input: str):
        nonlocal saved_report_path
        report_md = _format_debug_report(h.root, parsed, log_analysis_text, rca_text)
        atomic_write(h.state / DEBUG_REPORT_FILE, report_md, mode=0o600)

        if console.is_terminal:
            console.print(
                Panel(
                    Markdown(report_md[:3500]),
                    title="[bold cyan]Debug Root-Cause Analysis[/]",
                    border_style="cyan",
                )
            )

        out_arg = opts["out"]
        if out_arg:
            target_md = h.tools.path(out_arg)
            if h.approve(f"Write debug report to {out_arg}?"):
                target_md.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(target_md, report_md)
                saved_report_path = target_md
                record(h.state, "DEBUG_REPORT", out_arg)

        if opts["analyze_only"] or h.mode == "plan":
            return Event(route="done", output=report_md)

        allowed = h.approve(
            "Delegate fix & verification to fix_engineer and verifier sub-agents?"
        )
        return Event(route="fix" if allowed else "done", output=node_input)

    async def fix_engineer_node(node_input: str):
        nonlocal attempt, last_attempt_edits
        attempt += 1
        attempt_edits_before = h.tools.edit_history_count()
        _run_step_banner(f"fix_engineer (attempt {attempt}/{max_retries})")

        # Reload suspect files fresh on every attempt so retries see latest code on disk
        fresh_files = _load_suspect_files(h.root, parsed.frames)
        file_blocks = "\n\n".join(
            f"### File: `{rel}`\n```\n{body}\n```" for rel, body in fresh_files.items()
        )
        verify_hint = (
            f"Verification command: `{verify_cmd}` (you may run it via run_shell_command to test your fix)."
            if verify_cmd
            else "No verification command configured."
        )
        prompt_body = (
            f"## Log Analyst Brief\n{log_analysis_text}\n\n"
            f"## Root-Cause Investigator Report\n{rca_text}\n\n"
            f"## Current Suspect Files (line-numbered)\n{file_blocks or '_(use read_file/search_files)_'}\n\n"
            f"## Verification\n{verify_hint}\n\n"
            f"## Previous Attempt / Feedback\n{node_input if attempt > 1 else 'First fix attempt.'}"
        )
        with spinning(f"Debug [3/4] Sub-agent: fix_engineer (attempt {attempt}/{max_retries})"):
            raw_result = await h.ask(
                "You are the Fix Engineer sub-agent. ACT IMMEDIATELY WITH TOOLS — call apply_diff, "
                "apply_multi_diff, or write_file to fix the root cause (strip line-number prefixes "
                "when passing search_block). "
                "CRITICAL RULES: Do NOT do long arithmetic or simulate code execution by hand — use "
                "apply_diff and run_shell_command to observe real test output. If BOTH production code "
                "and a test expectation are demonstrably wrong, fix both cleanly. Keep your final "
                "summary under 15 lines listing the exact files and changes made.",
                prompt_body[:36000],
                write_tools,
            )
        result = _sanitize_subagent_output(raw_result, max_chars=2500)
        edits_made = h.tools.edit_history_count() - attempt_edits_before

        # Zero-edit recovery: if the agent returned prose without calling any edit tool,
        # invoke the strict JSON patch fallback so we actually patch the code instead of looping.
        if edits_made == 0:
            if console.is_terminal:
                console.print(
                    "[yellow]  ! fix_engineer made 0 file edits — invoking structured patch fallback...[/]"
                )
            fb_count, fb_summary = await _apply_structured_patch_fallback(
                h,
                fresh_files,
                log_analysis_text,
                rca_text,
                node_input if attempt > 1 else result,
            )
            edits_made = h.tools.edit_history_count() - attempt_edits_before
            result = (
                f"Structured patch fallback applied {fb_count} edit(s):\n{fb_summary}"
                if fb_count > 0
                else f"No file edits applied.\nFallback: {fb_summary}\nAgent note: {result[:600]}"
            )

        last_attempt_edits = edits_made
        diff_stat = _workspace_diff_summary(h.root)
        note = f"### Attempt {attempt} ({edits_made} file edit(s))\n{result}"
        if diff_stat:
            note += f"\n\nDiff stat:\n```\n{diff_stat}\n```"
        fix_notes.append(note)
        return result

    async def verifier_node(node_input: str):
        _run_step_banner(f"verifier (attempt {attempt}/{max_retries})")
        total_edits = h.tools.edit_history_count() - edits_before

        # Skip burning a heavy test command run if fix_engineer made zero edits on this attempt
        if last_attempt_edits == 0 and total_edits == 0:
            msg = (
                "Zero file edits were applied by fix_engineer. "
                "You MUST call apply_diff or write_file to modify the buggy source file(s)."
            )
            fix_notes.append(f"Verifier skipped test run: {msg}")
            if attempt < max_retries:
                return Event(route="retry", output=msg)
            return Event(route="rollback", output=f"No edits were applied after {attempt} attempt(s).")

        cmd_output = ""
        cmd_passed: bool | None = None
        if verify_cmd:
            with spinning(f"Debug [4/4] Sub-agent: verifier running `{verify_cmd}`"):
                cmd_output = await asyncio.to_thread(h.tools.run_shell_command, verify_cmd, 600)
            cmd_passed = cmd_output.split()[:2] == ["Exit", "0"]

        if cmd_passed is True:
            fix_notes.append(f"Verification `{verify_cmd}` passed (Exit 0).")
            return Event(route="pass", output=f"Fix verified cleanly (`{verify_cmd}` exited 0).\n{node_input}")

        if cmd_passed is False:
            # Parse failure output cleanly so retry gets crisp error lines, not raw build noise
            fail_parsed = parse_logs(cmd_output, h.root, tail=120, source_label=f"verify:{verify_cmd}")
            compact_errors = "\n".join(
                f"- ({cnt}x) {msg}" for msg, cnt in fail_parsed.error_lines[:15]
            )
            compact_feedback = (
                f"Verification command `{verify_cmd}` failed.\n"
                f"Top failures:\n{compact_errors or '(see tail below)'}\n\n"
                f"Output tail:\n{cmd_output[-2000:]}"
            )
            fix_notes.append(f"Verification `{verify_cmd}` failed:\n```\n{cmd_output[-1500:]}\n```")
            if attempt < max_retries:
                return Event(route="retry", output=compact_feedback)
            return Event(
                route="rollback",
                output=f"Verification failed after {attempt} attempt(s):\n{compact_errors or cmd_output[-1500:]}",
            )

        # No verify command configured: ask verifier sub-agent to review applied changes
        with spinning("Debug [4/4] Sub-agent: verifier reviewing patch"):
            verdict = await h.ask(
                "You are the Verifier sub-agent. Review the fix summary against the root cause. "
                "Start your response with PASS or RETRY and explain concisely (under 10 lines).",
                f"Root cause:\n{rca_text}\n\nFix summary:\n{node_input}"[:24000],
                read_tools,
                fast=True,
            )
        verdict = _sanitize_subagent_output(verdict, max_chars=1500)
        fix_notes.append(f"Verifier review: {verdict}")
        if verdict.lstrip().upper().startswith("PASS") or attempt >= max_retries:
            return Event(route="pass", output=verdict)
        return Event(route="retry", output=verdict)

    def rollback_node(node_input: str):
        edits_added = max(0, h.tools.edit_history_count() - edits_before)
        rollback_msg = ""
        if edits_added > 0:
            rollback_msg = h.tools.undo_last_edit(edits_added)
            fix_notes.append(f"Auto-rollback applied ({edits_added} edit(s)): {rollback_msg}")
        final_md = _format_debug_report(
            h.root, parsed, log_analysis_text, rca_text, "\n\n".join(fix_notes)
        )
        atomic_write(h.state / DEBUG_REPORT_FILE, final_md, mode=0o600)
        if saved_report_path != h.state / DEBUG_REPORT_FILE:
            atomic_write(saved_report_path, final_md)
        summary = (
            f"Debug fix could not be verified and was rolled back.\n"
            f"{rollback_msg}\n{node_input}\nReport saved to {saved_report_path}."
        ).strip()
        return Event(message=summary)

    def finish_node(node_input: str):
        final_md = _format_debug_report(
            h.root,
            parsed,
            log_analysis_text,
            rca_text,
            "\n\n".join(fix_notes) if fix_notes else "",
        )
        atomic_write(h.state / DEBUG_REPORT_FILE, final_md, mode=0o600)
        if saved_report_path != h.state / DEBUG_REPORT_FILE:
            atomic_write(saved_report_path, final_md)
        h.write(
            "context_summary.md",
            h.read("context_summary.md")
            + f"\nDeveloper: /debug {source_label}\nAssistant: Root cause analyzed; report at {saved_report_path}.\n",
        )
        if fix_notes:
            msg = f"Debug workflow completed.\n{node_input}\nReport saved to {saved_report_path}."
        else:
            msg = f"Debug analysis saved to {saved_report_path} ({len(parsed.error_lines)} error signature(s), {len(parsed.frames)} frame(s))."
        return Event(message=msg)

    graph = Workflow(
        name="debug_subagent_workflow",
        edges=[
            ("START", log_analyst_node, root_cause_investigator_node, report_and_gate_node),
            (report_and_gate_node, {"fix": fix_engineer_node, "done": finish_node}),
            (fix_engineer_node, verifier_node),
            (
                verifier_node,
                {
                    "pass": finish_node,
                    "retry": fix_engineer_node,
                    "rollback": rollback_node,
                },
            ),
        ],
    )
    return await run_agent(graph, f"Debug {source_label}", graph=True)
