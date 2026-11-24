"""Deterministic security (SAST, secrets, SCA) and SOLID/coding standards scanner."""

from __future__ import annotations

import ast
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .audit import CACHE, LEVELS, finding, sources
from .scrubber import scrub
from .spinner import spinning
from .tools import atomic_write
from .ui import console

REPORT = "security_report.json"

SECRET_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("SEC-SECRET-PEM", "CRITICAL", re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----")),
    ("SEC-SECRET-AWS", "CRITICAL", re.compile(r"\bAKIA[A-Z0-9]{16}\b")),
    ("SEC-SECRET-GH", "CRITICAL", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("SEC-SECRET-KEY", "HIGH", re.compile(r"\b(?:AIza[\w-]{30,}|sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[\w-]{10,})\b")),
    (
        "SEC-SECRET-DBURL",
        "HIGH",
        re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s/@]+:[^\s/@]+@"),
    ),
)

CONCRETE_INFRA_IMPORTS = {
    "requests",
    "httpx",
    "urllib",
    "aiohttp",
    "sqlalchemy",
    "psycopg",
    "psycopg2",
    "pymongo",
    "redis",
    "fastapi",
    "flask",
    "django",
    "subprocess",
    "sqlite3",
    "boto3",
}

SQL_KEYWORDS = re.compile(r"\b(?:SELECT|INSERT|UPDATE|DELETE|DROP|ALTER)\b", re.IGNORECASE)


