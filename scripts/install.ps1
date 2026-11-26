<#
.SYNOPSIS
    Installs adk-coder and adk-coder-config on Windows (PowerShell).
.DESCRIPTION
    Supports installing from:
      1) A local Python wheel (.whl) passed via -Wheel (or found next to this script / in .\dist\)
      2) An offline wheelhouse folder (.\wheels\ next to this script)
      3) The local source checkout (pyproject.toml)
      4) PyPI / index package name (adk-coder[local])
.EXAMPLE
    .\scripts\install.ps1
.EXAMPLE
    .\scripts\install.ps1 -Wheel .\dist\adk_coder-0.1.0-py3-none-any.whl
.EXAMPLE
    .\scripts\install.ps1 -OpenConfig
#>
[CmdletBinding()]
param(
    [string]$Wheel = "",
    [string]$InstallDir = "",
    [switch]$OpenConfig
)

$ErrorActionPreference = "Stop"

if (-not $InstallDir) {
    $base = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $HOME "AppData\Local" }
    $InstallDir = Join-Path $base "adk-coder"
}
$venvDir = Join-Path $InstallDir "venv"
$scriptsDir = Join-Path $venvDir "Scripts"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptRoot

function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        (Join-Path $HOME ".local\bin\uv.exe"),
        (Join-Path $HOME ".cargo\bin\uv.exe")
    )
    if ($env:LOCALAPPDATA) {
        $candidates += (Join-Path $env:LOCALAPPDATA "uv\uv.exe")
    }
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

function Find-Python311Plus {
    foreach ($entry in @(
        @{ Cmd = "py"; Prefix = @("-3.13") },
        @{ Cmd = "py"; Prefix = @("-3.12") },
        @{ Cmd = "py"; Prefix = @("-3.11") },
        @{ Cmd = "py"; Prefix = @("-3") },
        @{ Cmd = "python"; Prefix = @() },
        @{ Cmd = "python3"; Prefix = @() }
    )) {
        if (-not (Get-Command $entry.Cmd -ErrorAction SilentlyContinue)) { continue }
        & $entry.Cmd @($entry.Prefix + @("-c", "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)")) 2>$null
        if ($LASTEXITCODE -eq 0) { return $entry }
    }
    return $null
}

# Resolve install source
$offlineWheels = Join-Path $scriptRoot "wheels"
$installSpec = $null

if ($Wheel) {
    $resolved = (Resolve-Path $Wheel).Path
    $uri = ([System.Uri]$resolved).AbsoluteUri
    $installSpec = "adk-coder[local,desktop] @ $uri"
} elseif (Test-Path $offlineWheels) {
    $installSpec = "adk-coder[local,desktop]"
} else {
    $distWhl = Get-ChildItem -Path $scriptRoot -Filter "adk_coder-*.whl" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $distWhl) {
        $distWhl = Get-ChildItem -Path (Join-Path $repoRoot "dist") -Filter "adk_coder-*.whl" -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    if ($distWhl) {
        $uri = ([System.Uri]$distWhl.FullName).AbsoluteUri
        $installSpec = "adk-coder[local,desktop] @ $uri"
    } elseif (Test-Path (Join-Path $repoRoot "pyproject.toml")) {
        $installSpec = "$repoRoot[local,desktop]"
    } else {
        $installSpec = "adk-coder[local,desktop]"
    }
}

$alreadyInstalled = Test-Path (Join-Path $scriptsDir "python.exe")
if ($alreadyInstalled) {
    Write-Host "==> Updating existing adk-coder installation in $venvDir" -ForegroundColor Cyan
} else {
    Write-Host "==> Installing adk-coder into $venvDir" -ForegroundColor Cyan
}
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

$uv = Find-Uv
if ($uv -and -not (Test-Path $offlineWheels)) {
    Write-Host "    Using uv: $uv"
    if (-not $alreadyInstalled) {
        & $uv venv $venvDir --python ">=3.11"
        if ($LASTEXITCODE -ne 0) { throw "uv venv failed." }
    }
    $venvPy = Join-Path $scriptsDir "python.exe"
    & $uv pip install --python $venvPy --reinstall-package adk-coder $installSpec
    if ($LASTEXITCODE -ne 0) { throw "uv pip install failed." }
} else {
    $py = Find-Python311Plus
    if (-not $py) {
        throw "Python 3.11+ or uv is required. Install uv (`irm https://astral.sh/uv/install.ps1 | iex`) or Python 3.11+ (`winget install Python.Python.3.13`) and retry."
    }
    Write-Host "    Using Python: $($py.Cmd) $($py.Prefix -join ' ')"
    if (-not $alreadyInstalled) {
        & $py.Cmd @($py.Prefix + @("-m", "venv", $venvDir))
        if ($LASTEXITCODE -ne 0) { throw "python -m venv failed." }
    }
    $pip = Join-Path $scriptsDir "pip.exe"
    if (Test-Path $offlineWheels) {
        & $pip install --no-index --find-links $offlineWheels --upgrade --force-reinstall --no-deps $installSpec
    } else {
        & $pip install --upgrade --force-reinstall --no-deps $installSpec
        & $pip install $installSpec
    }
    if ($LASTEXITCODE -ne 0) { throw "pip install failed." }
}

# Add venv Scripts directory to User PATH if not already present
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$parts = if ($userPath) { $userPath -split ";" | Where-Object { $_ -ne "" } } else { @() }
if ($parts -notcontains $scriptsDir) {
    $newPath = ($parts + $scriptsDir) -join ";"
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "==> Added $scriptsDir to your User PATH (restart PowerShell to pick it up globally)." -ForegroundColor Green
}

# Also expose in the current PowerShell session immediately
if (($env:Path -split ";") -notcontains $scriptsDir) {
    $env:Path = "$scriptsDir;$env:Path"
}

Write-Host ""
Write-Host "Installed/Updated adk-coder, adk-coder-app, and adk-coder-config!" -ForegroundColor Green
Write-Host "  Desktop IDE:   adk-coder-app   (or: adk-coder --app)"
Write-Host "  Configure UI:  adk-coder --config"
Write-Host "  Launch CLI:    adk-coder"
Write-Host ""

if ($OpenConfig) {
    & (Join-Path $scriptsDir "adk-coder.exe") --config
}
