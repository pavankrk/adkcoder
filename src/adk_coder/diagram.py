"""Deterministic offline Mermaid dependency graph, API route map, and cycle detector."""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel

from .changelog import record
from .spinner import spinning
from .tools import IGNORED_DIRS, _is_protected_part, atomic_write
from .ui import console

SUPPORTED_EXTS = {".py", ".java", ".kt", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go"}

_JAVA_PKG_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
_JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;", re.MULTILINE)
_SPRING_CLASS_MAP_RE = re.compile(
    r'@RequestMapping\s*\(\s*(?:value\s*=\s*|path\s*=\s*)?["\']([^"\']+)["\']'
)
_SPRING_METHOD_MAP_RE = re.compile(
    r"@(Get|Post|Put|Delete|Patch|Request)Mapping\s*(?:\(\s*(?:(?:value|path)\s*=\s*)?(?:[\"']([^\"']*)[\"'])?[^)]*\))?"
)
_TS_IMPORT_RE = re.compile(
    r"""(?:import\s+.*?\s+from\s+|require\s*\(\s*|import\s*\(\s*)["'](\.[^"']+)["']"""
)
_EXPRESS_ROUTE_RE = re.compile(
    r"""\b(?:app|router)\.(get|post|put|delete|patch|all)\s*\(\s*["']([^"']+)["']"""
)
_GO_IMPORT_RE = re.compile(r'"([^"]+)"')
_GO_HTTP_RE = re.compile(
    r"""(?:HandleFunc|Handle|GET|POST|PUT|DELETE|PATCH)\s*\(\s*["']([^"']+)["']"""
)
_PY_ROUTE_RE = re.compile(
    r"""@(?:app|router|bp|blueprint)\.(get|post|put|delete|patch|route)\s*\(\s*["']([^"']+)["']"""
)


def _iter_source_files(root: Path, target_rel: str = ".") -> list[Path]:
    base = (root / target_rel).resolve()
    if base.is_file():
        return [base] if base.suffix in SUPPORTED_EXTS else []
    found: list[Path] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix not in SUPPORTED_EXTS:
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in IGNORED_DIRS or _is_protected_part(part) for part in rel_parts):
            continue
        found.append(path)
    return found[:300]


def _safe_node_id(name: str) -> str:
    return "n_" + re.sub(r"[^A-Za-z0-9_]", "_", name)


