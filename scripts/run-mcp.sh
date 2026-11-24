#!/bin/sh
set -eu

pkg_dir=$(CDPATH= cd -P "$(dirname "$0")/.." && pwd)
parent_dir=$(CDPATH= cd -P "$pkg_dir/.." && pwd)

# Load .env quietly (never print to stdout so MCP stdio JSON-RPC stays clean)
if [ -f "$parent_dir/.env" ]; then
    . "$parent_dir/.env" >/dev/null 2>&1 || true
elif [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/adk-coder/.env" ]; then
    . "${XDG_CONFIG_HOME:-$HOME/.config}/adk-coder/.env" >/dev/null 2>&1 || true
fi

if [ -x "$pkg_dir/.venv/bin/python" ]; then
    py="$pkg_dir/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    py="python3"
else
    echo "Error: Python 3.11+ not found." >&2
    exit 1
fi

usage() {
    cat >&2 <<EOF
Usage: $0 [options]
  (default)              Run the ADK Coder MCP App server over stdio
  --workspace <dir>      Default workspace path for the MCP server
  --html <file>          Override workbench HTML path for live UI iteration
  --build-ui             Rebuild mcp_app_ui/ -> src/adk_coder/mcp_assets/workbench.html
  --install              Install/update Antigravity plugin (~/.gemini/config/plugins/adk-coder-workbench)
  --check                Verify MCP server, ui:// resource, and tools without blocking
  -h, --help             Show this help
EOF
}

do_build=0
do_install=0
do_check=0
workspace=""
html_override=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --build-ui)
            do_build=1
            shift
            ;;
        --install)
            do_install=1
            shift
            ;;
        --check)
            do_check=1
            shift
            ;;
        --workspace)
            workspace=$(CDPATH= cd -P "$2" && pwd)
            shift 2
            ;;
        --html)
            html_override="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if [ "$do_build" -eq 1 ]; then
    echo "==> Building MCP App single-file UI..." >&2
    npm --prefix "$pkg_dir/mcp_app_ui" run build >&2
fi

if [ "$do_install" -eq 1 ]; then
    echo "==> Installing ADK Coder Workbench plugin into Antigravity..." >&2
    "$py" -m adk_coder.cli --install-mcp-plugin >&2
    if [ -n "${ANTIGRAVITY_CSRF_TOKEN:-}" ]; then
        curl -sf -H "Content-Type: application/json" \
            -H "x-codeium-csrf-token: ${ANTIGRAVITY_CSRF_TOKEN}" \
            -d '{"serverName": "adk-coder-workbench_workbench"}' \
            "http://${ANTIGRAVITY_LS_ADDRESS:-localhost:5387}/exa.language_server_pb.LanguageServerService/RefreshMcpServers" >/dev/null 2>&1 || true
    fi
    exit 0
fi

if [ "$do_check" -eq 1 ]; then
    "$py" - <<'PY'
import asyncio
from adk_coder.mcp_server import APP_URI, create_mcp_server, load_workbench_html

srv = create_mcp_server()
tools = asyncio.run(srv.list_tools())
resources = asyncio.run(srv.list_resources())
html = load_workbench_html()
print(f"MCP Server OK: {len(tools)} tools, {len(resources)} resource(s), UI HTML={len(html):,} bytes")
print("UI Resource:", APP_URI)
print("Tools:", ", ".join(t.name for t in tools))
PY
    exit 0
fi

set -- -m adk_coder.mcp_server
if [ -n "$workspace" ]; then
    set -- "$@" --workspace "$workspace"
fi
if [ -n "$html_override" ]; then
    set -- "$@" --html "$html_override"
fi

exec "$py" "$@"
