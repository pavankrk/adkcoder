"""Rich terminal UI components, live token streaming, approval policy, and status panels."""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from time import monotonic

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .scrubber import scrub
from .token_manager import capacity, estimate

console = Console()
error_console = Console(stderr=True)

_active_lives: list = []


def register_live(live) -> None:
    _active_lives.append(live)


def unregister_live(live) -> None:
    if live in _active_lives:
        _active_lives.remove(live)


def stop_all_live() -> None:
    """Stop any currently active Live spinners (e.g. before streaming tokens)."""
    for live in _active_lives:
        if getattr(live, "is_started", False):
            live.stop()


def resume_all_live() -> None:
    """Resume registered Live spinners (e.g. after a tool call finishes while awaiting next LLM turn)."""
    if _active_lives:
        top = _active_lives[-1]
        if not getattr(top, "is_started", False):
            top.start(refresh=True)


@contextmanager
def pause_live():
    """Temporarily stop any active Rich Live spinners before printing prompts."""
    paused = []
    for live in _active_lives:
        if getattr(live, "is_started", False):
            live.stop()
            paused.append(live)
    try:
        yield
    finally:
        for live in reversed(paused):
            live.start(refresh=True)


class StreamDisplay:
    """Streams live thought tokens (dimmed) and syntax-highlighted Rich Markdown live."""

    def __init__(self) -> None:
        self.started_at = monotonic()
        self.started_thought = False
        self.started_text = False
        self.streamed_text = False
        self._buffer: list[str] = []
        self._live: Live | None = None
        self.tool_calls = 0
        self.prompt_tokens = 0
        self.output_tokens = 0
        self._enabled = console.is_terminal and not console.is_dumb_terminal

    def on_thought(self, chunk: str) -> None:
        if not self._enabled or not chunk:
            return
        if not self.started_thought and not chunk.strip():
            return
        stop_all_live()
        if not self.started_thought:
            prefix = "[thought] " if console.legacy_windows else "💭 "
            sys.stdout.write(f"\x1b[2m{prefix}")
            self.started_thought = True
        sys.stdout.write(chunk)
        sys.stdout.flush()

    def on_text(self, chunk: str) -> None:
        if not self._enabled or not chunk:
            return
        # Ignore leading whitespace-only chunks (e.g. "\n\n" emitted before tool calls)
        # so the Thinking spinner stays visible while the model prepares tool calls.
        if not self.started_text and not chunk.strip():
            return
        stop_all_live()
        if self.started_thought and not self.started_text:
            sys.stdout.write("\x1b[0m\n\n")
            sys.stdout.flush()
            self.started_thought = False
        self.started_text = True
        self.streamed_text = True
        self._buffer.append(chunk)
        rendered = Markdown("".join(self._buffer))
        if self._live is None:
            self._live = Live(
                rendered,
                console=console,
                refresh_per_second=12,
                vertical_overflow="visible",
                transient=False,
            )
            self._live.start(refresh=True)
        else:
            self._live.update(rendered)

    def before_tool_call(self) -> None:
        """Close any active thought/markdown stream cleanly before printing a tool call."""
        if self._live is not None:
            self._live.stop()
            self._live = None
        if self._enabled and self.started_thought and not self.started_text:
            sys.stdout.write("\x1b[0m\n")
            sys.stdout.flush()
        self.started_thought = False
        self.started_text = False
        self._buffer.clear()

    def after_tool_result(self) -> None:
        """Resume the Thinking spinner while awaiting the model's next turn."""
        if self._enabled:
            resume_all_live()

    def record_usage(self, prompt_tokens: int, output_tokens: int) -> None:
        self.prompt_tokens += prompt_tokens
        self.output_tokens += output_tokens

    def record_tool_call(self) -> None:
        self.tool_calls += 1

    def finish(self, *, final_text: str = "") -> None:
        if self._live is not None:
            if final_text:
                self._live.update(Markdown(scrub(final_text)))
            self._live.stop()
            self._live = None
        elif self._enabled and self.started_thought and not self.started_text:
            sys.stdout.write("\x1b[0m\n")
            sys.stdout.flush()

    def render_footer(self, *, fallback_output_text: str = "") -> None:
        if not self._enabled:
            return
        elapsed = max(0.01, monotonic() - self.started_at)
        out_tok = self.output_tokens or estimate(
            "".join(self._buffer) or fallback_output_text
        )
        tps = out_tok / elapsed if out_tok else 0.0
        clock = "" if console.legacy_windows else "⏱ "
        sep = " | " if console.legacy_windows else " · "
        parts = [f"{clock}{elapsed:.1f}s"]
        if tps > 0:
            parts.append(f"{tps:.1f} tok/s")
        if self.prompt_tokens or self.output_tokens:
            parts.append(f"{self.prompt_tokens:,} in / {self.output_tokens:,} out")
        elif out_tok:
            parts.append(f"~{out_tok:,} out")
        if self.tool_calls:
            parts.append(f"{self.tool_calls} tool{'s' if self.tool_calls != 1 else ''}")
        console.print(f"[dim]  {sep.join(parts)}[/]")


