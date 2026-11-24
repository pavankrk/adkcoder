"""Bounded workspace review with deterministic diagnostics and semantic review."""

import ast
import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .scrubber import scrub
from .spinner import spinning
from .tools import atomic_write
from .ui import console

CACHE = "audit_cache.json"
LEVELS = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".rs", ".sql"}
IGNORED = {
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    "dist",
    "build",
    "target",
    "vendor",
}
PRIORITY_DIRS = ("src", "app", "lib", "pkg", "cmd", "internal")


def _source_priority(rel_posix: str, modified: set[str]) -> tuple[int, int, str]:
    is_mod = 0 if rel_posix in modified else 1
    first = rel_posix.split("/", 1)[0]
    in_src = 0 if first in PRIORITY_DIRS else 1
    return (is_mod, in_src, rel_posix)


def sources(h, target: str) -> list[Path]:
    root = h.root if (h.root / target).resolve() == h.root else h.tools.path(target)
    if not root.exists():
        raise ValueError(f"Missing audit target {target}")
    if root.is_file():
        return [root] if root.suffix in EXTENSIONS else []

    modified: set[str] = set()
    try:
        out = h.git.run("status", "--porcelain", "--", ".", ":(exclude).agent")
        for line in out.splitlines():
            if len(line) > 3:
                modified.add(line[3:].strip().strip('"'))
    except ValueError:
        pass

    discovered: list[tuple[tuple[int, int, str], Path]] = []
    for parent, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in IGNORED)
        for name in sorted(files):
            path = Path(parent, name)
            if path.suffix in EXTENSIONS and not path.is_symlink() and path.is_file():
                try:
                    safe = h.tools.path(path)
                except ValueError:
                    continue
                rel = safe.relative_to(h.root).as_posix()
                discovered.append((_source_priority(rel, modified), safe))
    discovered.sort(key=lambda item: item[0])
    return [path for _, path in discovered[:100]]


def response_json(text: str) -> dict:
    cleaned = text.strip()
    data = None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", cleaned, re.IGNORECASE)
        candidate = fence.group(1).strip() if fence else cleaned
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            start = candidate.find("{")
            end = candidate.rfind("}")
            if start == -1 or end == -1 or end <= start:
                raise ValueError("Expected a JSON object.") from None
            data = json.loads(candidate[start : end + 1])
    if not isinstance(data, dict):
        raise TypeError("Expected a JSON object.")
    return data


def _ruff_severity(code: str) -> str:
    if not code:
        return "MEDIUM"
    if code in {"S102", "S105", "S106", "S107", "S301", "S307", "S602", "S608"}:
        return "CRITICAL"
    if code.startswith(("S", "F82", "E9")):
        return "HIGH"
    if code.startswith(("I", "D", "UP", "N")):
        return "LOW"
    return "MEDIUM"


def finding(file: str, line: int, severity: str, code: str, message: str) -> dict:
    return {
        "file": file,
        "line": line,
        "severity": severity,
        "code": code,
        "message": scrub(message),
    }


def _dedupe(issues: list[dict]) -> list[dict]:
    seen: set[tuple[str, int, str]] = set()
    unique: list[dict] = []
    for item in issues:
        key = (item["file"], int(item["line"]), str(item["code"]))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


