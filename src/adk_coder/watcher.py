"""Live workspace file watcher with automatic test re-run and optional /debug auto-fix."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .debugger import debug, parse_logs
from .tools import IGNORED_DIRS, _is_protected_part
from .ui import console

WATCH_EXTENSIONS = {
    ".py",
    ".java",
    ".kt",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".go",
    ".rs",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
}


def snapshot_workspace(root: Path) -> dict[str, tuple[int, int]]:
    """Capture {relative_path: (mtime_ns, size)} for tracked source files."""
    snap: dict[str, tuple[int, int]] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in WATCH_EXTENSIONS:
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in IGNORED_DIRS or _is_protected_part(part) for part in rel_parts):
            continue
        rel = path.relative_to(root).as_posix()
        if rel in {"docs/DEBUG_REPORT.md", "docs/ANALYSIS.md", "docs/PR_REVIEW.md", "docs/DIAGRAM.md"}:
            continue
        try:
            st = path.stat()
            snap[rel] = (st.st_mtime_ns, st.st_size)
        except OSError:
            continue
    return snap


def diff_snapshots(
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
) -> list[str]:
    """Return sorted relative paths of created, modified, or deleted files."""
    changed: set[str] = set()
    for rel, meta in after.items():
        if before.get(rel) != meta:
            changed.add(rel)
    for rel in before:
        if rel not in after:
            changed.add(rel)
    return sorted(changed)


def _parse_watch_args(args: list[str]) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "cmd": None,
        "auto_fix": False,
        "interval": 1.5,
        "once": False,
    }
    cmd_parts: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--auto-fix":
            opts["auto_fix"] = True
            i += 1
        elif tok == "--once":
            opts["once"] = True
            i += 1
        elif tok == "--interval" and i + 1 < len(args):
            try:
                opts["interval"] = max(0.2, float(args[i + 1]))
            except ValueError as exc:
                raise ValueError("--interval requires a number of seconds.") from exc
            i += 2
        elif tok == "--cmd" and i + 1 < len(args):
            opts["cmd"] = args[i + 1]
            i += 2
        elif tok.startswith("--"):
            raise ValueError(
                "Usage: /watch [test-cmd] [--auto-fix] [--interval 1.5] [--once]"
            )
        else:
            cmd_parts.append(tok)
            i += 1
    if not opts["cmd"] and cmd_parts:
        opts["cmd"] = " ".join(cmd_parts)
    return opts


async def _run_watch_cycle(h, cmd: str, *, auto_fix: bool) -> tuple[bool, str]:
    output = await asyncio.to_thread(h.tools.run_shell_command, cmd, 600)
    passed = output.split()[:2] == ["Exit", "0"]
    if passed:
        msg = f"[PASS] `{cmd}` exited 0."
        if console.is_terminal:
            console.print(f"[bold green]✓ {msg}[/]")
        return True, msg

    parsed = parse_logs(output, h.root, tail=120, source_label=f"watch:{cmd}")
    top_errs = "\n".join(f"  - ({c}x) {m}" for m, c in parsed.error_lines[:8])
    fail_msg = f"[FAIL] `{cmd}` failed.\n{top_errs or output[-800:]}"
    if console.is_terminal:
        console.print(f"[bold red]✗ {fail_msg}[/]")

    if auto_fix:
        fix_res = await debug(h, ["--cmd", cmd, "--max-retries", "2"])
        return False, f"{fail_msg}\nAuto-debug result:\n{fix_res}"
    return False, fail_msg


async def watch(h, args: list[str]) -> str:
    """Handle `/watch [test-cmd] [--auto-fix] [--interval 1.5] [--once]`."""
    opts = _parse_watch_args(args)
    cmd = opts["cmd"] or h.test_command()
    if not cmd:
        raise ValueError(
            "Provide a command (`/watch \"pytest\"`) or set `test_command` with `/config test_command <cmd>`."
        )

    if opts["once"]:
        _, summary = await _run_watch_cycle(h, cmd, auto_fix=opts["auto_fix"])
        return summary

    if console.is_terminal:
        console.print(
            f"[bold cyan]Watching[/] {h.root} -> `{cmd}` "
            f"(interval: {opts['interval']}s, auto-fix: {'on' if opts['auto_fix'] else 'off'}). "
            "Press Ctrl+C to stop."
        )

    snap = snapshot_workspace(h.root)
    cycles = 0
    try:
        while True:
            if h.abort_signal is not None and h.abort_signal.is_set():
                break
            await asyncio.sleep(opts["interval"])
            latest = snapshot_workspace(h.root)
            changed = diff_snapshots(snap, latest)
            if not changed:
                continue
            cycles += 1
            if console.is_terminal:
                console.print(f"[dim]Changed: {', '.join(changed[:5])}{'...' if len(changed) > 5 else ''}[/]")
            await _run_watch_cycle(h, cmd, auto_fix=opts["auto_fix"])
            # Refresh snapshot after test/auto-fix so agent edits don't self-trigger a loop
            snap = snapshot_workspace(h.root)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    return f"Stopped watching `{cmd}` after {cycles} triggered run(s)."