def _cyclomatic_complexity(node: ast.AST) -> int:
    score = 1
    for child in ast.walk(node):
        if isinstance(
            child,
            (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.With, ast.AsyncWith, ast.IfExp),
        ):
            score += 1
        elif isinstance(child, ast.BoolOp):
            score += max(0, len(child.values) - 1)
        elif isinstance(child, ast.comprehension):
            score += 1 + len(child.ifs)
    return score


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _analyze_python_ast(rel: str, source: str) -> tuple[list[dict], list[dict]]:
    sec_issues: list[dict] = []
    solid_issues: list[dict] = []
    lines = source.splitlines()

    if len(lines) > 400:
        solid_issues.append(
            finding(
                rel,
                1,
                "LOW",
                "SOLID-SRP-FILE",
                f"Module has {len(lines)} lines (>400). Consider splitting responsibilities.",
            )
        )

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        sec_issues.append(finding(rel, exc.lineno or 1, "HIGH", "SYNTAX", exc.msg))
        return sec_issues, solid_issues

    in_domain = "domain" in Path(rel).parts

    for node in ast.walk(tree):
        # DIP check: concrete infra imports inside domain/ layer
        if in_domain and isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = (
                [alias.name.split(".", 1)[0] for alias in node.names]
                if isinstance(node, ast.Import)
                else [(node.module or "").split(".", 1)[0]]
            )
            for mod in mods:
                if mod in CONCRETE_INFRA_IMPORTS:
                    solid_issues.append(
                        finding(
                            rel,
                            node.lineno,
                            "MEDIUM",
                            "SOLID-DIP",
                            f"Domain module imports concrete infrastructure `{mod}`. Depend on a port/interface instead.",
                        )
                    )

        # SOLID checks on classes and functions
        if isinstance(node, ast.ClassDef):
            methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            if len(methods) > 12:
                solid_issues.append(
                    finding(
                        rel,
                        node.lineno,
                        "MEDIUM",
                        "SOLID-SRP-CLASS",
                        f"Class `{node.name}` defines {len(methods)} methods (>12). Split into smaller focused collaborators.",
                    )
                )

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end_ln = getattr(node, "end_lineno", node.lineno) or node.lineno
            length = end_ln - node.lineno + 1
            if length > 60:
                solid_issues.append(
                    finding(
                        rel,
                        node.lineno,
                        "LOW",
                        "SOLID-SRP-FUNC",
                        f"Function `{node.name}` spans {length} lines (>60). Extract helper functions.",
                    )
                )
            arg_count = len(node.args.posonlyargs) + len(node.args.args) + len(node.args.kwonlyargs)
            if node.args.args and node.args.args[0].arg in {"self", "cls"}:
                arg_count -= 1
            if arg_count > 6:
                solid_issues.append(
                    finding(
                        rel,
                        node.lineno,
                        "MEDIUM",
                        "SOLID-ISP-ARGS",
                        f"Function `{node.name}` takes {arg_count} parameters (>6). Group related inputs into a parameter object.",
                    )
                )
            complexity = _cyclomatic_complexity(node)
            if complexity > 10:
                solid_issues.append(
                    finding(
                        rel,
                        node.lineno,
                        "MEDIUM",
                        "SOLID-COMPLEXITY",
                        f"Function `{node.name}` has cyclomatic complexity {complexity} (>10). Simplify branching or use polymorphism.",
                    )
                )

        # SAST checks on function calls
        if isinstance(node, ast.Call):
            callee = _dotted_name(node.func)
            if callee in {"eval", "exec"}:
                sec_issues.append(
                    finding(
                        rel,
                        node.lineno,
                        "CRITICAL",
                        "SAST-EXEC",
                        f"Dynamic code execution via `{callee}()`. Replace with safe parsing (e.g. `ast.literal_eval`).",
                    )
                )
            elif callee in {"pickle.loads", "pickle.load", "marshal.loads"}:
                sec_issues.append(
                    finding(
                        rel,
                        node.lineno,
                        "CRITICAL",
                        "SAST-DESERIALIZE",
                        f"Unsafe deserialization via `{callee}()`. Use JSON or schema-validated formats.",
                    )
                )
            elif callee == "yaml.load":
                has_safe = any(
                    kw.arg == "Loader" and "Safe" in _dotted_name(kw.value)
                    for kw in node.keywords
                )
                if not has_safe:
                    sec_issues.append(
                        finding(
                            rel,
                            node.lineno,
                            "HIGH",
                            "SAST-YAML",
                            "`yaml.load()` without SafeLoader allows arbitrary object instantiation. Use `yaml.safe_load()`.",
                        )
                    )
            elif callee.startswith("subprocess.") and any(
                kw.arg == "shell"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in node.keywords
            ):
                sec_issues.append(
                    finding(
                        rel,
                        node.lineno,
                        "CRITICAL",
                        "SAST-SHELL",
                        f"`{callee}(..., shell=True)` risks command injection. Pass an argv list with `shell=False`.",
                    )
                )
            elif callee in {"hashlib.md5", "hashlib.sha1"}:
                sec_issues.append(
                    finding(
                        rel,
                        node.lineno,
                        "MEDIUM",
                        "SAST-WEAK-HASH",
                        f"Weak digest `{callee}`. Prefer SHA-256 or stronger (or pass `usedforsecurity=False`).",
                    )
                )
            elif callee.endswith((".execute", ".executemany")) and node.args:
                first_arg = node.args[0]
                if isinstance(first_arg, ast.JoinedStr):
                    literal_parts = " ".join(
                        v.value
                        for v in first_arg.values
                        if isinstance(v, ast.Constant) and isinstance(v.value, str)
                    )
                    if SQL_KEYWORDS.search(literal_parts):
                        sec_issues.append(
                            finding(
                                rel,
                                node.lineno,
                                "CRITICAL",
                                "SAST-SQLI",
                                "SQL query constructed with f-string interpolation. Use parameterized queries instead.",
                            )
                        )

        # Hardcoded 0.0.0.0 bind check
        if isinstance(node, ast.Constant) and node.value == "0.0.0.0":
            sec_issues.append(
                finding(
                    rel,
                    node.lineno,
                    "LOW",
                    "SAST-BIND-ALL",
                    "Hardcoded `0.0.0.0` bind address. Prefer configurable host defaulting to `127.0.0.1`.",
                )
            )

    return sec_issues, solid_issues


def _scan_secrets(rel: str, source: str) -> list[dict]:
    hits: list[dict] = []
    for lineno, line in enumerate(source.splitlines(), 1):
        for code, severity, pattern in SECRET_RULES:
            if pattern.search(line):
                hits.append(
                    finding(
                        rel,
                        lineno,
                        severity,
                        code,
                        "Potential hardcoded credential detected. Move secrets to environment variables or a secret manager.",
                    )
                )
                break
    return hits