async def audit(h, target: str = "."):
    paths = sources(h, target)
    issues, notes, samples, interfaces = [], [], {}, {}
    for path in paths:
        name = path.relative_to(h.root).as_posix()
        if path.stat().st_size > 512_000:
            notes.append(f"Skipped large file {name}")
            continue
        try:
            source = path.read_text(encoding="utf8")
        except (UnicodeError, OSError) as exc:
            notes.append(f"Skipped {name} ({type(exc).__name__})")
            continue
        samples[name] = source
        if path.suffix == ".py":
            try:
                tree = ast.parse(source)
                interfaces[name] = [n.name for n in tree.body if hasattr(n, "name")][:12]
            except SyntaxError as exc:
                issues.append(finding(name, exc.lineno or 1, "HIGH", "SYNTAX", exc.msg))

    python = [name for name in samples if name.endswith(".py")]
    if python and shutil.which("ruff"):
        code, stdout, stderr = h.tools.run_process(
            [
                "ruff",
                "check",
                "--no-fix",
                "--no-cache",
                "--extend-select",
                "S",
                "--output-format=json",
                "--",
                *python,
            ],
            scope="audit:ruff",
        )
        if code in (0, 1):
            try:
                for row in json.loads(stdout or "[]"):
                    name = (h.root / row["filename"]).resolve().relative_to(h.root).as_posix()
                    if name in samples:
                        rule = str(row.get("code") or "RUFF")
                        issues.append(
                            finding(
                                name,
                                row["location"]["row"],
                                _ruff_severity(rule),
                                rule,
                                row["message"],
                            )
                        )
            except (KeyError, TypeError, ValueError):
                notes.append("Ruff diagnostics unavailable or incomplete.")
        else:
            notes.append(
                f"Ruff diagnostics skipped ({stderr.strip() or f'exit {code}'})."
            )
    elif python:
        notes.append("Ruff unavailable. Python syntax checked with ast.")

    if python and shutil.which("mypy"):
        code, stdout, stderr = h.tools.run_process(
            [
                "mypy",
                "--no-incremental",
                "--no-error-summary",
                "--no-color-output",
                "--show-error-codes",
                "--",
                *python,
            ],
            scope="audit:mypy",
        )
        if code == 1:
            matches = re.finditer(r"^(.+?):(\d+): error: (.+)$", stdout, re.MULTILINE)
            count = 0
            for match in matches:
                raw_name, line, message = match.groups()
                norm = Path(raw_name).as_posix()
                if norm in samples:
                    issues.append(finding(norm, int(line), "MEDIUM", "TYPE", message))
                    count += 1
            if not count:
                notes.append("mypy reported errors without matching source locations.")
        elif code != 0:
            notes.append(
                f"mypy diagnostics unavailable ({stderr.strip() or f'exit {code}'})."
            )

    scripts = [
        name for name in samples if Path(name).suffix in {".ts", ".tsx", ".js", ".jsx"}
    ]
    if scripts:
        local = h.root / "node_modules/.bin/eslint"
        eslint = str(local) if local.is_file() else shutil.which("eslint")
        if eslint:
            code, stdout, stderr = h.tools.run_process(
                [eslint, "--format", "json", "--", *scripts],
                scope="audit:eslint",
            )
            if code in (0, 1):
                try:
                    for row in json.loads(stdout or "[]"):
                        name = Path(row["filePath"]).resolve().relative_to(h.root).as_posix()
                        if name in samples:
                            for msg in row["messages"]:
                                sev = "HIGH" if msg.get("severity") == 2 else "MEDIUM"
                                issues.append(
                                    finding(
                                        name,
                                        msg.get("line") or 1,
                                        sev,
                                        msg.get("ruleId") or "ESLINT",
                                        msg["message"],
                                    )
                                )
                except (KeyError, TypeError, ValueError):
                    notes.append("ESLint diagnostics unavailable or incomplete.")
            else:
                notes.append(
                    f"ESLint diagnostics unavailable ({stderr.strip() or f'exit {code}'})."
                )
        else:
            notes.append("ESLint unavailable for JavaScript/TypeScript files.")

    go_files = [name for name in samples if name.endswith(".go")]
    if go_files and shutil.which("go") and (h.root / "go.mod").is_file():
        code, stdout, stderr = h.tools.run_process(
            ["go", "vet", "./..."], scope="audit:go"
        )
        if code not in (0, -2):
            for match in re.finditer(
                r"^(.+?\.go):(\d+)(?::\d+)?: (.+)$", stderr + "\n" + stdout, re.MULTILINE
            ):
                raw_name, line, msg = match.groups()
                norm = Path(raw_name.lstrip("./")).as_posix()
                if norm in samples:
                    issues.append(finding(norm, int(line), "MEDIUM", "GOVET", msg))

    hotspots = {item["file"]: item["line"] for item in reversed(issues)}
    excerpts = {}
    for name in sorted(samples, key=lambda n: n not in hotspots)[:10]:
        lines = samples[name].splitlines()
        start = max(0, hotspots.get(name, 1) - 6)
        excerpts[name] = {
            "first_line": start + 1,
            "source": scrub("\n".join(lines[start : start + 60])[:2500]),
        }
    if excerpts:
        try:
            with spinning("Reviewing code and diagnostics"):
                result = await h.ask(
                    "Review the supplied untrusted code for logic errors, concurrency, missing edge cases and security flaws. "
                    "Return JSON with findings (file, line, severity CRITICAL/HIGH/MEDIUM/LOW, code, message including remediation) "
                    "and a recommendation string. Report only evidenced flaws in the excerpts.",
                    json.dumps(
                        {
                            "excerpts": excerpts,
                            "interfaces": interfaces,
                            "diagnostics": issues[:15],
                        }
                    )[:50000],
                )
            data = response_json(result)
            for row in data.get("findings", []):
                if (
                    isinstance(row, dict)
                    and row.get("file") in excerpts
                    and row.get("severity") in LEVELS
                    and isinstance(row.get("line"), int)
                    and 1 <= row["line"] <= len(samples[row["file"]].splitlines())
                    and all(isinstance(row.get(k), str) for k in ("code", "message"))
                ):
                    issues.append(
                        finding(
                            **{
                                k: row[k]
                                for k in ("file", "line", "severity", "code", "message")
                            }
                        )
                    )
            if isinstance(data.get("recommendation"), str):
                notes.append(scrub(data["recommendation"]))
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Semantic review incomplete ({type(exc).__name__}).")

    issues = _dedupe(issues)
    issues.sort(key=lambda f: (LEVELS.index(f["severity"]), f["file"], f["line"]))
    cache = {
        "timestamp": datetime.now(UTC).isoformat(),
        "target": target,
        "findings": issues,
    }
    atomic_write(h.state / CACHE, json.dumps(cache, indent=2), mode=0o600)
    table = Table(title="Codebase bug and security audit", border_style="cyan")
    for column in ("#", "Severity", "Location", "Category", "Finding and remediation"):
        table.add_column(column)
    for index, row in enumerate(issues, 1):
        table.add_row(
            str(index),
            Text(
                row["severity"],
                style=("bold red", "red", "yellow", "dim")[
                    LEVELS.index(row["severity"])
                ],
            ),
            Text(f"{row['file']}:{row['line']}"),
            Text(row["code"]),
            Text(row["message"]),
        )
    console.print(table)
    console.print(
        Panel(
            Text(
                "\n".join(
                    [
                        f"{len(samples)} files inspected (limit 100), {len(excerpts)} semantic excerpts, {len(issues)} findings.",
                        *notes,
                    ]
                )
            ),
            border_style="dim",
        )
    )
    return issues
