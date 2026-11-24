"""Automated unit & edge-case test generator with framework detection and self-verification."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

from .changelog import record
from .debugger import _sanitize_subagent_output, parse_logs
from .scrubber import scrub
from .spinner import spinning
from .ui import console


def detect_test_framework(root: Path, source_file: Path) -> dict[str, str]:
    """Detect the target project's test framework, default test command, and target test path."""
    rel = source_file.relative_to(root).as_posix()
    suffix = source_file.suffix.lower()

    if suffix in {".java", ".kt"}:
        is_gradle = (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file()
        default_cmd = "./gradlew test" if is_gradle else "mvn -B test"
        test_rel = (
            rel.replace("src/main/", "src/test/", 1)
            if rel.startswith("src/main/")
            else f"src/test/java/{source_file.name}"
        )
        p = Path(test_rel)
        target_rel = str(p.with_name(f"{p.stem}Test{p.suffix}")).replace("\\", "/")
        return {
            "framework": "JUnit 5",
            "command": default_cmd,
            "target_test_path": target_rel,
            "language": "Java" if suffix == ".java" else "Kotlin",
        }

    if suffix == ".py":
        stem = source_file.stem
        test_name = stem if stem.startswith("test_") else f"test_{stem}"
        target_rel = f"tests/{test_name}.py"
        return {
            "framework": "pytest",
            "command": f'"{sys.executable}" -m pytest -q {target_rel}',
            "target_test_path": target_rel,
            "language": "Python",
        }

    if suffix in {".ts", ".tsx", ".js", ".jsx"}:
        pkg_json = root / "package.json"
        fw = "Vitest"
        cmd = "npm test"
        if pkg_json.is_file():
            try:
                pkg = json.loads(pkg_json.read_text(encoding="utf8"))
                deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
                if "jest" in deps and "vitest" not in deps:
                    fw = "Jest"
            except (OSError, json.JSONDecodeError):
                pass
        target_rel = str(source_file.relative_to(root).with_name(f"{source_file.stem}.test{suffix}")).replace("\\", "/")
        return {
            "framework": fw,
            "command": cmd,
            "target_test_path": target_rel,
            "language": "TypeScript" if suffix.startswith(".ts") else "JavaScript",
        }

    if suffix == ".go":
        target_rel = str(source_file.relative_to(root).with_name(f"{source_file.stem}_test.go")).replace("\\", "/")
        return {
            "framework": "Go testing",
            "command": "go test ./...",
            "target_test_path": target_rel,
            "language": "Go",
        }

    raise ValueError(f"Unsupported source language for /testgen: {source_file.name}")


def _extract_code_block(text: str) -> str:
    match = re.search(r"```[A-Za-z0-9_+-]*\n(.*?)```", text, re.DOTALL)
    return match.group(1).rstrip() + "\n" if match else ""


def _parse_testgen_args(args: list[str]) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "source": "",
        "out": "",
        "cmd": "",
        "max_retries": 2,
    }
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--out" and i + 1 < len(args):
            opts["out"] = args[i + 1]
            i += 2
        elif tok == "--cmd" and i + 1 < len(args):
            opts["cmd"] = args[i + 1]
            i += 2
        elif tok == "--max-retries" and i + 1 < len(args):
            try:
                opts["max_retries"] = max(1, min(int(args[i + 1]), 4))
            except ValueError as exc:
                raise ValueError("--max-retries requires an integer between 1 and 4.") from exc
            i += 2
        elif tok.startswith("--"):
            raise ValueError(
                "Usage: /testgen <source-file> [--out <test-file>] [--cmd <test-cmd>] [--max-retries N]"
            )
        elif not opts["source"]:
            opts["source"] = tok.removeprefix("@")
            i += 1
        else:
            raise ValueError(
                "Usage: /testgen <source-file> [--out <test-file>] [--cmd <test-cmd>] [--max-retries N]"
            )
    if not opts["source"]:
        raise ValueError(
            "Usage: /testgen <source-file> [--out <test-file>] [--cmd <test-cmd>] [--max-retries N]"
        )
    return opts


