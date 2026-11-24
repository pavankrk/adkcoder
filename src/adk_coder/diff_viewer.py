"""Terminal diff rendering with line numbers, surrounding context, and stats."""

import difflib
from pathlib import Path

from pygments.lexers import get_lexer_for_filename
from pygments.util import ClassNotFound
from rich.syntax import Syntax
from rich.table import Table

from .ui import console, pause_live


def show_patch(patch: str) -> None:
    with pause_live():
        console.print(Syntax(patch or "(no changes)", "diff", word_wrap=True))


def preview(path: str, before: str, after: str, context: int = 2) -> None:
    left, right = before.splitlines(), after.splitlines()
    opcodes = difflib.SequenceMatcher(None, left, right).get_opcodes()
    added = sum(m - k for kind, _, _, k, m in opcodes if kind in {"insert", "replace"})
    removed = sum(j - i for kind, i, j, _, _ in opcodes if kind in {"delete", "replace"})

    with pause_live():
        console.print(
            f"[bold cyan]{path}[/] [green]+{added}[/] [red]-{removed}[/]"
        )
        if console.width < 100 or not console.is_terminal:
            show_patch(
                "\n".join(difflib.unified_diff(left, right, path, path, lineterm=""))
            )
            return
        try:
            lexer = get_lexer_for_filename(Path(path).name).aliases[0]
        except ClassNotFound:
            lexer = "text"

        table = Table(
            "Original",
            "Replacement",
            expand=True,
            border_style="dim",
            show_lines=True,
        )
        has_rows = False
        for group in difflib.SequenceMatcher(None, left, right).get_grouped_opcodes(context):
            old_start = group[0][1]
            old_end = group[-1][2]
            new_start = group[0][3]
            new_end = group[-1][4]
            old_highlight = {
                line
                for kind, i, j, _, _ in group
                if kind in {"delete", "replace"}
                for line in range(i + 1, j + 1)
            }
            new_highlight = {
                line
                for kind, _, _, k, m in group
                if kind in {"insert", "replace"}
                for line in range(k + 1, m + 1)
            }
            table.add_row(
                Syntax(
                    "\n".join(left[old_start:old_end]) or " ",
                    lexer,
                    line_numbers=True,
                    start_line=old_start + 1,
                    highlight_lines=old_highlight,
                    word_wrap=True,
                ),
                Syntax(
                    "\n".join(right[new_start:new_end]) or " ",
                    lexer,
                    line_numbers=True,
                    start_line=new_start + 1,
                    highlight_lines=new_highlight,
                    word_wrap=True,
                ),
            )
            has_rows = True
        if has_rows:
            console.print(table)
        else:
            console.print("[dim](no changes)[/]")
