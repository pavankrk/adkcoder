<#
.SYNOPSIS
Creates a Desktop and Start Menu shortcut for ADK Coder Desktop on Windows (no flashing console window).
#>
param(
    [string]$Workspace = (Get-Location).Path
)

$ErrorActionPreference = "Stop"
$appCmd = Get-Command adk-coder-app -ErrorAction SilentlyContinue
if (-not $appCmd) {
    Write-Error "adk-coder-app not found on PATH. Install first: pip install 'adk-coder[local,desktop]'"
    exit 1
}

$wsh = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "ADK Coder.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "ADK Coder.lnk")
)

foreach ($lnkPath in $targets) {
    $sc = $wsh.CreateShortcut($lnkPath)
    $sc.TargetPath = $appCmd.Source
    $sc.Arguments = "--workspace `"$Workspace`""
    $sc.WorkingDirectory = $Workspace
    $sc.WindowStyle = 7  # Minimized launcher window while native WebView2 opens
    $sc.Description = "ADK Coder — Native Desktop Agent IDE"
    $sc.Save()
    Write-Host "Created shortcut: $lnkPath" -ForegroundColor Green
}
