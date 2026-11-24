"""Terminal spinner that cooperates cleanly with interactive prompts, streaming, and web capture."""

import os
import sys
from contextlib import contextmanager
from time import monotonic

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner

from .ui import pause_live, register_live, unregister_live


@contextmanager
def spinning(label: str):
    is_tty = sys.stderr.isatty() or sys.stdout.isatty()
    is_dumb = os.environ.get("TERM", "").lower() == "dumb"
    if not is_tty or is_dumb:
        if is_tty:
            sys.stderr.write(f"... {label}\n")
            sys.stderr.flush()
        yield
        return

    live_console = Console(stderr=True, force_terminal=True)
    start = monotonic()
    spinner_name = "line" if live_console.legacy_windows else "dots"
    spinner = Spinner(spinner_name, style="cyan")

    class Timer:
        def __rich__(self):
            spinner.update(
                text=f"[bold cyan]{label}[/] [dim]{monotonic() - start:.1f}s[/]"
            )
            return spinner

    with pause_live():
        live = Live(
            Timer(),
            console=live_console,
            transient=True,
            refresh_per_second=12,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        register_live(live)
        try:
            with live:
                yield live
        finally:
            unregister_live(live)