async def generate_tests(h, args: list[str]) -> str:
    """Handle `/testgen <source-file> [--out <test-file>] [--cmd <test-cmd>] [--max-retries N]`."""
    opts = _parse_testgen_args(args)
    src_path = h.tools.path(opts["source"])
    if not src_path.is_file():
        raise ValueError(f"Source file not found: {opts['source']}")

    meta = detect_test_framework(h.root, src_path)
    target_rel = opts["out"] or meta["target_test_path"]
    target_path = h.tools.path(target_rel)
    verify_cmd = opts["cmd"] or h.test_command() or meta["command"]
    max_retries: int = opts["max_retries"]

    source_text = scrub(src_path.read_text(encoding="utf8", errors="replace"))[:16000]
    existing_test = (
        scrub(target_path.read_text(encoding="utf8", errors="replace"))[:8000]
        if target_path.is_file()
        else ""
    )

    write_tools = [
        h.tools.list_dir,
        h.tools.find_files,
        h.tools.search_files,
        h.tools.read_file,
        h.tools.write_file,
        h.tools.apply_diff,
    ]
    edits_before = h.tools.edit_history_count()
    feedback = ""

    for attempt in range(1, max_retries + 1):
        attempt_edits_before = h.tools.edit_history_count()
        prompt = (
            f"Generate comprehensive, deterministic {meta['framework']} ({meta['language']}) unit tests "
            f"for `{src_path.relative_to(h.root).as_posix()}` and save them to `{target_rel}`.\n"
            "Cover: happy paths, boundary/zero/empty values, invalid inputs, and error handling.\n"
            "Use `write_file` (or `apply_diff` if updating an existing test file) to write `{target_rel}`.\n\n"
            f"## Source File: `{src_path.relative_to(h.root).as_posix()}`\n```\n{source_text}\n```\n"
        )
        if existing_test:
            prompt += f"\n## Existing Test File: `{target_rel}`\n```\n{existing_test}\n```\n"
        if feedback:
            prompt += f"\n## Previous Test Run Failure (Attempt {attempt - 1})\n{feedback}\n"

        with spinning(f"TestGen [{attempt}/{max_retries}]: writing {target_rel}"):
            raw = await h.ask(
                f"You are a Test Engineering sub-agent specializing in {meta['framework']}. "
                f"Write clean, self-contained, passing unit tests into `{target_rel}` using write_file "
                "or apply_diff. Do NOT modify production code. Keep your summary under 10 lines.",
                prompt[:34000],
                write_tools,
            )

        # Fallback if the model returned a fenced code block without calling write_file
        if h.tools.edit_history_count() == attempt_edits_before:
            code = _extract_code_block(raw)
            if code:
                res = h.tools.write_file(target_rel, code)
                if res != "Saved.":
                    return f"Test generation halted: {res}"
            else:
                feedback = (
                    f"No file edits were made on attempt {attempt}. "
                    f"You MUST call write_file('{target_rel}', content)."
                )
                continue

        with spinning(f"TestGen [{attempt}/{max_retries}]: verifying with `{verify_cmd}`"):
            cmd_out = await asyncio.to_thread(h.tools.run_shell_command, verify_cmd, 600)

        if cmd_out.split()[:2] == ["Exit", "0"]:
            record(h.state, "TESTGEN", f"{target_rel} ({meta['framework']})")
            summary = _sanitize_subagent_output(raw, max_chars=1200)
            msg = (
                f"Generated and verified `{target_rel}` ({meta['framework']}) — "
                f"`{verify_cmd}` passed on attempt {attempt}/{max_retries}."
            )
            if console.is_terminal and summary:
                console.print(f"[green]✓ {msg}[/]")
            return msg

        parsed = parse_logs(cmd_out, h.root, tail=120, source_label=f"testgen:{verify_cmd}")
        errs = "\n".join(f"- ({c}x) {m}" for m, c in parsed.error_lines[:12])
        feedback = (
            f"Verification command `{verify_cmd}` failed.\n"
            f"Errors:\n{errs or cmd_out[-1800:]}\n\nTail:\n{cmd_out[-1500:]}"
        )
        if target_path.is_file():
            existing_test = scrub(target_path.read_text(encoding="utf8", errors="replace"))[:8000]

    # Roll back failing generated tests cleanly so workspace isn't left broken
    edits_added = max(0, h.tools.edit_history_count() - edits_before)
    rollback_note = ""
    if edits_added > 0:
        rollback_note = f"\nRolled back {edits_added} edit(s): {h.tools.undo_last_edit(edits_added)}"
    return (
        f"Generated tests in `{target_rel}` did not pass `{verify_cmd}` after {max_retries} attempt(s)."
        f"{rollback_note}\nLast failure:\n{feedback[:1200]}"
    )
