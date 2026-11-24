"""Antigravity MCP App (SEP-1865) server for ADK Coder Workbench.

Exposes:
  - UI resource: `ui://adk-coder/workbench.html` (`text/html;profile=mcp-app`)
  - Entrypoint tool: `open_workbench` (`aux_pane` + `global`, accepts `{}`)
  - Inline UI tools: `run_security_audit`, `generate_workspace_sbom`, `inspect_codebase_diagram`
  - App-only tools (`visibility: ["app"]`): snapshot, scan, SBOM, diagram, eager doc save,
    scaffold studio, and provider profile switching.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from . import __version__
from .config_ui import default_env_path
from .diagram import analyze_structure, render_diagram_markdown
from .harness import AgentHarness
from .profiles import activate_profile, masked_profiles_summary, set_fallback_profile
from .sbom import build_cyclonedx, build_spdx, discover_components
from .scaffolder import STACKS, scaffold
from .scrubber import scrub
from .security import security_scan
from .tools import atomic_write

APP_URI = "ui://adk-coder/workbench.html"
MIME_MCP_APP = "text/html;profile=mcp-app"

INLINE_META: dict[str, Any] = {
    "ui": {"resourceUri": APP_URI},
}

PANE_META: dict[str, Any] = {
    "ui": {"resourceUri": APP_URI},
    "com.google.antigravity/ui": {
        "entrypoints": [
            {"type": "aux_pane", "title": "ADK Coder Workbench"},
            {"type": "global", "title": "ADK Coder"},
        ]
    },
}

APP_ONLY_META: dict[str, Any] = {
    "ui": {"visibility": ["app"]},
}

ALLOWED_EDITABLE_DOCS = {
    "docs/PLAN.md",
    "docs/ARCHITECTURE.md",
    "docs/CODING_STANDARDS.md",
    ".agent/TASKS.md",
}


def _resolve_workspace(workspace: str = "") -> Path:
    raw = (workspace or "").strip() or os.environ.get("ADK_CODER_WORKSPACE") or os.getcwd()
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"Workspace directory not found: {raw}")
    return path


def _load_harness(workspace: str = "") -> AgentHarness:
    root = _resolve_workspace(workspace)
    return AgentHarness(root, approve=lambda _: True)


def load_workbench_html() -> str:
    """Read the bundled single-file MCP App HTML (or ADK_CODER_MCP_HTML env override)."""
    override = os.environ.get("ADK_CODER_MCP_HTML", "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return p.read_text(encoding="utf8")
    asset = Path(__file__).resolve().parent / "mcp_assets" / "workbench.html"
    if asset.is_file():
        return asset.read_text(encoding="utf8")
    return (
        "<!doctype html><html><body style='font-family:system-ui;padding:24px'>"
        "<h2>ADK Coder Workbench UI not built yet</h2>"
        "<p>Run <code>cd mcp_app_ui &amp;&amp; npm install &amp;&amp; npm run build</code>.</p>"
        "</body></html>"
    )


def _read_doc(root: Path, rel: str, limit: int = 24000) -> str:
    target = (root / rel).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return ""
    if not target.is_file():
        return ""
    try:
        return scrub(target.read_text(encoding="utf8", errors="replace"))[:limit]
    except OSError:
        return ""


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _collect_snapshot(workspace: str = "") -> dict[str, Any]:
    root = _resolve_workspace(workspace)
    state_dir = root / ".agent"

    docs = {
        "docs/PLAN.md": _read_doc(root, "docs/PLAN.md"),
        "docs/ARCHITECTURE.md": _read_doc(root, "docs/ARCHITECTURE.md"),
        "docs/CODING_STANDARDS.md": _read_doc(root, "docs/CODING_STANDARDS.md"),
        ".agent/TASKS.md": _read_doc(root, ".agent/TASKS.md"),
        "docs/DEBUG_REPORT.md": _read_doc(root, "docs/DEBUG_REPORT.md")
        or _read_doc(root, ".agent/DEBUG_REPORT.md"),
        "docs/PR_REVIEW.md": _read_doc(root, "docs/PR_REVIEW.md")
        or _read_doc(root, ".agent/PR_REVIEW.md"),
        "docs/ANALYSIS.md": _read_doc(root, "docs/ANALYSIS.md")
        or _read_doc(root, ".agent/ANALYSIS.md"),
    }

    adrs: list[dict[str, str]] = []
    adr_dir = root / "docs" / "adr"
    if adr_dir.is_dir():
        for p in sorted(adr_dir.glob("*.md"))[:12]:
            adrs.append(
                {
                    "path": p.relative_to(root).as_posix(),
                    "content": _read_doc(root, p.relative_to(root).as_posix(), limit=8000),
                }
            )

    sec_report = _read_json_file(state_dir / "security_report.json")
    sbom_data = _read_json_file(state_dir / "sbom.cdx.json") or _read_json_file(
        state_dir / "sbom.spdx.json"
    )
    diagram_data = analyze_structure(root, ".")
    profiles = masked_profiles_summary(default_env_path())

    return {
        "version": __version__,
        "workspace": str(root),
        "workspace_name": root.name,
        "docs": docs,
        "adrs": adrs,
        "security": sec_report,
        "sbom": _compact_sbom(sbom_data) if sbom_data else None,
        "diagram": {
            "files_count": len(diagram_data["files"]),
            "edges": dict(list(diagram_data["edges"].items())[:40]),
            "cycles": diagram_data["cycles"][:20],
            "routes": diagram_data["routes"][:80],
            "mermaid": render_diagram_markdown(root, ".", diagram_data)[:12000],
        },
        "profiles": profiles,
        "stacks": sorted(STACKS.keys()),
    }


def _compact_sbom(sbom: dict[str, Any]) -> dict[str, Any]:
    """Keep SBOM payload compact and well below the 1 MiB trajectory cap."""
    components = sbom.get("components") or sbom.get("packages") or []
    compact_rows: list[dict[str, str]] = []
    for item in components[:200]:
        if not isinstance(item, dict):
            continue
        lic_val = "UNKNOWN"
        if isinstance(item.get("licenses"), list) and item["licenses"]:
            first = item["licenses"][0]
            if isinstance(first, dict):
                lic_obj = first.get("license") or first
                lic_val = str(lic_obj.get("id") or lic_obj.get("name") or "UNKNOWN")
        elif item.get("licenseConcluded"):
            lic_val = str(item["licenseConcluded"])
        elif item.get("license"):
            lic_val = str(item["license"])
        compact_rows.append(
            {
                "name": str(item.get("name") or ""),
                "version": str(item.get("version") or item.get("versionInfo") or ""),
                "purl": str(item.get("purl") or ""),
                "license": lic_val,
                "policy": str(item.get("license_policy") or _policy_for_license(lic_val)),
            }
        )
    return {
        "format": str(sbom.get("bomFormat") or sbom.get("spdxVersion") or "CycloneDX"),
        "total": len(components),
        "components": compact_rows,
    }


def _policy_for_license(lic: str) -> str:
    upper = (lic or "").upper()
    if any(k in upper for k in ("MIT", "APACHE", "BSD", "ISC", "PSF", "0BSD", "UNLICENSE")):
        return "PERMISSIVE"
    if any(k in upper for k in ("LGPL", "MPL", "EPL")):
        return "WEAK_COPYLEFT"
    if any(k in upper for k in ("GPL", "AGPL")):
        return "STRONG_COPYLEFT"
    return "UNKNOWN"


def _tool_result(text: str, structured: dict[str, Any]):
    from mcp import types

    fields = set(getattr(types.CallToolResult, "model_fields", {}).keys())
    kwargs: dict[str, Any] = {
        "content": [types.TextContent(type="text", text=scrub(text))],
    }
    if "structured_content" in fields:
        kwargs["structured_content"] = structured
    elif "structuredContent" in fields:
        kwargs["structuredContent"] = structured
    return types.CallToolResult(**kwargs)


def create_mcp_server():
    """Construct and configure the ADK Coder Workbench MCP Server (supports mcp 2.x & 1.x)."""
    try:
        from mcp.server.mcpserver import MCPServer as ServerCls
    except ImportError:
        from mcp.server.fastmcp import FastMCP as ServerCls  # type: ignore[no-redef]

    server = ServerCls(
        name="adk_coder_workbench",
        instructions=(
            "ADK Coder Workbench MCP App: security/SOLID auditing, CycloneDX/SPDX SBOM, "
            "deterministic architecture & API diagrams, SDLC planning docs, provider profiles, "
            "and enterprise project scaffolding."
        ),
    )

    @server.resource(
        APP_URI,
        name="ADK Coder Workbench UI",
        mime_type=MIME_MCP_APP,
        meta={"ui": {"prefersBorder": False}},
    )
    def workbench_ui_resource() -> str:
        return load_workbench_html()

    @server.tool(
        name="open_workbench",
        title="ADK Coder Workbench",
        meta=PANE_META,
    )
    def open_workbench(workspace: str = "", tab: str = "overview"):
        """Open the interactive ADK Coder Workbench side pane / full-page dashboard."""
        snap = _collect_snapshot(workspace)
        snap["initial_tab"] = tab or "overview"
        summary = (
            f"Opened ADK Coder Workbench for `{snap['workspace_name']}` "
            f"({snap['diagram']['files_count']} source files, "
            f"{len(snap['diagram']['routes'])} API routes, "
            f"{len(snap['diagram']['cycles'])} cycles)."
        )
        return _tool_result(summary, snap)

    @server.tool(
        name="run_security_audit",
        title="Run Security & SOLID Audit",
        meta=INLINE_META,
    )
    def run_security_audit(workspace: str = "", target: str = "."):
        """Run offline SAST, secret scanning, and SOLID complexity analysis on a workspace."""
        h = _load_harness(workspace)
        report = security_scan(h, target or ".")
        payload = {
            "initial_tab": "security",
            "workspace": str(h.root),
            "workspace_name": h.root.name,
            "security": {
                "files_scanned": report["files_scanned"],
                "summary": report.get("summary", {}),
                "findings": report["findings"][:120],
                "solid_metrics": report.get("solid_metrics", {}),
                "notes": report.get("notes", []),
            },
        }
        text = (
            f"Security & SOLID scan on `{h.root.name}` ({report['files_scanned']} files): "
            f"{len(report['security_findings'])} security, "
            f"{len(report['solid_findings'])} SOLID findings."
        )
        return _tool_result(text, payload)

    @server.tool(
        name="generate_workspace_sbom",
        title="Generate Workspace SBOM",
        meta=INLINE_META,
    )
    def generate_workspace_sbom(workspace: str = "", fmt: str = "cyclonedx"):
        """Generate a CycloneDX 1.5 or SPDX 2.3 Software Bill of Materials and license breakdown."""
        root = _resolve_workspace(workspace)
        clean_fmt = "spdx" if str(fmt).lower() == "spdx" else "cyclonedx"
        components = discover_components(root)
        doc = build_spdx(root, components) if clean_fmt == "spdx" else build_cyclonedx(root, components)
        policy: dict[str, int] = {}
        for comp in components:
            pol = str(comp.get("license_policy") or "UNKNOWN")
            policy[pol] = policy.get(pol, 0) + 1
        out_name = "sbom.spdx.json" if clean_fmt == "spdx" else "sbom.cdx.json"
        state_dir = root / ".agent"
        state_dir.mkdir(mode=0o700, exist_ok=True)
        atomic_write(state_dir / out_name, json.dumps(doc, indent=2), mode=0o600)
        compact = _compact_sbom(doc)
        compact["policy"] = policy
        payload = {
            "initial_tab": "sbom",
            "workspace": str(root),
            "workspace_name": root.name,
            "sbom": compact,
        }
        text = (
            f"Generated {clean_fmt.upper()} SBOM for `{root.name}`: "
            f"{compact['total']} component(s), policy={policy}."
        )
        return _tool_result(text, payload)

    @server.tool(
        name="inspect_codebase_diagram",
        title="Inspect Architecture & API Map",
        meta=INLINE_META,
    )
    def inspect_codebase_diagram(workspace: str = "", target: str = "."):
        """Build a deterministic Mermaid dependency graph, cycle check, and HTTP API route table."""
        root = _resolve_workspace(workspace)
        data = analyze_structure(root, target or ".")
        md = render_diagram_markdown(root, target or ".", data)
        payload = {
            "initial_tab": "diagram",
            "workspace": str(root),
            "workspace_name": root.name,
            "diagram": {
                "files_count": len(data["files"]),
                "edges": dict(list(data["edges"].items())[:50]),
                "cycles": data["cycles"][:25],
                "routes": data["routes"][:100],
                "mermaid": md[:14000],
            },
        }
        text = (
            f"Diagram for `{root.name}`: {len(data['files'])} files, "
            f"{sum(len(v) for v in data['edges'].values())} import edges, "
            f"{len(data['cycles'])} cycle(s), {len(data['routes'])} route(s)."
        )
        return _tool_result(text, payload)

    # ---------------- App-Only Tools (visibility: ["app"]) ----------------

    @server.tool(name="app_get_workspace_snapshot", meta=APP_ONLY_META)
    def app_get_workspace_snapshot(workspace: str = ""):
        snap = _collect_snapshot(workspace)
        return _tool_result("Snapshot loaded.", snap)

    @server.tool(name="app_run_security", meta=APP_ONLY_META)
    def app_run_security(workspace: str = "", target: str = "."):
        return run_security_audit(workspace=workspace, target=target)

    @server.tool(name="app_run_sbom", meta=APP_ONLY_META)
    def app_run_sbom(workspace: str = "", fmt: str = "cyclonedx"):
        return generate_workspace_sbom(workspace=workspace, fmt=fmt)

    @server.tool(name="app_run_diagram", meta=APP_ONLY_META)
    def app_run_diagram(workspace: str = "", target: str = "."):
        return inspect_codebase_diagram(workspace=workspace, target=target)

    @server.tool(name="app_save_doc", meta=APP_ONLY_META)
    def app_save_doc(workspace: str = "", rel_path: str = "", content: str = ""):
        root = _resolve_workspace(workspace)
        norm = rel_path.replace("\\", "/").strip().lstrip("./")
        if norm not in ALLOWED_EDITABLE_DOCS:
            raise ValueError(
                f"Document not writable via Workbench: {rel_path}. "
                f"Allowed: {', '.join(sorted(ALLOWED_EDITABLE_DOCS))}"
            )
        target = (root / norm).resolve()
        target.relative_to(root.resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target, content)
        return _tool_result(f"Saved {norm}.", {"ok": True, "path": norm, "bytes": len(content)})

    @server.tool(name="app_scaffold_project", meta=APP_ONLY_META)
    def app_scaffold_project(
        workspace: str = "",
        template: str = "fastapi-service",
        name: str = "",
        parent: str = "",
        minimal: bool = False,
    ):
        if not name.strip():
            raise ValueError("Project name is required.")
        h = _load_harness(workspace)
        cli_args = [template.strip(), name.strip()]
        if parent.strip():
            cli_args.append(parent.strip())
        if minimal:
            cli_args.append("--minimal")
        msg = scaffold(h, cli_args)
        return _tool_result(msg, {"ok": True, "message": msg, "template": template, "name": name})

    @server.tool(name="app_use_profile", meta=APP_ONLY_META)
    def app_use_profile(name: str = "", workspace: str = ""):
        ws = _resolve_workspace(workspace) if workspace.strip() else None
        res = activate_profile(name, env_path=default_env_path(), workspace=ws)
        return _tool_result(
            f"Activated profile {res['name']}.",
            {
                "ok": True,
                "active": res["name"],
                "profiles": masked_profiles_summary(default_env_path()),
            },
        )

    @server.tool(name="app_set_fallback_profile", meta=APP_ONLY_META)
    def app_set_fallback_profile(name: str = ""):
        store = set_fallback_profile(name, env_path=default_env_path())
        return _tool_result(
            f"Fallback profile updated to {store['fallback'] or 'none'}.",
            {
                "ok": True,
                "fallback": store["fallback"],
                "profiles": masked_profiles_summary(default_env_path()),
            },
        )

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="adk-coder-mcp",
        description="Run the ADK Coder Workbench Antigravity MCP App server (stdio).",
    )
    parser.add_argument("--data-dir", "--data_dir", dest="data_dir", default="", help="Optional plugin data dir.")
    parser.add_argument("--workspace", default="", help="Default workspace root.")
    parser.add_argument("--html", default="", help="Override workbench HTML path for live UI iteration.")
    args = parser.parse_args(argv)

    if args.workspace:
        os.environ["ADK_CODER_WORKSPACE"] = str(Path(args.workspace).expanduser().resolve())
    if args.html:
        os.environ["ADK_CODER_MCP_HTML"] = str(Path(args.html).expanduser().resolve())

    server = create_mcp_server()
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
