"""Tests for the standalone native Desktop Agent IDE controller, SSE stream, and CLI shortcuts."""

from __future__ import annotations

import json
import sys
import threading

from adk_coder import cli as cli_mod
from adk_coder import desktop_app as desktop_mod
from adk_coder import harness as harness_mod
from adk_coder.desktop_app import (
    DesktopController,
    dispatch_desktop_request,
    load_desktop_html,
)


def test_desktop_html_asset_present():
    html = load_desktop_html()
    assert "ADK Coder" in html
    assert "New Session" in html
    assert "Automations" in html
    assert "Customizations" in html
    assert "/api/events" in html


def test_desktop_routes_bootstrap_files_sessions_terminal_and_inspector(tmp_path):
    env_file = tmp_path / ".env"
    ws = tmp_path / "demo_repo"
    ws.mkdir()
    (ws / "hello.py").write_text('@app.get("/hi")\ndef hi(): return eval("1")\n', encoding="utf8")

    ctrl = DesktopController(env_file, ws)

    st, ct, html = dispatch_desktop_request(ctrl, "GET", "/", {})
    assert st == 200 and "text/html" in ct and b"ADK Coder" in html

    st, _, raw = dispatch_desktop_request(ctrl, "GET", "/api/bootstrap", {})
    assert st == 200
    boot = json.loads(raw)
    assert boot["workspace_name"] == "demo_repo"
    assert boot["version"] == "2.0.0"
    assert boot["workspaces"][0]["name"] == "demo_repo"

    st, _, files_raw = dispatch_desktop_request(ctrl, "GET", "/api/files", {})
    assert st == 200 and "hello.py" in json.loads(files_raw)["files"]

    st, _, file_raw = dispatch_desktop_request(ctrl, "GET", "/api/file?path=hello.py", {})
    assert st == 200 and "/hi" in json.loads(file_raw)["content"]

    st, _, sess_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/session/new", {"name": "feature-work"}
    )
    assert st == 200 and json.loads(sess_raw)["ok"] is True

    st, _, mode_raw = dispatch_desktop_request(ctrl, "POST", "/api/mode", {"mode": "full-auto"})
    assert st == 200 and json.loads(mode_raw)["state"]["mode"] == "full-auto"

    st, _, term_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/terminal/run", {"command": f'"{sys.executable}" -c "print(42)"'}
    )
    term_data = json.loads(term_raw)
    assert st == 200 and term_data["ok"] is True and "42" in term_data["output"]

    st, _, sec_raw = dispatch_desktop_request(ctrl, "POST", "/api/tools/security", {})
    assert st == 200 and json.loads(sec_raw)["ok"] is True

    st, _, diag_raw = dispatch_desktop_request(ctrl, "POST", "/api/tools/diagram", {})
    diag = json.loads(diag_raw)
    assert st == 200 and diag["routes"][0]["path"] == "/hi"


def test_desktop_controller_chat_streaming_and_approval(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    ws = tmp_path / "chat_repo"
    ws.mkdir()

    ctrl = DesktopController(env_file, ws)
    q = ctrl.subscribe()

    # Verify interactive approval handshake works across threads
    decision_holder: dict[str, object] = {}

    def _ask_in_thread():
        decision_holder["result"] = ctrl._interactive_approve("Allow edit to app.py?")

    t = threading.Thread(target=_ask_in_thread)
    t.start()
    req_ev = q.get(timeout=3)
    assert req_ev["type"] == "approval_request"
    assert ctrl.resolve_approval(req_ev["id"], "always") is True
    t.join(timeout=3)
    assert decision_holder["result"] == "always"

    # Verify live chat turn emits turn_start and turn_done
    async def fake_generate(model, instruction, prompt, tools=(), **kwargs):
        return "Hello from desktop agent!"

    monkeypatch.setattr(harness_mod, "generate", fake_generate)

    st, _, send_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/chat/send", {"message": "say hi"}
    )
    assert st == 200 and json.loads(send_raw)["started"] is True

    seen_types: list[str] = []
    done_ev = None
    for _ in range(20):
        ev = q.get(timeout=5)
        seen_types.append(ev["type"])
        if ev["type"] == "turn_done":
            done_ev = ev
            break
    assert "turn_start" in seen_types
    assert done_ev is not None
    assert "Hello from desktop agent!" in done_ev["text"]


def test_cli_app_shortcuts(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(desktop_mod, "main", lambda argv: (calls.append(argv) or 0))

    assert cli_mod.main(["--workspace", str(tmp_path), "--app"]) == 0
    assert cli_mod.main(["--workspace", str(tmp_path), "-app"]) == 0
    assert cli_mod.main(["--workspace", str(tmp_path), "app"]) == 0
    assert len(calls) == 3
