"""Tests for the Antigravity MCP App (SEP-1865) server, Workbench UI bundle, plugin installer, and mcp-app scaffold."""

from __future__ import annotations

import asyncio
import json

from adk_coder.harness import AgentHarness
from adk_coder.mcp_plugin import PLUGIN_NAME, install_mcp_plugin
from adk_coder.mcp_server import (
    APP_ONLY_META,
    APP_URI,
    INLINE_META,
    MIME_MCP_APP,
    PANE_META,
    create_mcp_server,
    load_workbench_html,
)


def test_workbench_html_bundle_exists_and_uses_mcp_apps_sdk():
    html = load_workbench_html()
    assert len(html) > 20_000
    assert len(html.encode("utf8")) < 10 * 1024 * 1024
    assert "ADK Coder Workbench" in html
    assert "ui/initialize" in html or "ext-apps" in html or "workbench" in html.lower()


def test_mcp_server_registers_sep1865_resource_entrypoints_and_tools(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/PLAN.md").write_text("# Plan\nInitial plan\n", encoding="utf8")
    (tmp_path / "app.py").write_text(
        'import os\n@app.get("/ping")\ndef ping(): return eval("1")\n',
        encoding="utf8",
    )

    srv = create_mcp_server()

    # Verify SEP-1865 metadata contracts
    assert APP_URI.startswith("ui://")
    assert MIME_MCP_APP == "text/html;profile=mcp-app"
    assert PANE_META["ui"]["resourceUri"] == APP_URI
    entrypoints = PANE_META["com.google.antigravity/ui"]["entrypoints"]
    assert {e["type"] for e in entrypoints} == {"aux_pane", "global"}
    assert INLINE_META["ui"]["resourceUri"] == APP_URI
    assert APP_ONLY_META == {"ui": {"visibility": ["app"]}}

    # List tools & resources via MCPServer async APIs
    tools = asyncio.run(srv.list_tools())
    tool_map = {t.name: t for t in tools}
    assert "open_workbench" in tool_map
    assert "run_security_audit" in tool_map
    assert "generate_workspace_sbom" in tool_map
    assert "inspect_codebase_diagram" in tool_map
    assert "app_get_workspace_snapshot" in tool_map
    assert "app_save_doc" in tool_map
    assert "app_scaffold_project" in tool_map

    open_meta = tool_map["open_workbench"].meta or {}
    assert open_meta["ui"]["resourceUri"] == APP_URI
    assert open_meta["com.google.antigravity/ui"]["entrypoints"][0]["type"] == "aux_pane"

    app_only_meta = tool_map["app_save_doc"].meta or {}
    assert app_only_meta["ui"]["visibility"] == ["app"]

    resources = asyncio.run(srv.list_resources())
    res_map = {str(r.uri): r for r in resources}
    assert APP_URI in res_map
    assert res_map[APP_URI].mime_type == MIME_MCP_APP

    # Call open_workbench with empty {} defaults (env workspace) and explicit workspace
    res_open = asyncio.run(srv.call_tool("open_workbench", {"workspace": str(tmp_path)}))
    assert res_open is not None

    # Call app_save_doc and verify eager persistence
    asyncio.run(
        srv.call_tool(
            "app_save_doc",
            {
                "workspace": str(tmp_path),
                "rel_path": "docs/PLAN.md",
                "content": "# Plan\nUpdated from Workbench\n",
            },
        )
    )
    assert "Updated from Workbench" in (tmp_path / "docs/PLAN.md").read_text(encoding="utf8")

    # Call run_security_audit and inspect_codebase_diagram
    sec_res = asyncio.run(srv.call_tool("run_security_audit", {"workspace": str(tmp_path)}))
    assert sec_res is not None
    diag_res = asyncio.run(srv.call_tool("inspect_codebase_diagram", {"workspace": str(tmp_path)}))
    assert diag_res is not None


def test_install_mcp_plugin_creates_valid_antigravity_bundle(tmp_path):
    dest = tmp_path / PLUGIN_NAME
    installed = install_mcp_plugin(dest=dest, python_executable="/usr/bin/python3")
    assert installed == dest.resolve()

    plugin_json = json.loads((installed / "plugin.json").read_text(encoding="utf8"))
    assert plugin_json["name"] == PLUGIN_NAME
    assert plugin_json["logo"] == "assets/logo.svg"

    mcp_cfg = json.loads((installed / "mcp_config.json").read_text(encoding="utf8"))
    srv_cfg = mcp_cfg["mcpServers"]["workbench"]
    assert srv_cfg["command"] == "bash"
    assert "${PLUGIN_ROOT}/bin/adk-coder-mcp-server" in srv_cfg["args"]
    assert (installed / "bin/adk-coder-mcp-server").is_file()
    assert (installed / "assets/logo.svg").is_file()


def test_scaffold_mcp_app_template(tmp_path):
    h = AgentHarness(tmp_path, approve=lambda _: True)
    res = asyncio.run(h.execute("/scaffold mcp-app demo-widget"))
    assert "demo-widget" in res

    proj = tmp_path / "demo-widget"
    assert (proj / "plugin.json").is_file()
    assert (proj / "mcp_config.json").is_file()
    assert (proj / "bin/start-server.sh").is_file()
    assert (proj / "server/main.py").is_file()
    assert (proj / "web/package.json").is_file()
    assert (proj / "web/src/main.ts").is_file()
    assert (proj / "docs/PLAN.md").is_file()
    assert (proj / "docs/ARCHITECTURE.md").is_file()
    assert "ui://demo-widget/app.html" in (proj / "server/main.py").read_text(encoding="utf8")
    assert "${PLUGIN_ROOT}" in (proj / "mcp_config.json").read_text(encoding="utf8")