class ApprovalManager:
    """Wraps an approval callback with optional session-scoped 'always' memory."""

    def __init__(self, approve_fn):
        self.approve_fn = approve_fn
        self.always_scopes: set[str] = set()

    def reset(self) -> None:
        self.always_scopes.clear()

    def ask(self, message: str, scope: str | None = None) -> bool:
        if scope and scope in self.always_scopes:
            return True
        with pause_live():
            decision = self.approve_fn(message)
        if isinstance(decision, str):
            lowered = decision.strip().lower()
            if lowered in {"a", "always"}:
                if scope:
                    self.always_scopes.add(scope)
                return True
            return lowered in {"y", "yes"}
        return bool(decision)


def interactive_confirm(message: str) -> str | bool:
    """Rich interactive prompt supporting [y]es, [n]o, and [a]lways (for session)."""
    if not sys.stdin.isatty():
        try:
            return input(f"{message} [y/N] ").strip().lower() in {"y", "yes"}
        except EOFError:
            return False
    try:
        console.print(
            f"[bold yellow]?[/] {scrub(message)} [dim]([bold green]y[/]es / [bold red]N[/]o / [bold cyan]a[/]lways)[/] ",
            end="",
        )
        answer = input().strip().lower()
    except EOFError:
        return False
    if answer in {"a", "always"}:
        return "always"
    return answer in {"y", "yes"}


LOGO_LINES = (
    ("bold bright_cyan", " █████╗ ██████╗ ██╗  ██╗      ██████╗ ██████╗ ██████╗ ███████╗██████╗ "),
    ("bold cyan",        "██╔══██╗██╔══██╗██║ ██╔╝     ██╔════╝██╔═══██╗██╔══██╗██╔════╝██╔══██╗"),
    ("bold deep_sky_blue1", "███████║██║  ██║█████╔╝█████╗██║     ██║   ██║██║  ██║█████╗  ██████╔╝"),
    ("bold dodger_blue1",   "██╔══██║██║  ██║██╔═██╗╚════╝██║     ██║   ██║██║  ██║██╔══╝  ██╔══██╗"),
    ("bold blue",           "██║  ██║██████╔╝██║  ██╗     ╚██████╗╚██████╔╝██████╔╝███████╗██║  ██║"),
    ("bold magenta",        "╚═╝  ╚═╝╚═════╝ ╚═╝  ╚═╝      ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝╚═╝  ╚═╝"),
)

COMPACT_LOGO_LINES = (
    ("bold bright_cyan", "▄▀█ █▀▄ █▄▀     █▀▀ █▀█ █▀▄ █▀▀ █▀█"),
    ("bold dodger_blue1", "█▀█ █▄▀ █ █ ▀▀▀ █▄▄ █▄█ █▄▀ ██▄ █▀▄"),
)

ASCII_LOGO_LINES = (
    ("bold bright_cyan", "  ___  ____  _  __      ____ ___  ____  _____ ____  "),
    ("bold cyan",        " / _ \\|  _ \\| |/ /     / ___/ _ \\|  _ \\| ____|  _ \\ "),
    ("bold blue",        "| |_| | | | | ' /_____| |  | | | | | | |  _| | |_) |"),
    ("bold magenta",     "|_| |_|____/|_|\\_\\____|\\____\\___/|____/|_____|_| \\_\\"),
)


