"""Styled REPL prompt and bottom status toolbar with safe Git probing and mode display."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .token_manager import capacity, estimate


def git_status(root: Path) -> tuple[str, bool]:
    def _git(*args: str) -> str:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
            )
            return proc.stdout.strip() if proc.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    branch = _git("branch", "--show-current")
    dirty = bool(
        branch and _git("status", "--porcelain", "--", ".", ":(exclude).agent")
    )
    return branch, dirty


def prompt(
    root: Path,
    model: str,
    memory: str,
    *,
    mode: str = "ask",
    context_window: int | None = None,
    styled: bool = False,
):
    branch, dirty = git_status(root)
    pct = min(100, (100 * estimate(memory)) // max(capacity(model, context_window), 1))
    if not styled:
        mark = "*" if dirty else ""
        return f"({model}) {branch}{mark} [{pct}%] ❯ "

    from prompt_toolkit.formatted_text import FormattedText

    pct_style = (
        "ansigreen" if pct < 50 else ("ansiyellow" if pct < 75 else "ansired bold")
    )
    mode_style = {
        "plan": "ansiblue bold",
        "ask": "ansicyan",
        "auto-edit": "ansiyellow bold",
        "full-auto": "ansired bold",
    }.get(mode, "ansicyan")

    tokens: list[tuple[str, str]] = [
        ("ansicyan bold", f"({model}) "),
        (mode_style, f"<{mode}> "),
    ]
    if branch:
        tokens.append(
            ("ansimagenta bold" if dirty else "ansigreen", f"{branch}{'*' if dirty else ''} ")
        )
    tokens.append((pct_style, f"[{pct}%] "))
    glyph = "> " if (sys.platform == "win32" and not os.environ.get("WT_SESSION")) else "❯ "
    tokens.append(("ansicyan bold", glyph))
    return FormattedText(tokens)


def bottom_toolbar(harness):
    from prompt_toolkit.formatted_text import FormattedText

    memory = harness.read("context_summary.md")
    used = estimate(memory)
    limit = capacity(harness.model, harness.context_window)
    pct = min(100, (100 * used) // max(limit, 1))
    scopes = len(harness.tools.always_scopes)
    scope_text = f"  |  auto: {scopes}" if scopes else ""
    return FormattedText(
        [
            (
                "class:bottom-toolbar.text",
                f" {harness.root.name}  |  mode:{harness.mode}  |  ~{used:,}/{limit:,} tok ({pct}%){scope_text}  |  ",
            ),
            ("bold", "Shift+Tab"),
            ("", " mode  "),
            ("bold", "!cmd"),
            ("", " shell  "),
            ("bold", "Alt+Enter"),
            ("", " newline  "),
            ("bold", "Ctrl+S"),
            ("", " status "),
        ]
    )
