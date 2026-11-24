<#
.SYNOPSIS
    Runs or installs the ADK Coder Workbench Antigravity MCP App server on Windows PowerShell.
.EXAMPLE
    .\scripts\run-mcp.ps1 -Check
.EXAMPLE
    .\scripts\run-mcp.ps1 -Install
.EXAMPLE
    .\scripts\run-mcp.ps1 -Workspace C:\work\my-app
#>
[CmdletBinding()]
param(
    [string]$Workspace = "",
    [string]$Html = "",
    [switch]$BuildUi,
    [switch]$Install,
    [switch]$Check
)

$ErrorActionPreference = "Stop"
$pkgDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$venvPy = Join-Path $pkgDir ".venv\Scripts\python.exe"
$py = if (Test-Path $venvPy) { $venvPy } else { "python" }

if ($BuildUi) {
    [Console]::Error.WriteLine("==> Building MCP App single-file UI...")
    npm --prefix (Join-Path $pkgDir "mcp_app_ui") run build 1>&2
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ($Install) {
    [Console]::Error.WriteLine("==> Installing ADK Coder Workbench plugin into Antigravity...")
    & $py -m adk_coder.cli --install-mcp-plugin
    exit $LASTEXITCODE
}

if ($Check) {
    & $py -c "import asyncio; from adk_coder.mcp_server import APP_URI, create_mcp_server, load_workbench_html; s=create_mcp_server(); t=asyncio.run(s.list_tools()); r=asyncio.run(s.list_resources()); print(f'MCP Server OK: {len(t)} tools, {len(r)} resource(s), UI HTML={len(load_workbench_html()):,} bytes'); print('UI Resource:', APP_URI); print('Tools:', ', '.join(x.name for x in t))"
    exit $LASTEXITCODE
}

$argsList = @("-m", "adk_coder.mcp_server")
if ($Workspace) { $argsList += @("--workspace", (Resolve-Path $Workspace).Path) }
if ($Html) { $argsList += @("--html", $Html) }

& $py @argsList
exit $LASTEXITCODE