def render_banner(
    root: Path,
    model: str,
    branch: str = "",
    dirty: bool = False,
    *,
    mode: str = "ask",
    rules_sources: list[str] | None = None,
) -> None:
    if not console.is_terminal:
        return
    if console.legacy_windows:
        logo = ASCII_LOGO_LINES
    else:
        logo = LOGO_LINES if console.width >= 76 else COMPACT_LOGO_LINES
    logo_text = Text()
    for idx, (style, line) in enumerate(logo):
        if idx:
            logo_text.append("\n")
        logo_text.append(line, style=style)
    console.print(logo_text)

    git_label = f"{branch}{'*' if dirty else ''}" if branch else "no git"
    mode_style = {
        "plan": "bold blue",
        "ask": "bold cyan",
        "auto-edit": "bold yellow",
        "full-auto": "bold red",
    }.get(mode, "bold cyan")
    body = Text()
    body.append("Workspace: ", style="dim")
    body.append(f"{root}\n", style="bold")
    body.append("Model:     ", style="dim")
    body.append(f"{model}", style="bold cyan")
    body.append("   Mode: ", style="dim")
    body.append(mode, style=mode_style)
    body.append("   Branch: ", style="dim")
    body.append(git_label, style="bold magenta" if dirty else "green")
    if rules_sources:
        body.append("\nRules:     ", style="dim")
        body.append(", ".join(rules_sources), style="green")
    env_model = os.environ.get("ADK_CODER_MODEL")
    if env_model and env_model != model:
        body.append(
            f"\nNote:      .agent/config.json overrides ADK_CODER_MODEL ({env_model}). "
            f"Run `/model {env_model}` to switch.",
            style="yellow",
        )
    body.append(
        "\n\nType a request, !cmd for shell, @path for files, or /help. "
        "Shift+Tab mode · Alt+Enter newline · Ctrl+S status · Ctrl+C cancel.",
        style="dim",
    )
    console.print(
        Panel(
            body,
            title=f"[bold cyan]adk-coder[/] [dim]v{__version__}[/]",
            border_style="cyan",
            padding=(0, 1),
        )
    )


def render_response(text: str) -> None:
    if not text:
        return
    cleaned = scrub(text)
    if console.is_terminal:
        console.print(Markdown(cleaned))
    else:
        print(cleaned)


def render_tool_call(name: str, args: dict | None = None) -> None:
    if not console.is_terminal:
        return
    summary = ""
    if args:
        first_key = next(iter(args))
        val = scrub(str(args[first_key])).replace("\n", " ")
        if len(val) > 64:
            val = val[:61] + "..."
        summary = f" [dim]{first_key}={val}[/]"
    with pause_live():
        console.print(f"  [bold blue]⚙[/] [cyan]{name}[/]{summary}")


def render_tool_result(name: str, preview: str = "") -> None:
    if not console.is_terminal:
        return
    first = scrub(preview).splitlines()[0] if preview else "done"
    if len(first) > 80:
        first = first[:77] + "..."
    with pause_live():
        console.print(f"  [green]✓[/] [dim]{name}: {first}[/]")


def render_help(commands: dict[str, str]) -> str:
    groups = [
        ("Chat, Plan & Mode", ["mode", "plan", "explore", "paste", "tdd", "test-fix", "watch", "workflow", "review", "pr-review", "model", "profile", "compact", "reset"]),
        (
            "Audit, Security & Scaffold",
            ["analyze", "diagram", "debug", "testgen", "feature", "map", "audit", "scan", "security", "sbom", "fix", "create", "scaffold", "new", "blueprint"],
        ),
        (
            "Git, Edits & Checkpoints",
            ["commit", "undo-edit", "resolve-conflicts", "diff", "branch", "merge", "stash", "revert", "undo", "checkpoint", "sync"],
        ),
        (
            "Extensions & Workspace",
            ["init", "status", "doctor", "settings", "export", "session", "context", "pin", "unpin", "commands", "skills", "skill", "hooks", "mcp", "task", "config", "changelog", "clear", "help", "exit"],
        ),
    ]
    if console.is_terminal:
        table = Table(title="adk-coder Commands (also: !<shell cmd>, @<file>)", border_style="cyan", show_lines=False)
        table.add_column("Group", style="bold magenta", no_wrap=True)
        table.add_column("Command", style="bold cyan", no_wrap=True)
        table.add_column("Arguments", style="dim")
        seen = set()
        for group, names in groups:
            for idx, name in enumerate(names):
                if name in commands:
                    table.add_row(group if idx == 0 else "", f"/{name}", commands[name])
                    seen.add(name)
        for name, usage in commands.items():
            if name not in seen:
                table.add_row("Other", f"/{name}", usage)
        console.print(table)
        return ""
    return "\n".join(f"/{k} {v}".rstrip() for k, v in commands.items())


