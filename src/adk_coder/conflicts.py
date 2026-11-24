"""Git merge/rebase conflict detector and AI-assisted verified resolver."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audit import response_json
from .changelog import record
from .fixer import _static_check
from .scrubber import scrub
from .spinner import spinning
from .tools import IGNORED_DIRS, _is_protected_part
from .ui import console

_CONFLICT_RE = re.compile(
    r"<<<<<<<[ \t]*([^\r\n]*)\r?\n(.*?)=======\r?\n(.*?)>>>>>>>[ \t]*([^\r\n]*)(?:\r?\n)?",
    re.DOTALL,
)


@dataclass
class ConflictHunk:
    ours_label: str
    ours_code: str
    theirs_label: str
    theirs_code: str
    raw_block: str
    start_line: int


def parse_conflict_hunks(content: str) -> list[ConflictHunk]:
    hunks: list[ConflictHunk] = []
    for m in _CONFLICT_RE.finditer(content):
        start_line = content.count("\n", 0, m.start()) + 1
        hunks.append(
            ConflictHunk(
                ours_label=m.group(1).strip() or "HEAD",
                ours_code=m.group(2),
                theirs_label=m.group(4).strip() or "incoming",
                theirs_code=m.group(3),
                raw_block=m.group(0),
                start_line=start_line,
            )
        )
    return hunks


def find_conflicted_files(root: Path, specific: str | None = None) -> list[Path]:
    if specific:
        p = (root / specific).resolve()
        if not p.is_file():
            raise ValueError(f"File not found: {specific}")
        txt = p.read_text(encoding="utf8", errors="replace")
        return [p] if _CONFLICT_RE.search(txt) else []

    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in IGNORED_DIRS or _is_protected_part(part) for part in rel_parts):
            continue
        try:
            if path.stat().st_size > 1_000_000:
                continue
            txt = path.read_text(encoding="utf8", errors="ignore")
        except OSError:
            continue
        if "<<<<<<<" in txt and "=======" in txt and ">>>>>>>" in txt and _CONFLICT_RE.search(txt):
            found.append(path)
    return found


async def resolve_conflicts(h, args: list[str]) -> str:
    """Handle `/resolve-conflicts [file] [--dry-run]`."""
    dry_run = False
    target_file: str | None = None
    for tok in args:
        if tok == "--dry-run":
            dry_run = True
        elif tok.startswith("-"):
            raise ValueError("Usage: /resolve-conflicts [file] [--dry-run]")
        else:
            target_file = tok

    conflicted = find_conflicted_files(h.root, target_file)
    if not conflicted:
        return "No Git merge conflict markers found in workspace."

    resolved_files: list[str] = []
    skipped_files: list[str] = []
    summaries: list[str] = []

    for path in conflicted:
        rel = path.relative_to(h.root).as_posix()
        original = path.read_text(encoding="utf8", errors="replace")
        hunks = parse_conflict_hunks(original)
        if not hunks:
            continue

        updated = original
        file_notes: list[str] = []
        failed_file = False

        for idx, hunk in enumerate(hunks, 1):
            lines = original.splitlines()
            ctx_before = "\n".join(lines[max(0, hunk.start_line - 15) : max(0, hunk.start_line - 1)])
            payload: dict[str, Any] = {
                "file": rel,
                "hunk_index": idx,
                "context_before": scrub(ctx_before),
                "ours_label": hunk.ours_label,
                "ours_code": scrub(hunk.ours_code),
                "theirs_label": hunk.theirs_label,
                "theirs_code": scrub(hunk.theirs_code),
            }
            with spinning(f"Resolving conflict {idx}/{len(hunks)} in {rel}"):
                raw = await h.ask(
                    "You are a Git Merge Conflict Resolver sub-agent. Preserve intended behavior "
                    "from both branches where compatible and remove all conflict markers. "
                    "Return ONLY JSON: "
                    '{"resolved_code": "...", "strategy": "merge|ours|theirs", "explanation": "short reason"}.',
                    json.dumps(payload)[:24000],
                )
            try:
                parsed = response_json(raw)
                resolved_code = parsed["resolved_code"]
                if not isinstance(resolved_code, str):
                    raise TypeError("resolved_code must be a string")
                if "<<<<<<<" in resolved_code or ">>>>>>>" in resolved_code:
                    raise ValueError("resolved_code still contains conflict markers")
            except (KeyError, ValueError, TypeError) as exc:
                file_notes.append(f"Hunk #{idx}: skipped ({exc})")
                failed_file = True
                continue

            # Preserve trailing newline consistency with raw_block
            if hunk.raw_block.endswith("\n") and resolved_code and not resolved_code.endswith("\n"):
                resolved_code += "\n"
            updated = updated.replace(hunk.raw_block, resolved_code, 1)
            strategy = scrub(str(parsed.get("strategy") or "merge"))
            expl = scrub(str(parsed.get("explanation") or "")).strip()
            file_notes.append(f"Hunk #{idx} [{strategy}]: {expl or 'resolved'}")

        if failed_file or _CONFLICT_RE.search(updated):
            skipped_files.append(rel)
            summaries.append(f"- `{rel}`: incomplete resolution ({'; '.join(file_notes)})")
            continue

        ok_static, static_msg = _static_check(path, updated)
        if not ok_static:
            skipped_files.append(rel)
            summaries.append(f"- `{rel}`: static check failed ({static_msg})")
            continue

        if dry_run:
            summaries.append(f"- `{rel}` (dry-run): {'; '.join(file_notes)}")
            continue

        res = h.tools.apply_diff(rel, original, updated)
        if res == "Saved.":
            resolved_files.append(rel)
            summaries.append(f"- `{rel}`: {'; '.join(file_notes)}")
            record(h.state, "RESOLVE_CONFLICT", rel)
        else:
            skipped_files.append(rel)
            summaries.append(f"- `{rel}`: not saved ({res})")

    # Run configured test_command after applying resolutions
    test_note = ""
    if resolved_files and not dry_run:
        cmd = h.test_command()
        if cmd:
            with spinning(f"Verifying resolved files with `{cmd}`"):
                out = h.tools.run_shell_command(cmd, 600)
            if out.split()[:2] == ["Exit", "0"]:
                test_note = f"\nVerification `{cmd}` passed (Exit 0)."
            else:
                test_note = (
                    f"\nWarning: `{cmd}` failed after conflict resolution. "
                    f"Run `/undo-edit {len(resolved_files)}` to revert or `/debug --cmd \"{cmd}\"`."
                )

    if console.is_terminal:
        console.print("\n".join(summaries))

    return (
        f"Resolved {len(resolved_files)}/{len(conflicted)} conflicted file(s).\n"
        + "\n".join(summaries)
        + test_note
    )
