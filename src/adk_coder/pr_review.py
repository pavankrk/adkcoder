"""Branch / PR Code Reviewer combining offline SAST/SOLID checks with structured LLM review."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel

from .audit import CACHE, response_json
from .changelog import record
from .scrubber import scrub
from .security import security_scan
from .spinner import spinning
from .tools import atomic_write
from .ui import console

PR_REVIEW_FILE = "PR_REVIEW.md"


def _detect_base_branch(h) -> str:
    for cand in ("main", "master", "origin/main", "origin/master"):
        try:
            h.git.run("rev-parse", "--verify", "--quiet", cand)
            return cand
        except ValueError:
            continue
    return "HEAD"


def _collect_pr_diff(h, base_branch: str | None) -> tuple[str, list[str], str]:
    """Return (base_label, changed_files, diff_text)."""
    base = base_branch or _detect_base_branch(h)
    try:
        current = h.git.run("branch", "--show-current").strip()
    except ValueError:
        current = ""

    diff_text = ""
    changed: list[str] = []
    label = f"{base}...HEAD"

    if base != "HEAD" and base != current:
        try:
            changed = [
                ln.strip()
                for ln in h.git.run("diff", "--name-only", f"{base}...HEAD").splitlines()
                if ln.strip()
            ]
            diff_text = h.git.run("diff", f"{base}...HEAD")
        except ValueError:
            changed, diff_text = [], ""

    # Fallback to uncommitted working-tree diff when on base branch or no branch commits yet
    if not diff_text.strip():
        label = "working-tree (staged + unstaged)"
        try:
            changed = [
                ln.strip()
                for ln in h.git.run("diff", "--name-only", "HEAD").splitlines()
                if ln.strip()
            ]
            diff_text = h.git.run("diff", "HEAD")
        except ValueError:
            changed = [
                ln.strip()
                for ln in h.git.run("diff", "--name-only").splitlines()
                if ln.strip()
            ]
            diff_text = h.git.run("diff")

    return label, sorted(set(changed)), scrub(diff_text)


def _format_pr_review_markdown(
    root: Path,
    base_label: str,
    changed_files: list[str],
    offline_findings: list[dict[str, Any]],
    review: dict[str, Any],
) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    risk = str(review.get("risk_level") or "LOW").upper()
    summary = scrub(str(review.get("pr_summary") or "No summary generated.")).strip()
    ai_findings = review.get("findings") if isinstance(review.get("findings"), list) else []
    test_gaps = review.get("test_gaps") if isinstance(review.get("test_gaps"), list) else []

    all_rows = [
        "| # | Severity | Category | Location | Issue | Suggestion |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    idx = 0
    for f in offline_findings[:25]:
        idx += 1
        msg = str(f.get("message", "")).replace("|", "\\|")
        all_rows.append(
            f"| {idx} | {f.get('severity', 'MEDIUM')} | Offline-SAST | `{f.get('file')}:{f.get('line', 1)}` | {msg} | Run `/fix {idx}` |"
        )
    for f in ai_findings[:25]:
        if not isinstance(f, dict):
            continue
        idx += 1
        sev = str(f.get("severity") or "MEDIUM").upper()
        cat = scrub(str(f.get("category") or "Correctness")).replace("|", "\\|")
        loc = f"`{f.get('file', '?')}:{f.get('line', 1)}`"
        msg = scrub(str(f.get("message") or "")).replace("|", "\\|")
        sug = scrub(str(f.get("suggestion") or "")).replace("|", "\\|")
        all_rows.append(f"| {idx} | {sev} | {cat} | {loc} | {msg} | {sug} |")

    gap_lines = (
        [f"- {scrub(str(g))}" for g in test_gaps[:15]]
        if test_gaps
        else ["- No obvious missing test scenarios identified."]
    )

    sections = [
        f"# PR Code Review — {root.name}",
        f"_Generated: {stamp} | Diff Scope: `{base_label}` | Changed Files: {len(changed_files)} | Risk: **{risk}**_",
        "## 1. PR Summary\n" + summary,
        "## 2. Changed Files\n" + ("\n".join(f"- `{f}`" for f in changed_files[:40]) or "- _(none)_"),
        "## 3. Review Findings\n"
        + ("\n".join(all_rows) if idx > 0 else "No blocking issues detected."),
        "## 4. Test Coverage Gaps\n" + "\n".join(gap_lines),
        "## 5. Next Actions\n"
        + ("- Run `/fix all` or `/fix <index>` to patch reported issues.\n" if idx > 0 else "")
        + "- Run `/testgen <file>` to generate missing unit tests for changed files.\n",
    ]
    return "\n\n".join(sections).rstrip() + "\n"


def _post_gh_comment(root: Path, body: str) -> str:
    if not shutil.which("gh"):
        return "Skipped `--post`: GitHub CLI (`gh`) not found on PATH."
    proc = subprocess.run(
        ["gh", "pr", "comment", "--body", body[:60000]],
        cwd=root,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
        check=False,
    )
    if proc.returncode == 0:
        return "Posted PR review comment via `gh pr comment`."
    return f"Failed to post PR comment: {scrub((proc.stderr or proc.stdout).strip())[:300]}"


async def pr_review(h, args: list[str]) -> str:
    """Handle `/pr-review [base-branch] [--out docs/PR_REVIEW.md] [--post]`."""
    base_branch: str | None = None
    out_arg = "docs/PR_REVIEW.md"
    post = False

    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--out" and i + 1 < len(args):
            out_arg = args[i + 1]
            i += 2
        elif tok == "--post":
            post = True
            i += 1
        elif tok.startswith("--"):
            raise ValueError("Usage: /pr-review [base-branch] [--out docs/PR_REVIEW.md] [--post]")
        else:
            base_branch = tok
            i += 1

    base_label, changed_files, diff_text = _collect_pr_diff(h, base_branch)
    if not diff_text.strip():
        return f"No changes found to review ({base_label})."

    # Offline SAST/SOLID scan scoped only to changed files
    scan_report = security_scan(h, ".")
    changed_set = set(changed_files)
    offline_findings = [
        f for f in scan_report.get("findings", []) if f.get("file") in changed_set
    ]

    payload = {
        "scope": base_label,
        "changed_files": changed_files[:40],
        "offline_findings": offline_findings[:20],
        "diff": diff_text[:32000],
    }

    review_data: dict[str, Any] = {}
    with spinning("PR Review: inspecting diff for bugs, security, SOLID & test gaps"):
        raw = await h.ask(
            "You are a Staff Code Reviewer sub-agent reviewing a pull request diff. "
            "Evaluate Correctness/Bugs, Security, Maintainability/SOLID, and Test Gaps. "
            "Return ONLY JSON:\n"
            '{"risk_level": "LOW|MEDIUM|HIGH|CRITICAL", '
            '"pr_summary": "concise 2-4 line summary suitable for a PR description", '
            '"findings": [{"severity": "CRITICAL|HIGH|MEDIUM|LOW", "category": "Correctness|Security|SOLID|Performance", '
            '"file": "relative/path", "line": 1, "message": "concise defect", "suggestion": "concrete fix"}], '
            '"test_gaps": ["missing test scenario"]}.',
            json.dumps(payload)[:36000],
        )
    try:
        parsed = response_json(raw)
        if isinstance(parsed, dict):
            review_data = parsed
    except ValueError:
        review_data = {"risk_level": "MEDIUM", "pr_summary": scrub(raw)[:1500], "findings": [], "test_gaps": []}

    # Sync findings into .agent/audit_cache.json so `/fix` can immediately repair them
    combined_for_cache: list[dict[str, Any]] = list(offline_findings)
    for item in review_data.get("findings", []) or []:
        if not isinstance(item, dict) or not item.get("file"):
            continue
        rel = str(item["file"]).strip()
        if (h.root / rel).is_file():
            combined_for_cache.append(
                {
                    "file": rel,
                    "line": int(item["line"]) if str(item.get("line", "")).isdecimal() else 1,
                    "severity": str(item.get("severity") or "MEDIUM").upper(),
                    "code": f"PR-{str(item.get('category') or 'REVIEW').upper()}",
                    "message": str(item.get("message") or ""),
                }
            )
    if combined_for_cache:
        atomic_write(
            h.state / CACHE,
            json.dumps({"target": base_label, "findings": combined_for_cache}, indent=2),
            mode=0o600,
        )

    markdown = _format_pr_review_markdown(
        h.root, base_label, changed_files, offline_findings, review_data
    )
    atomic_write(h.state / PR_REVIEW_FILE, markdown, mode=0o600)
    saved = h.state / PR_REVIEW_FILE

    target_md = h.tools.path(out_arg)
    if h.approve(f"Write PR review report to {out_arg}?"):
        target_md.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target_md, markdown)
        saved = target_md
        record(h.state, "PR_REVIEW", out_arg)

    if console.is_terminal:
        console.print(
            Panel(
                Markdown(markdown[:3500]),
                title="[bold cyan]PR Code Review[/]",
                border_style="cyan",
            )
        )

    post_note = ""
    if post and h.approve("Post this review comment to the current GitHub PR via `gh pr comment`?"):
        post_note = "\n" + _post_gh_comment(h.root, markdown)

    total_issues = len(offline_findings) + len(review_data.get("findings") or [])
    return (
        f"PR review saved to {saved} "
        f"({len(changed_files)} file(s), {total_issues} finding(s), risk: {str(review_data.get('risk_level') or 'LOW').upper()})."
        f"{post_note}"
    )
