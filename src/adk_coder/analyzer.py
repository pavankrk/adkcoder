"""Existing-application analysis to Markdown, interactive fix handoff, and feature addition."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from rich.markdown import Markdown
from rich.panel import Panel

from .audit import sources
from .changelog import record
from .fixer import fix
from .sbom import discover_components
from .scaffolder import resolve_requirements
from .scrubber import scrub
from .security import security_scan
from .spinner import spinning
from .tools import atomic_write
from .ui import console, pause_live

ANALYSIS_FILE = "ANALYSIS.md"


def _detect_stack(root: Path) -> list[str]:
    stacks: list[str] = []
    markers = (
        ("pom.xml", "Java (Maven)"),
        ("build.gradle", "Java/Kotlin (Gradle)"),
        ("build.gradle.kts", "Kotlin/Java (Gradle)"),
        ("pyproject.toml", "Python (pyproject)"),
        ("requirements.txt", "Python (pip)"),
        ("package.json", "Node.js / TypeScript"),
        ("go.mod", "Go"),
        ("Cargo.toml", "Rust"),
        ("Dockerfile", "Docker"),
    )
    for filename, label in markers:
        if (root / filename).is_file():
            stacks.append(label)
    return stacks or ["Unknown / polyglot"]


def _build_deterministic_markdown(
    root: Path,
    target: str,
    stacks: list[str],
    files: list[Path],
    report: dict,
    components: list[dict],
    ai_notes: str = "",
) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    rel_files = [p.relative_to(root).as_posix() for p in files[:40]]
    sec = report["security_findings"]
    solid = report["solid_findings"]
    findings = report["findings"]

    dep_lines = [
        f"- `{c['name']}` ({c['version']}, {c['ecosystem']}, license: {c['license']} [{c['license_policy']}])"
        for c in components[:25]
    ]
    if len(components) > 25:
        dep_lines.append(f"- ... and {len(components) - 25} more dependencies")

    finding_rows = [
        "| # | Severity | Location | Rule | Description |",
        "| --- | --- | --- | --- | --- |",
    ]
    for idx, item in enumerate(findings[:30], 1):
        msg = item["message"].replace("|", "\\|").replace("\n", " ")
        finding_rows.append(
            f"| {idx} | {item['severity']} | `{item['file']}:{item['line']}` | `{item['code']}` | {msg} |"
        )
    if len(findings) > 30:
        finding_rows.append(f"| ... | | | | +{len(findings) - 30} more findings in `.agent/audit_cache.json` |")

    sections = [
        f"# Application Analysis — {root.name}",
        f"_Generated: {stamp} | Scope: `{target}`_",
        "## 1. Tech Stack & Inventory",
        f"- **Detected Stack**: {', '.join(stacks)}",
        f"- **Source Files Inspected**: {len(files)}",
        f"- **Dependencies Discovered**: {len(components)}",
        f"- **Findings**: {len(sec)} security, {len(solid)} SOLID/coding standards ({len(findings)} total)",
        "### Key Files\n" + ("\n".join(f"- `{f}`" for f in rel_files) or "- _(no supported source files found)_"),
    ]
    if ai_notes.strip():
        sections.append("## 2. Architectural & Codebase Notes\n" + scrub(ai_notes.strip()))
    else:
        sections.append(
            "## 2. Architectural & Codebase Notes\n"
            "_AI narrative unavailable or skipped; deterministic diagnostics are captured below._"
        )
    sections.append(
        "## 3. Dependencies & Licenses\n"
        + ("\n".join(dep_lines) if dep_lines else "- _(no dependency manifests detected)_")
    )
    sections.append(
        "## 4. Detected Errors, Security & SOLID Findings\n"
        + ("\n".join(finding_rows) if findings else "No deterministic errors, security flaws, or SOLID violations found.")
    )
    sections.append(
        "## 5. Recommended Next Actions\n"
        + (
            "- Run `/fix all` (or `/fix <index>`) to repair detected issues with verification.\n"
            if findings
            else ""
        )
        + "- Run `/feature <description>` to plan and implement a new feature using this analysis.\n"
        + "- Run `/sbom` to export a full CycloneDX/SPDX Software Bill of Materials.\n"
    )
    return "\n\n".join(sections).rstrip() + "\n"


def _default_input(prompt_text: str) -> str:
    with pause_live():
        return input(prompt_text)


async def analyze(
    h,
    args: list[str],
    *,
    interactive: bool | None = None,
    ask_input=_default_input,
) -> str:
    """Analyze an existing application, write Markdown notes, offer fixes, and offer adding a feature."""
    target = "."
    out_path_arg = "docs/ANALYSIS.md"
    idx = 0
    while idx < len(args):
        if args[idx] == "--out" and idx + 1 < len(args):
            out_path_arg = args[idx + 1]
            idx += 2
        elif not args[idx].startswith("-"):
            target = args[idx]
            idx += 1
        else:
            raise ValueError("Usage: /analyze [path] [--out docs/ANALYSIS.md]")

    with spinning("Inspecting workspace & dependencies"):
        stacks = _detect_stack(h.root)
        files = sources(h, target)
        components = discover_components(h.root)
    report = security_scan(h, target)

    # Build bounded codebase overview payload for LLM narrative
    excerpts: dict[str, str] = {}
    for path in files[:12]:
        rel = path.relative_to(h.root).as_posix()
        try:
            text = path.read_text(encoding="utf8")
            excerpts[rel] = scrub("\n".join(text.splitlines()[:45])[:1600])
        except (OSError, UnicodeError):
            continue

    ai_notes = ""
    if files:
        payload = {
            "project": h.root.name,
            "stacks": stacks,
            "file_list": [p.relative_to(h.root).as_posix() for p in files[:50]],
            "excerpts": excerpts,
            "findings_summary": report["findings"][:15],
            "dependencies": [c["name"] for c in components[:30]],
        }
        try:
            with spinning("Synthesizing architectural analysis"):
                ai_notes = await h.ask(
                    "Analyze this existing codebase. Summarize its architecture (include a brief Mermaid flowchart), "
                    "main components, strengths, technical debt/risks, and extension points for adding new features. "
                    "Be concise and factual based only on the provided files.",
                    json.dumps(payload)[:40000],
                )
        except Exception as exc:  # noqa: BLE001
            ai_notes = f"_Architectural synthesis skipped ({type(exc).__name__})._"

    markdown = _build_deterministic_markdown(
        h.root, target, stacks, files, report, components, ai_notes=ai_notes
    )

    # Always store internal copy in .agent/ANALYSIS.md for `/feature` and memory.
    atomic_write(h.state / ANALYSIS_FILE, markdown, mode=0o600)

    # Also write workspace Markdown file (default docs/ANALYSIS.md) after confirmation.
    saved_md = h.state / ANALYSIS_FILE
    target_md = h.tools.path(out_path_arg)
    if h.approve(f"Write analysis report to {out_path_arg}?"):
        target_md.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target_md, markdown)
        saved_md = target_md
        record(h.state, "ANALYZE_DOC", out_path_arg)

    if console.is_terminal:
        console.print(Panel(Markdown(markdown[:3000]), title="[bold cyan]Application Analysis[/]", border_style="cyan"))

    findings = report["findings"]
    summary_parts = [
        f"Analysis saved to {saved_md} ({len(files)} files, {len(findings)} findings)."
    ]

    is_interactive = sys.stdin.isatty() if interactive is None else interactive
    if is_interactive:
        if findings:
            crit_high = sum(1 for f in findings if f["severity"] in {"CRITICAL", "HIGH"})
            if h.approve(
                f"Found {len(findings)} issue(s) ({crit_high} critical/high). Would you like to fix them now?"
            ):
                fix_result = await fix(h, "all")
                summary_parts.append(f"Fix result: {fix_result}")

        if h.approve("Would you like to add a new feature based on this analysis?"):
            feat_raw = ask_input(
                "Describe the new feature (or enter @file.md / path): "
            ).strip()
            if feat_raw:
                feat_result = await add_feature(h, [feat_raw])
                summary_parts.append(f"Feature result: {feat_result}")

    return "\n".join(summary_parts)


async def add_feature(h, args: list[str], *, ask_input=_default_input) -> str:
    """Plan and implement a new feature grounded in existing codebase analysis."""
    quick = "--quick" in args
    clean_args = [a for a in args if a != "--quick"]
    raw_req = " ".join(clean_args).strip()

    if not raw_req:
        if not sys.stdin.isatty() and ask_input is _default_input:
            raise ValueError("Usage: /feature <description|@file.md> [--quick]")
        raw_req = ask_input(
            "Describe the new feature to add (or enter @file.md): "
        ).strip()
    requirement = resolve_requirements(h, raw_req)
    if not requirement:
        raise ValueError("Feature description cannot be empty.")

    # Load existing analysis notes if present so the agent follows existing architecture.
    analysis_context = ""
    for candidate in (h.root / "docs/ANALYSIS.md", h.state / ANALYSIS_FILE):
        if candidate.is_file():
            analysis_context = candidate.read_text(encoding="utf8")[:8000]
            break

    # Record feature in TASKS.md
    first_line = requirement.splitlines()[0][:120]
    with (h.state / "TASKS.md").open("a", encoding="utf8") as stream:
        stream.write(f"- [ ] Feature: {scrub(first_line)}\n")

    enriched_request = (
        f"Add a new feature to the existing application while preserving its architecture, "
        f"SOLID boundaries, coding standards, and tests.\n\n"
        f"## Feature Requirement\n{requirement}\n"
    )
    if analysis_context:
        enriched_request += f"\n## Existing Application Analysis\n{analysis_context}\n"

    record(h.state, "FEATURE", first_line)
    if quick:
        return await h.chat(enriched_request)
    return await h.workflow(enriched_request)
