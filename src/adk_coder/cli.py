"""Interactive REPL, dynamic completion, and CLI entry point for adk-coder."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import signal
import sys
import sysconfig
from pathlib import Path

from . import __version__
from .extensions import list_custom_commands, list_skills
from .harness import COMMANDS, CONFIG_KEYS, AgentHarness
from .scaffolder import STACKS
from .scrubber import scrub
from .theme import bottom_toolbar, git_status, prompt
from .tools import IGNORED_DIRS, MODES, _is_protected_part
from .ui import console, render_banner, render_error, render_response, stop_all_live

QUOTED_COMMANDS = {
    "/diff",
    "/commit",
    "/undo-edit",
    "/mode",
    "/plan",
    "/session",
    "/pin",
    "/unpin",
    "/tdd",
    "/test-fix",
    "/commands",
    "/hooks",
    "/mcp",
    "/doctor",
    "/map",
    "/export",
    "/analyze",
    "/analyse",
    "/diagram",
    "/debug",
    "/testgen",
    "/pr-review",
    "/resolve-conflicts",
    "/watch",
    "/profile",
    "/feature",
    "/audit",
    "/scan",
    "/security",
    "/sbom",
    "/fix",
    "/scaffold",
    "/new",
    "/create",
    "/blueprint",
    "/branch",
    "/merge",
    "/revert",
    "/undo",
    "/stash",
    "/checkpoint",
    "/sync",
    "/config",
}

MODEL_SUGGESTIONS = (
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "openai/gpt-4o-mini",
    "openai/gpt-4o",
    "anthropic/claude-sonnet-4-20250514",
    "ollama_chat/",
    "openai/",
)


def normalize_cli_request(request_args: list[str]) -> str:
    """Normalize one-shot CLI arguments regardless of whether quoted as one string."""
    if not request_args:
        return ""
    if len(request_args) == 1:
        return request_args[0].strip()
    command = request_args[0]
    if command in QUOTED_COMMANDS:
        return shlex.join(request_args)
    return " ".join(request_args)


def _iter_workspace_files(root: Path, limit: int = 300) -> list[str]:
    found: list[str] = []
    for parent, dirs, files in os.walk(root):
        dirs[:] = sorted(
            d for d in dirs if d not in IGNORED_DIRS and not _is_protected_part(d)
        )
        for name in sorted(files):
            if _is_protected_part(name):
                continue
            found.append(Path(parent, name).relative_to(root).as_posix())
            if len(found) >= limit:
                return found
    return found


def build_completer(harness: AgentHarness):
    from prompt_toolkit.completion import Completer, Completion

    subcommands: dict[str, tuple[str, ...]] = {
        "/checkpoint": ("list", "create", "restore", "delete"),
        "/stash": ("list", "push", "show", "pop", "apply", "drop"),
        "/model": ("list", *MODEL_SUGGESTIONS),
        "/mode": MODES,
        "/plan": ("--from",),
        "/session": ("list", "new", "resume", "fork", "delete"),
        "/commands": ("list", "new"),
        "/hooks": ("list", "init"),
        "/mcp": ("list", "add", "remove"),
        "/doctor": ("--offline",),
        "/unpin": ("all",),
        "/sync": ("--release", "--title"),
        "/diff": ("--staged",),
        "/merge": ("--squash", "--rebase"),
        "/fix": ("all", "debug"),
        "/analyze": ("--out",),
        "/analyse": ("--out",),
        "/diagram": ("--out",),
        "/debug": ("--cmd", "--analyze-only", "--tail", "--out", "--max-retries"),
        "/testgen": ("--out", "--cmd", "--max-retries"),
        "/pr-review": ("main", "master", "--out", "--post"),
        "/resolve-conflicts": ("--dry-run",),
        "/watch": ("--auto-fix", "--interval", "--once"),
        "/profile": ("list", "save", "use", "fallback", "delete"),
        "/feature": ("--quick", "--from"),
        "/sbom": ("cyclonedx", "spdx"),
        "/scaffold": (*STACKS.keys(), "--from", "--minimal"),
        "/new": (*STACKS.keys(), "--from", "--minimal"),
        "/create": ("--from",),
        "/blueprint": (*STACKS.keys(), "--from"),
        "/workflow": ("--from",),
        "/task": ("list", "add", "done", "rm"),
        "/config": tuple(sorted(CONFIG_KEYS)),
    }

    class AdkCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            word = document.get_word_before_cursor(WORD=True)

            # @file completion anywhere in natural language prompts
            if word.startswith("@"):
                prefix = word[1:]
                for rel in _iter_workspace_files(harness.root):
                    if rel.startswith(prefix):
                        yield Completion(
                            "@" + rel,
                            start_position=-len(word),
                            display=rel,
                            display_meta="file",
                        )
                return

            stripped = text.lstrip()
            if not stripped.startswith("/"):
                return

            parts = stripped.split()
            ends_space = text.endswith(" ")

            if len(parts) == 1 and not ends_space:
                all_cmds = dict(COMMANDS)
                for custom_name in list_custom_commands(harness.state):
                    all_cmds.setdefault(custom_name, "(custom command)")
                for cmd, usage in all_cmds.items():
                    full = "/" + cmd
                    if full.startswith(parts[0]):
                        yield Completion(
                            full,
                            start_position=-len(parts[0]),
                            display_meta=usage or "command",
                        )
                return

            cmd = parts[0]
            current = "" if ends_space else parts[-1]
            options: list[str] = list(subcommands.get(cmd, ()))
            if cmd == "/skill":
                options.extend(list_skills(harness.state).keys())
            elif cmd == "/branch":
                try:
                    branches = harness.git.run(
                        "for-each-ref", "--format=%(refname:short)", "refs/heads"
                    ).splitlines()
                    options.extend(branches)
                    options.append("-d")
                except ValueError:
                    pass
            elif cmd in {"/analyze", "/analyse", "/diagram", "/debug", "/testgen", "/resolve-conflicts", "/audit", "/scan", "/security", "/diff", "/revert", "/undo", "/fix", "/create", "/pin", "/unpin"}:
                options.extend(_iter_workspace_files(harness.root, limit=100))

            for opt in dict.fromkeys(options):
                if opt.startswith(current):
                    yield Completion(opt, start_position=-len(current))

    return AdkCompleter()


async def repl(harness: AgentHarness) -> None:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.history import FileHistory

    from .keybindings import bindings

    class History(FileHistory):
        def store_string(self, string: str) -> None:
            super().store_string(scrub(string))

    branch, dirty = git_status(harness.root)
    rules_sources, _ = harness.load_project_rules()
    render_banner(
        harness.root,
        harness.model,
        branch,
        dirty,
        mode=harness.mode,
        rules_sources=rules_sources,
    )

    session = PromptSession(
        history=History(str(harness.state / ".history")),
        completer=build_completer(harness),
        auto_suggest=AutoSuggestFromHistory(),
        enable_history_search=True,
        multiline=True,
        key_bindings=bindings(harness.status, cycle_mode=harness.cycle_mode),
        bottom_toolbar=lambda: bottom_toolbar(harness),
    )
    loop = asyncio.get_running_loop()
    while True:
        try:
            line = (
                await session.prompt_async(
                    prompt(
                        harness.root,
                        harness.model,
                        harness.read("context_summary.md"),
                        mode=harness.mode,
                        context_window=harness.context_window,
                        styled=True,
                    )
                )
            ).strip()
            if line == "/exit":
                return
            if line:
                abort_event = asyncio.Event()
                harness.abort_signal = abort_event
                task = asyncio.create_task(harness.execute(line))

                def _on_sigint(_task=task, _ev=abort_event):
                    _ev.set()
                    stop_all_live()
                    if not _task.done():
                        _task.cancel()

                sig_installed = False
                try:
                    loop.add_signal_handler(signal.SIGINT, _on_sigint)
                    sig_installed = True
                except (NotImplementedError, RuntimeError):
                    pass
                try:
                    result = await task
                except asyncio.CancelledError:
                    stop_all_live()
                    console.print("\n[yellow]Interrupted.[/]")
                    continue
                finally:
                    if sig_installed:
                        loop.remove_signal_handler(signal.SIGINT)
                    harness.abort_signal = None
                if result and not harness.last_streamed:
                    render_response(result)
        except EOFError:
            return
        except KeyboardInterrupt:
            stop_all_live()
            continue
        except Exception as exc:  # noqa: BLE001
            render_error(exc)


def install(shell: str) -> None:
    filename = "adk-coder.bash" if shell == "bash" else "_adk-coder"
    sources = [
        Path(sysconfig.get_path("data")) / "share/adk-coder/completions",
        Path(__file__).resolve().parents[2] / "completions",
    ]
    source = next((p / filename for p in sources if (p / filename).is_file()), None)
    if not source:
        raise ValueError("Packaged completion script missing.")
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    dest = data / (
        "bash-completion/completions/adk-coder"
        if shell == "bash"
        else "zsh/site-functions/_adk-coder"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    print(f"Installed {dest}")
    if shell == "zsh":
        print(f"Add {dest.parent} to fpath before running compinit.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adk-coder")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "-c", "--continue", dest="continue_latest", action="store_true",
        help="Resume the most recent session in this workspace.",
    )
    parser.add_argument(
        "--resume", nargs="?", const="__LATEST__", default=None,
        help="Resume a named or ID session (defaults to latest).",
    )
    parser.add_argument(
        "-p", "--print", dest="print_mode", action="store_true",
        help="Headless single-turn print mode (plain output, reads piped stdin).",
    )
    parser.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Emit structured JSON output for automation/CI.",
    )
    parser.add_argument(
        "-y", "--yes", dest="auto_yes", action="store_true",
        help="Auto-approve all interactive confirmations non-interactively.",
    )
    parser.add_argument(
        "--mode", choices=MODES, default=None,
        help="Execution mode (ask, auto-edit, plan, full-auto).",
    )
    parser.add_argument(
        "--config", dest="open_config", action="store_true",
        help="Launch the lightweight Web Settings UI to configure LLM models and test connectivity.",
    )
    parser.add_argument(
        "--ui", dest="open_ui", action="store_true",
        help="Launch the standalone ADK Coder Browser Workbench (Docs, Security, SBOM, Diagrams, Profiles, Scaffold).",
    )
    parser.add_argument(
        "--mcp", dest="run_mcp", action="store_true",
        help="Run the ADK Coder MCP App server over stdio (or use `adk-coder mcp --check`).",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Optional port override for --config (default 8089) or --ui (default 8090).",
    )
    parser.add_argument("--install-completion", choices=["bash", "zsh"])
    parser.add_argument(
        "--install-mcp-plugin",
        action="store_true",
        help="Install the ADK Coder Workbench Antigravity MCP App plugin into ~/.gemini/config/plugins/.",
    )
    parser.add_argument("request", nargs=argparse.REMAINDER)
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    # Tolerate single-dash `-ui` / `-config` / `-mcp` aliases gracefully
    raw_argv = [
        ("--" + a[1:]) if a in {"-ui", "-config", "-mcp"} else a
        for a in raw_argv
    ]
    args = parser.parse_args(raw_argv)
    if args.request[:1] == ["--"]:
        args.request = args.request[1:]

    # Also support intuitive subcommand forms: `adk-coder ui`, `adk-coder config`, `adk-coder mcp`
    if args.request and args.request[0] == "ui" and not args.print_mode and not args.json_output:
        args.open_ui = True
        args.request = args.request[1:]
    elif args.request and args.request[0] == "config" and not args.print_mode and not args.json_output:
        args.open_config = True
        args.request = args.request[1:]
    elif args.request and args.request[0] == "mcp" and not args.print_mode and not args.json_output:
        args.run_mcp = True
        args.request = args.request[1:]

    try:
        from .config_ui import load_global_env
        from .config_ui import main as config_main

        load_global_env()
        if args.open_ui:
            from .workbench_ui import main as ui_main

            ui_args = ["--workspace", str(args.workspace)]
            if args.port:
                ui_args.extend(["--port", str(args.port)])
            ui_args.extend(args.request)
            return ui_main(ui_args)
        if args.open_config:
            cfg_args = ["--workspace", str(args.workspace)]
            if args.port:
                cfg_args.extend(["--port", str(args.port)])
            cfg_args.extend(args.request)
            return config_main(cfg_args)
        if args.run_mcp:
            if "--install" in args.request:
                from .mcp_plugin import install_mcp_plugin

                plugin_dir = install_mcp_plugin()
                print(f"Installed Antigravity MCP App plugin at {plugin_dir}")
                return 0
            if "--check" in args.request:
                from .mcp_server import APP_URI, create_mcp_server, load_workbench_html

                srv = create_mcp_server()
                tools = asyncio.run(srv.list_tools())
                resources = asyncio.run(srv.list_resources())
                print(
                    f"MCP Server OK: {len(tools)} tools, {len(resources)} resource(s), "
                    f"UI HTML={len(load_workbench_html()):,} bytes ({APP_URI})"
                )
                return 0
            from .mcp_server import main as mcp_main

            mcp_args = ["--workspace", str(args.workspace), *args.request]
            return mcp_main(mcp_args)
        if args.install_completion:
            install(args.install_completion)
            return 0
        if args.install_mcp_plugin:
            from .mcp_plugin import install_mcp_plugin

            plugin_dir = install_mcp_plugin()
            print(f"Installed Antigravity MCP App plugin at {plugin_dir}")
            print("Open 'ADK Coder Workbench' from Antigravity's side pane '+' menu or sidebar.")
            return 0
        approve_fn = (lambda _: True) if args.auto_yes else None
        harness = (
            AgentHarness(args.workspace, approve=approve_fn)
            if approve_fn is not None
            else AgentHarness(args.workspace)
        )
        if args.mode:
            harness.set_mode(args.mode)
        if args.continue_latest or args.resume is not None:
            key = None if (args.continue_latest or args.resume == "__LATEST__") else args.resume
            msg = harness.resume_session(key)
            if console.is_terminal and not args.json_output and not args.print_mode:
                console.print(f"[dim]{msg}[/]")

        piped = ""
        if not sys.stdin.isatty() and (args.print_mode or args.json_output or not args.request):
            try:
                piped = sys.stdin.read(65_536).strip()
            except OSError:
                piped = ""

        line = normalize_cli_request(args.request)
        if piped:
            line = f"{line}\n\n{piped}".strip() if line else piped

        if line or args.print_mode or args.json_output:
            if not line:
                raise ValueError("Provide a request argument or pipe input via stdin.")
            result = asyncio.run(harness.execute(line))
            if args.json_output:
                payload = {
                    "ok": True,
                    "workspace": str(harness.root),
                    "model": harness.model,
                    "mode": harness.mode,
                    "output": scrub(result or ""),
                    "usage": {
                        "prompt_tokens": harness.tracker.prompt_tokens,
                        "output_tokens": harness.tracker.output_tokens,
                        "turns": harness.tracker.turns,
                    },
                }
                print(json.dumps(payload, indent=2))
            elif result:
                if args.print_mode:
                    print(scrub(result))
                elif not harness.last_streamed:
                    render_response(result)
        else:
            asyncio.run(repl(harness))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        if args.json_output:
            print(json.dumps({"ok": False, "error": scrub(str(exc)) or type(exc).__name__}))
        else:
            render_error(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
