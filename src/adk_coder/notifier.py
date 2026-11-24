"""Cross-platform desktop notifications with safe message escaping."""

import shutil
import subprocess
import sys


def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _escape_powershell(text: str) -> str:
    return text.replace("'", "''").replace("\n", " ")


def build_command(message: str, platform: str | None = None) -> list[str]:
    target = platform or sys.platform
    clean = (message or "Command finished").strip()[:200]
    if target == "darwin":
        escaped = _escape_applescript(clean)
        return [
            "osascript",
            "-e",
            f'display notification "{escaped}" with title "adk-coder"',
        ]
    if target == "win32":
        escaped = _escape_powershell(clean)
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "Add-Type -AssemblyName System.Drawing;"
            "$n = New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon = [System.Drawing.SystemIcons]::Information;"
            "$n.Visible = $true;"
            f"$n.ShowBalloonTip(3000, 'adk-coder', '{escaped}', "
            "[System.Windows.Forms.ToolTipIcon]::Info);"
            "Start-Sleep -Seconds 4;$n.Dispose()"
        )
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    return ["notify-send", "adk-coder", clean]


def notify(message: str, enabled: bool) -> None:
    if not enabled:
        return
    print("\a", end="", file=sys.stderr, flush=True)
    args = build_command(message)
    if shutil.which(args[0]):
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