def _find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Find unique directed cycles using deterministic DFS."""
    visited: set[str] = set()
    in_stack: list[str] = []
    in_stack_set: set[str] = set()
    cycles: list[list[str]] = []
    seen_sigs: set[tuple[str, ...]] = set()

    def dfs(node: str) -> None:
        visited.add(node)
        in_stack.append(node)
        in_stack_set.add(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt not in visited:
                dfs(nxt)
            elif nxt in in_stack_set:
                idx = in_stack.index(nxt)
                cyc = [*in_stack[idx:], nxt]
                canon = tuple(sorted(set(cyc[:-1])))
                if canon not in seen_sigs:
                    seen_sigs.add(canon)
                    cycles.append(cyc)
        in_stack.pop()
        in_stack_set.remove(node)

    for node in sorted(graph):
        if node not in visited:
            dfs(node)
    return cycles


def analyze_structure(root: Path, target: str = ".") -> dict[str, Any]:
    """Deterministically extract internal module graph, circular deps, and HTTP API routes."""
    files = _iter_source_files(root, target)
    rel_map: dict[str, Path] = {p.relative_to(root).as_posix(): p for p in files}

    # Build lookup indexes for resolving internal imports
    py_modules: dict[str, str] = {}
    java_classes: dict[str, str] = {}
    go_dirs: dict[str, str] = {}

    for rel, path in rel_map.items():
        if path.suffix == ".py":
            parts = list(Path(rel).with_suffix("").parts)
            if parts and parts[0] == "src":
                parts = parts[1:]
            if parts and parts[-1] == "__init__":
                parts = parts[:-1]
            if parts:
                mod = ".".join(parts)
                py_modules[mod] = rel
                py_modules[parts[-1]] = py_modules.get(parts[-1], rel)
        elif path.suffix in {".java", ".kt"}:
            try:
                txt = path.read_text(encoding="utf8", errors="replace")
            except OSError:
                continue
            pkg_m = _JAVA_PKG_RE.search(txt)
            fqn = f"{pkg_m.group(1)}.{path.stem}" if pkg_m else path.stem
            java_classes[fqn] = rel
            java_classes[path.stem] = java_classes.get(path.stem, rel)
        elif path.suffix == ".go":
            dir_key = Path(rel).parent.as_posix()
            go_dirs[dir_key] = rel

    edges: dict[str, set[str]] = defaultdict(set)
    routes: list[dict[str, str]] = []

    for rel, path in rel_map.items():
        edges.setdefault(rel, set())
        try:
            text = path.read_text(encoding="utf8", errors="replace")
        except OSError:
            continue

        if path.suffix == ".py":
            # Python AST imports
            try:
                tree = ast.parse(text)
            except SyntaxError:
                tree = None
            if tree is not None:
                cur_parts = list(Path(rel).with_suffix("").parts)
                if cur_parts and cur_parts[0] == "src":
                    cur_parts = cur_parts[1:]
                cur_pkg = cur_parts[:-1]
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            target_rel = py_modules.get(alias.name)
                            if target_rel and target_rel != rel:
                                edges[rel].add(target_rel)
                    elif isinstance(node, ast.ImportFrom):
                        mod = node.module or ""
                        candidates: list[str] = []
                        if node.level > 0:
                            base = cur_pkg[: max(0, len(cur_pkg) - (node.level - 1))]
                            if mod:
                                candidates.append(".".join([*base, mod]))
                            for alias in node.names:
                                candidates.append(".".join([*base, *([mod] if mod else []), alias.name]))
                        elif mod:
                            candidates.append(mod)
                            for alias in node.names:
                                candidates.append(f"{mod}.{alias.name}")
                        for cand in candidates:
                            target_rel = py_modules.get(cand)
                            if target_rel and target_rel != rel:
                                edges[rel].add(target_rel)
                                break
            # FastAPI / Flask routes
            for m in _PY_ROUTE_RE.finditer(text):
                routes.append(
                    {
                        "method": m.group(1).upper(),
                        "path": m.group(2),
                        "file": rel,
                        "framework": "Python (FastAPI/Flask)",
                    }
                )

        elif path.suffix in {".java", ".kt"}:
            for m in _JAVA_IMPORT_RE.finditer(text):
                imp = m.group(1)
                target_rel = java_classes.get(imp)
                if target_rel and target_rel != rel:
                    edges[rel].add(target_rel)
            prefix_m = _SPRING_CLASS_MAP_RE.search(text)
            prefix = prefix_m.group(1).rstrip("/") if prefix_m else ""
            for m in _SPRING_METHOD_MAP_RE.finditer(text):
                verb = m.group(1).upper()
                if verb == "REQUEST" and prefix_m and m.start() == prefix_m.start():
                    continue
                sub_path = (m.group(2) or "").strip()
                full_path = f"{prefix}/{sub_path.lstrip('/')}" if sub_path else (prefix or "/")
                routes.append(
                    {
                        "method": "ANY" if verb == "REQUEST" else verb,
                        "path": full_path,
                        "file": rel,
                        "framework": "Spring Web",
                    }
                )

        elif path.suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs"}:
            parent = Path(rel).parent
            for m in _TS_IMPORT_RE.finditer(text):
                spec = m.group(1)
                resolved_base = (root / parent / spec).resolve()
                for ext in ("", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.js"):
                    cand = Path(str(resolved_base) + ext)
                    try:
                        cand_rel = cand.relative_to(root).as_posix()
                    except ValueError:
                        continue
                    if cand_rel in rel_map and cand_rel != rel:
                        edges[rel].add(cand_rel)
                        break
            for m in _EXPRESS_ROUTE_RE.finditer(text):
                routes.append(
                    {
                        "method": m.group(1).upper(),
                        "path": m.group(2),
                        "file": rel,
                        "framework": "Node/Express",
                    }
                )

        elif path.suffix == ".go":
            for m in _GO_IMPORT_RE.finditer(text):
                imp = m.group(1)
                for dkey, target_rel in go_dirs.items():
                    if dkey and dkey != "." and imp.endswith("/" + dkey) and target_rel != rel:
                        edges[rel].add(target_rel)
            for m in _GO_HTTP_RE.finditer(text):
                routes.append(
                    {
                        "method": "HTTP",
                        "path": m.group(1),
                        "file": rel,
                        "framework": "Go HTTP",
                    }
                )

    cycles = _find_cycles(edges)
    return {
        "files": sorted(rel_map.keys()),
        "edges": {k: sorted(v) for k, v in sorted(edges.items()) if v},
        "cycles": cycles,
        "routes": routes,
    }


def render_diagram_markdown(root: Path, target: str, data: dict[str, Any]) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    edges: dict[str, list[str]] = data["edges"]
    cycles: list[list[str]] = data["cycles"]
    routes: list[dict[str, str]] = data["routes"]
    files: list[str] = data["files"]

    mermaid_lines = ["```mermaid", "flowchart LR"]
    node_set: set[str] = set()
    edge_count = 0
    for src, targets in edges.items():
        node_set.add(src)
        for dst in targets:
            node_set.add(dst)
    for node in sorted(node_set)[:60]:
        label = node.replace('"', "'")
        mermaid_lines.append(f'    {_safe_node_id(node)}["{label}"]')
    for src, targets in edges.items():
        if src not in node_set:
            continue
        for dst in targets:
            if dst not in node_set:
                continue
            mermaid_lines.append(f"    {_safe_node_id(src)} --> {_safe_node_id(dst)}")
            edge_count += 1
            if edge_count >= 120:
                break
        if edge_count >= 120:
            break
    if edge_count == 0:
        mermaid_lines.append('    empty["No internal module import edges detected"]')
    mermaid_lines.append("```")

    cycle_lines = (
        ["- " + " -> ".join(f"`{n}`" for n in cyc) for cyc in cycles]
        if cycles
        else ["No circular dependencies detected."]
    )

    if routes:
        route_lines = [
            "| Method | Path | Framework | File |",
            "| --- | --- | --- | --- |",
            *[
                f"| `{r['method']}` | `{r['path']}` | {r['framework']} | `{r['file']}` |"
                for r in routes[:80]
            ],
        ]
    else:
        route_lines = ["_(No HTTP API endpoints detected.)_"]

    sections = [
        f"# Codebase Dependency Diagram & API Map — {root.name}",
        f"_Generated: {stamp} | Scope: `{target}` | Files scanned: {len(files)} | Import edges: {edge_count}_",
        "## 1. Module Dependency Graph\n" + "\n".join(mermaid_lines),
        "## 2. Circular Dependencies\n" + "\n".join(cycle_lines),
        "## 3. Detected HTTP API Routes\n" + "\n".join(route_lines),
    ]
    return "\n\n".join(sections).rstrip() + "\n"


def generate_diagram(h, args: list[str]) -> str:
    """Handle `/diagram [path] [--out docs/DIAGRAM.md]`."""
    target = "."
    out_arg = "docs/DIAGRAM.md"
    i = 0
    while i < len(args):
        if args[i] == "--out" and i + 1 < len(args):
            out_arg = args[i + 1]
            i += 2
        elif not args[i].startswith("-"):
            target = args[i]
            i += 1
        else:
            raise ValueError("Usage: /diagram [path] [--out docs/DIAGRAM.md]")

    with spinning("Building deterministic dependency graph & API route map"):
        data = analyze_structure(h.root, target)
        markdown = render_diagram_markdown(h.root, target, data)

    atomic_write(h.state / "DIAGRAM.md", markdown, mode=0o600)
    saved = h.state / "DIAGRAM.md"
    target_md = h.tools.path(out_arg)
    if h.approve(f"Write codebase diagram to {out_arg}?"):
        target_md.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target_md, markdown)
        saved = target_md
        record(h.state, "DIAGRAM", out_arg)

    if console.is_terminal:
        console.print(
            Panel(
                Markdown(markdown[:3500]),
                title="[bold cyan]Codebase Diagram & API Map[/]",
                border_style="cyan",
            )
        )

    return (
        f"Diagram saved to {saved} "
        f"({len(data['files'])} files, {sum(len(v) for v in data['edges'].values())} edges, "
        f"{len(data['cycles'])} cycle(s), {len(data['routes'])} route(s))."
    )
