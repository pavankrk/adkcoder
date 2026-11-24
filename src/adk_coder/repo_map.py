"""Compact AST/regex symbol repo map for Python, Java, TypeScript/JS, and Go."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from .tools import IGNORED_DIRS, _is_protected_part

SUPPORTED_EXTS = {".py", ".java", ".ts", ".tsx", ".js", ".jsx", ".go"}
PRIORITY_DIRS = ("src", "app", "lib", "internal", "cmd", "pkg")

_JAVA_TYPE = re.compile(r"\b(?:public\s+)?(?:abstract\s+|final\s+|sealed\s+)?(class|interface|record|enum)\s+(\w+)")
_JAVA_ROUTE = re.compile(r"@(?:Get|Post|Put|Patch|Delete|Request)Mapping\(\s*[\"']([^\"']+)[\"']")
_JAVA_METHOD = re.compile(r"^\s*public\s+(?:static\s+)?[\w<>\[\], ?]+\s+(\w+)\s*\(", re.MULTILINE)
_TS_EXPORT = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:async\s+)?(class|interface|type|enum|function|const)\s+(\w+)",
    re.MULTILINE,
)
_GO_SYMBOL = re.compile(
    r"^(?:type\s+(\w+)\s+(?:struct|interface)|func\s+(?:\([^)]*\)\s*)?(\w+)\s*\()",
    re.MULTILINE,
)


def _extract_python_symbols(text: str) -> list[str]:
    symbols: list[str] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ["(syntax error)"]
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = [
                n.name
                for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not n.name.startswith("_")
            ][:6]
            suffix = f"({', '.join(methods)})" if methods else ""
            symbols.append(f"class {node.name}{suffix}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            symbols.append(f"def {node.name}()")
    return symbols[:12]


def _extract_java_symbols(text: str) -> list[str]:
    symbols: list[str] = []
    for kind, name in _JAVA_TYPE.findall(text)[:4]:
        symbols.append(f"{kind} {name}")
    for route in _JAVA_ROUTE.findall(text)[:6]:
        symbols.append(f"route {route}")
    for method in _JAVA_METHOD.findall(text)[:6]:
        if method not in {"if", "for", "while", "switch"}:
            symbols.append(f"{method}()")
    return symbols[:12]


def _extract_ts_symbols(text: str) -> list[str]:
    return [f"{kind} {name}" for kind, name in _TS_EXPORT.findall(text)[:12]]


def _extract_go_symbols(text: str) -> list[str]:
    out: list[str] = []
    for type_name, func_name in _GO_SYMBOL.findall(text):
        if type_name:
            out.append(f"type {type_name}")
        elif func_name and func_name[0].isupper():
            out.append(f"func {func_name}()")
        if len(out) >= 12:
            break
    return out


def extract_symbols(path: Path) -> list[str]:
    try:
        if path.stat().st_size > 256_000:
            return []
        text = path.read_text(encoding="utf8")
    except (OSError, UnicodeError):
        return []
    suffix = path.suffix.lower()
    if suffix == ".py":
        return _extract_python_symbols(text)
    if suffix == ".java":
        return _extract_java_symbols(text)
    if suffix in {".ts", ".tsx", ".js", ".jsx"}:
        return _extract_ts_symbols(text)
    if suffix == ".go":
        return _extract_go_symbols(text)
    return []


def build_repo_map(
    root: Path,
    subpath: str = ".",
    *,
    max_files: int = 50,
    max_chars: int = 3500,
) -> str:
    """Build a compact file -> symbol map suitable for LLM system context."""
    base = (root / subpath).resolve()
    if not base.exists():
        return ""

    candidates: list[tuple[tuple[int, str], Path]] = []
    if base.is_file():
        candidates.append(((0, base.relative_to(root).as_posix()), base))
    else:
        for parent, dirs, files in os.walk(base):
            dirs[:] = sorted(
                d for d in dirs if d not in IGNORED_DIRS and not _is_protected_part(d)
            )
            for name in sorted(files):
                if _is_protected_part(name):
                    continue
                full = Path(parent, name)
                if full.suffix.lower() in SUPPORTED_EXTS and not full.is_symlink():
                    rel = full.relative_to(root).as_posix()
                    top = rel.split("/", 1)[0]
                    prio = 0 if top in PRIORITY_DIRS else 1
                    candidates.append(((prio, rel), full))

    candidates.sort(key=lambda item: item[0])
    lines: list[str] = []
    used = 0
    for (_, rel), path in candidates[:max_files]:
        syms = extract_symbols(path)
        entry = f"- `{rel}`: {', '.join(syms)}" if syms else f"- `{rel}`"
        if used + len(entry) + 1 > max_chars:
            lines.append(f"- ... ({len(candidates) - len(lines)} more files)")
            break
        lines.append(entry)
        used += len(entry) + 1
    return "\n".join(lines)
