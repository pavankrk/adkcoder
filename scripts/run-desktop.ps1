<#
.SYNOPSIS
    Launch the standalone native ADK Coder Desktop Agent IDE window on Windows (WebView2).
.EXAMPLE
    .\scripts\run-desktop.ps1
.EXAMPLE
    .\scripts\run-desktop.ps1 -Workspace C:\work\my-project
#>
[CmdletBinding()]
param(
    [string]$Workspace = (Get-Location).Path
)

$ErrorActionPreference = "Stop"
$pkgDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$venvApp = Join-Path $pkgDir ".venv\Scripts\adk-coder-app.exe"
$venvPy = Join-Path $pkgDir ".venv\Scripts\python.exe"

if (Test-Path $venvApp) {
    & $venvApp --workspace $Workspace
} elseif (Test-Path $venvPy) {
    & $venvPy -m adk_coder.desktop_app --workspace $Workspace
} else {
    & python -m adk_coder.desktop_app --workspace $Workspace
}
exit $LASTEXITCODE