def _dedupe(items: list[dict]) -> list[dict]:
    seen: set[tuple[str, int, str]] = set()
    out: list[dict] = []
    for item in items:
        key = (item["file"], int(item["line"]), str(item["code"]))
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def security_scan(h, target: str = ".") -> dict:
    with spinning("Scanning security & SOLID standards"):
        paths = sources(h, target)
        sec_findings: list[dict] = []
        solid_findings: list[dict] = []
        notes: list[str] = []

        for path in paths:
            rel = path.relative_to(h.root).as_posix()
            try:
                if path.stat().st_size > 512_000:
                    continue
                source = path.read_text(encoding="utf8")
            except (OSError, UnicodeError):
                continue
            sec_findings.extend(_scan_secrets(rel, source))
            if path.suffix == ".py":
                s_hits, solid_hits = _analyze_python_ast(rel, source)
                sec_findings.extend(s_hits)
                solid_findings.extend(solid_hits)
            elif len(source.splitlines()) > 400:
                solid_findings.append(
                    finding(
                        rel,
                        1,
                        "LOW",
                        "SOLID-SRP-FILE",
                        f"Module has {len(source.splitlines())} lines (>400). Consider splitting responsibilities.",
                    )
                )

    # Optional external dependency CVE audits (SCA) when available
    if (h.root / "pyproject.toml").is_file() or list(h.root.glob("requirements*.txt")):
        if shutil.which("pip-audit"):
            code, stdout, _ = h.tools.run_process(
                ["pip-audit", "--format", "json"], scope="security:pip-audit"
            )
            if code in (0, 1) and stdout.strip():
                try:
                    data = json.loads(stdout)
                    for dep in data.get("dependencies", []):
                        for vuln in dep.get("vulns", []):
                            sec_findings.append(
                                finding(
                                    "pyproject.toml",
                                    1,
                                    "HIGH",
                                    f"CVE-{vuln.get('id', 'VULN')}",
                                    f"{dep.get('name')}=={dep.get('version')}: {vuln.get('description', 'Known vulnerability')[:140]}",
                                )
                            )
                except (ValueError, KeyError, TypeError):
                    notes.append("pip-audit returned unreadable output.")
        else:
            notes.append("pip-audit not installed (skipping Python CVE lookup).")

    if (h.root / "package-lock.json").is_file():
        if shutil.which("npm"):
            code, stdout, _ = h.tools.run_process(
                ["npm", "audit", "--json"], scope="security:npm-audit"
            )
            if code in (0, 1) and stdout.strip():
                try:
                    data = json.loads(stdout)
                    for pkg_name, vuln in (data.get("vulnerabilities") or {}).items():
                        sev = str(vuln.get("severity", "medium")).upper()
                        mapped = sev if sev in LEVELS else "MEDIUM"
                        sec_findings.append(
                            finding(
                                "package-lock.json",
                                1,
                                mapped,
                                "SCA-NPM",
                                f"Vulnerable npm package `{pkg_name}` ({sev}). Run `npm audit fix`.",
                            )
                        )
                except (ValueError, KeyError, TypeError):
                    notes.append("npm audit returned unreadable output.")
        else:
            notes.append("npm not installed (skipping Node CVE lookup).")

    if (h.root / "go.mod").is_file() and not shutil.which("govulncheck"):
        notes.append("govulncheck not installed (skipping Go CVE lookup).")

    sec_findings = _dedupe(sec_findings)
    solid_findings = _dedupe(solid_findings)
    all_findings = sec_findings + solid_findings
    all_findings.sort(key=lambda f: (LEVELS.index(f["severity"]), f["file"], f["line"]))

    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "target": target,
        "files_scanned": len(paths),
        "security_findings": sec_findings,
        "solid_findings": solid_findings,
        "findings": all_findings,
        "notes": notes,
    }
    atomic_write(h.state / REPORT, json.dumps(report, indent=2) + "\n", mode=0o600)
    # Also update audit_cache.json so `/fix` can repair findings directly.
    atomic_write(
        h.state / CACHE,
        json.dumps(
            {"timestamp": report["timestamp"], "target": target, "findings": all_findings},
            indent=2,
        )
        + "\n",
        mode=0o600,
    )

    table = Table(title="Security (SAST/Secrets/SCA) & SOLID Standards Scan", border_style="cyan")
    for col in ("#", "Severity", "Location", "Rule", "Finding & Remediation"):
        table.add_column(col)
    for idx, row in enumerate(all_findings, 1):
        table.add_row(
            str(idx),
            Text(
                row["severity"],
                style=("bold red", "red", "yellow", "dim")[LEVELS.index(row["severity"])],
            ),
            Text(f"{row['file']}:{row['line']}"),
            Text(row["code"]),
            Text(scrub(row["message"])),
        )
    summary = (
        f"Scanned {len(paths)} files: {len(sec_findings)} security findings, "
        f"{len(solid_findings)} SOLID/standards findings."
    )
    if console.is_terminal:
        console.print(table)
        console.print(Panel("\n".join([summary, *notes]), border_style="dim"))
    return report