def render_status(
    root: Path,
    model: str,
    memory: str,
    tasks_md: str,
    *,
    mode: str = "ask",
    rules_sources: list[str] | None = None,
    context_window: int | None = None,
    test_command: str | None = None,
    tracker=None,
    branch: str = "",
    dirty: bool = False,
) -> str:
    used = estimate(memory)
    limit = capacity(model, context_window)
    pct = min(100, (100 * used) // max(limit, 1))
    plain = (
        f"{root}\n{model} [{mode}] | ~{used} / {limit} tokens ({pct}%)\n"
        + tasks_md
    )
    if not console.is_terminal:
        return plain

    bar_width = 24
    filled = round(bar_width * pct / 100)
    bar_color = "green" if pct < 50 else ("yellow" if pct < 75 else "red")
    bar = f"[{bar_color}]{'█' * filled}[/][dim]{'░' * (bar_width - filled)}[/]"

    task_lines = [line for line in tasks_md.splitlines() if line.lstrip().startswith("- [")]
    done_count = sum(1 for line in task_lines if line.lstrip().startswith(("- [x]", "- [X]")))

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", no_wrap=True)
    grid.add_column()
    grid.add_row("Workspace", str(root))
    grid.add_row("Git", f"{branch}{'*' if dirty else ''}" if branch else "(not a git repo)")
    grid.add_row("Model", f"[bold cyan]{model}[/]")
    grid.add_row("Mode", f"[bold]{mode}[/] [dim](Shift+Tab or /mode to change)[/]")
    if rules_sources:
        grid.add_row("Rules", ", ".join(rules_sources))
    grid.add_row("Context", f"{bar} ~{used:,} / {limit:,} tokens ({pct}%)")
    if tracker and tracker.total_tokens:
        grid.add_row(
            "Session API",
            f"{tracker.prompt_tokens:,} in / {tracker.output_tokens:,} out ({tracker.turns} turns)",
        )
    grid.add_row("Test cmd", test_command or "[dim](not configured — use /config test_command <cmd>)[/]")
    grid.add_row("Tasks", f"{done_count}/{len(task_lines)} completed")

    console.print(Panel(grid, title="[bold cyan]Workspace Status[/]", border_style="cyan"))
    if task_lines:
        console.print(Markdown(tasks_md))
    return ""


def render_error(exc: Exception) -> None:
    message = scrub(str(exc)) or type(exc).__name__
    hint = _error_hint(message, exc)
    if error_console.is_terminal:
        body = Text(message, style="red")
        if hint:
            body.append(f"\n\nHint: {hint}", style="yellow")
        error_console.print(Panel(body, title="[bold red]Error[/]", border_style="red"))
    else:
        print(message, file=sys.stderr)
        if hint:
            print(f"Hint: {hint}", file=sys.stderr)


def _error_hint(message: str, exc: Exception) -> str:
    lowered = message.lower()
    if (
        ("api_key" in lowered or "api key" in lowered or "credentials" in lowered)
        and not os.environ.get("GOOGLE_API_KEY")
        and not os.environ.get("OPENAI_API_KEY")
    ):
        return "Export GOOGLE_API_KEY (for Gemini) or OPENAI_API_KEY (for OpenAI/compatible endpoints)."
    if "litellm" in lowered and isinstance(exc, ImportError):
        return "Install the local extra: pip install -e '.[local]'."
    if "not a git repository" in lowered:
        return "Initialize Git with `git init` or run adk-coder from a Git repository root."
    if "rate limit" in lowered or "429" in lowered:
        return "Provider rate limit reached. Wait briefly or switch models with `/model`."
    return ""
