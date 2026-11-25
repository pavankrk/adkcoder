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

    # Verify live chat turn emits turn_start, coalesced write_file preview card, and turn_done
    async def fake_generate(model, instruction, prompt, tools=(), **kwargs):
        # Simulate streaming partial chunks of the SAME write_file call — must coalesce into 1 card!
        harness_mod.render_tool_call("write_file", {"file_path": "app.py", "content": "def hi():\n"})
        harness_mod.render_tool_call(
            "write_file", {"file_path": "app.py", "content": "def hi():\n    return 42\n"}
        )
        harness_mod.render_tool_result("write_file", "Wrote app.py (2 lines)")
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
    # Crucial: turn_done state must report busy=False so a second request is immediately allowed!
    assert done_ev["state"]["busy"] is False
    # Partial streaming write_file chunks coalesced into 1 rich preview card with full code content!
    assert len(done_ev["steps"]) == 1
    wstep = done_ev["steps"][0]
    assert wstep["kind"] == "write" and wstep["file_path"] == "app.py"
    assert "return 42" in wstep["content"] and wstep["lines_added"] == 2
    assert wstep["status"] == "done"
    # Step card persisted onto the model turn in session history
    assert done_ev["state"]["history"][-1]["steps"][0]["file_path"] == "app.py"

    # Send a second consecutive chat turn immediately after the first
    st2, _, send2_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/chat/send", {"message": "second turn"}
    )
    assert st2 == 200 and json.loads(send2_raw)["started"] is True
    done2 = None
    for _ in range(20):
        ev = q.get(timeout=5)
        if ev["type"] == "turn_done":
            done2 = ev
            break
    assert done2 is not None and done2["state"]["busy"] is False

    # Test aborting a slow in-flight turn unlocks busy state cleanly
    import asyncio

    async def slow_generate(model, instruction, prompt, tools=(), **kwargs):
        await asyncio.sleep(30)
        return "never"

    monkeypatch.setattr(harness_mod, "generate", slow_generate)
    st3, _, _ = dispatch_desktop_request(ctrl, "POST", "/api/chat/send", {"message": "hang"})
    assert st3 == 200
    # Wait for turn_start so worker task is active
    for _ in range(10):
        ev = q.get(timeout=3)
        if ev["type"] == "turn_start":
            break
    st_ab, _, ab_raw = dispatch_desktop_request(ctrl, "POST", "/api/chat/abort", {})
    assert st_ab == 200 and json.loads(ab_raw)["ok"] is True
    done3 = None
    for _ in range(20):
        ev = q.get(timeout=5)
        if ev["type"] in {"turn_done", "turn_error"}:
            done3 = ev
            break
    assert done3 is not None and done3["state"]["busy"] is False
    # Stopped turn must stay in session history (not wipe back to an empty session!)
    assert any("Stopped by user" in t["text"] for t in done3["state"]["history"])

    # Status polling endpoint reflects finished turn cleanly
    st_poll, _, poll_raw = dispatch_desktop_request(ctrl, "GET", "/api/chat/status", {})
    poll_data = json.loads(poll_raw)
    assert st_poll == 200 and poll_data["busy"] is False and poll_data["done_turn_id"] >= 3

    # Slash command output must also be persisted to session history
    st_cmd, _, cmd_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/chat/send", {"message": "/mode"}
    )
    assert st_cmd == 200 and json.loads(cmd_raw)["turn_id"] >= 4
    done4 = None
    for _ in range(20):
        ev = q.get(timeout=5)
        if ev["type"] == "turn_done":
            done4 = ev
            break
    assert done4 is not None and done4["state"]["busy"] is False
    assert any(t["role"] == "user" and t["text"] == "/mode" for t in done4["state"]["history"])


def test_cli_app_shortcuts(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(desktop_mod, "main", lambda argv: (calls.append(argv) or 0))

    assert cli_mod.main(["--workspace", str(tmp_path), "--app"]) == 0
    assert cli_mod.main(["--workspace", str(tmp_path), "-app"]) == 0
    assert cli_mod.main(["--workspace", str(tmp_path), "app"]) == 0
    assert len(calls) == 3


def test_desktop_workspace_switching_picker_and_profile_delete(tmp_path):
    from adk_coder import config_ui

    env_file = tmp_path / ".env"
    ws1 = tmp_path / "repo_one"
    ws2 = tmp_path / "repo_two"
    ws1.mkdir()
    ws2.mkdir()
    (ws1 / "one.py").write_text("x = 1\n", encoding="utf8")
    (ws2 / "two.py").write_text("y = 2\n", encoding="utf8")

    ctrl = DesktopController(env_file, ws1)

    class _FakeWindow:
        def __init__(self, dialog_result):
            self.dialog_result = dialog_result
            self.titles: list[str] = []

        def set_title(self, title: str) -> None:
            self.titles.append(title)

        def create_file_dialog(self, dialog_type, directory=""):
            return self.dialog_result

    fake_win = _FakeWindow(str(ws2))  # bare str return should NOT slice to "/"
    ctrl.attach_window(fake_win)

    # Switch via native picker returning a bare str
    st, _, pick_raw = dispatch_desktop_request(ctrl, "POST", "/api/workspace/pick", {})
    pick_data = json.loads(pick_raw)
    assert st == 200 and pick_data["ok"] is True
    assert pick_data["state"]["workspace_name"] == "repo_two"
    assert fake_win.titles[-1] == "ADK Coder — repo_two"

    # Files endpoint reflects newly active workspace
    st, _, files_raw = dispatch_desktop_request(ctrl, "GET", "/api/files", {})
    assert st == 200 and "two.py" in json.loads(files_raw)["files"]

    # Switch back with quoted path via /api/workspace/select
    st, _, sel_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/workspace/select", {"workspace": f'"{ws1}"'}
    )
    sel_data = json.loads(sel_raw)
    assert st == 200 and sel_data["ok"] is True and sel_data["workspace_name"] == "repo_one"
    assert any(w["name"] == "repo_one" and w["active"] for w in sel_data["workspaces"])
    assert any(w["name"] == "repo_two" and not w["active"] for w in sel_data["workspaces"])

    # Remove second workspace cleanly
    st, _, rm_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/workspace/remove", {"workspace": str(ws2)}
    )
    rm_data = json.loads(rm_raw)
    assert st == 200 and all(w["name"] != "repo_two" for w in rm_data["workspaces"])

    # Save & delete profile via desktop routes (including name with spaces)
    st, _, psave_raw = dispatch_desktop_request(
        ctrl,
        "POST",
        "/api/profiles/save",
        {
            "name": "Local Test Profile",
            "provider": "openai-compatible",
            "model": "qwen2.5-coder",
            "base_url": "http://127.0.0.1:8080/v1",
        },
    )
    assert st == 200 and json.loads(psave_raw)["ok"] is True

    st, _, pdel_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/profiles/delete", {"name": "Local Test Profile"}
    )
    assert st == 200 and json.loads(pdel_raw)["ok"] is True

    st, _, plist_raw = dispatch_desktop_request(ctrl, "GET", "/api/profiles", {})
    assert "Local Test Profile" not in json.loads(plist_raw)["profiles"]

    # Verify config_ui HTML uses safe data-* attributes (no broken JSON.stringify inside double-quoted onclick)
    assert 'onclick="deleteProfile(' not in config_ui.HTML_PAGE
    assert 'onclick="useProfile(' not in config_ui.HTML_PAGE
    assert "data-delete-profile" in config_ui.HTML_PAGE
    assert "data-use-profile" in config_ui.HTML_PAGE

    # Verify desktop HTML has no WKWebView-broken window.prompt() calls and includes profile delete UI
    html = load_desktop_html()
    assert "prompt(" not in html
    assert "data-del-prof" in html
    assert "openWorkspaceModal" in html


def test_desktop_model_syncs_across_workspaces_and_profile_switches(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(os, "environ", dict(os.environ))
    env_file = tmp_path / ".env"
    ws1 = tmp_path / "ws_alpha"
    ws2 = tmp_path / "ws_beta"
    ws1.mkdir()
    ws2.mkdir()

    # Pre-seed ws2 with an old stale .agent/config.json model
    (ws2 / ".agent").mkdir()
    (ws2 / ".agent" / "config.json").write_text(
        json.dumps({"model": "gemini-2.5-flash", "workspace_root": str(ws2)}),
        encoding="utf8",
    )

    ctrl = DesktopController(env_file, ws1)

    # Save two profiles and activate Profile A while on ws1
    st, _, _ = dispatch_desktop_request(
        ctrl,
        "POST",
        "/api/profiles/save",
        {
            "name": "ProfA",
            "provider": "openai-compatible",
            "model": "model-a",
            "base_url": "http://127.0.0.1:8081/v1",
            "mode": "auto-edit",
        },
    )
    assert st == 200
    st, _, _ = dispatch_desktop_request(
        ctrl,
        "POST",
        "/api/profiles/save",
        {
            "name": "ProfB",
            "provider": "ollama",
            "model": "qwen2.5-coder:14b",
            "base_url": "http://127.0.0.1:11434",
            "mode": "full-auto",
        },
    )
    assert st == 200
    st, _, _ = dispatch_desktop_request(
        ctrl, "POST", "/api/profiles/use", {"name": "ProfA", "workspace": str(ws1)}
    )
    assert st == 200

    # Switching to ws2 MUST sync ws2 to ProfA's model (NOT stale gemini-2.5-flash)
    st, _, sel_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/workspace/select", {"workspace": str(ws2)}
    )
    sel_data = json.loads(sel_raw)
    assert st == 200
    assert sel_data["workspace_name"] == "ws_beta"
    assert sel_data["model"] == "openai/model-a"

    # Switching profile to ProfB while on ws2 updates ws2 immediately...
    st, _, _ = dispatch_desktop_request(
        ctrl, "POST", "/api/profiles/use", {"name": "ProfB", "workspace": str(ws2)}
    )
    assert st == 200
    st, _, boot_raw = dispatch_desktop_request(ctrl, "GET", "/api/bootstrap", {})
    boot = json.loads(boot_raw)
    assert boot["model"] == "ollama_chat/qwen2.5-coder:14b"
    assert boot["mode"] == "full-auto"

    # ...and switching back to ws1 ALSO has ProfB's model (NOT stale ProfA model)
    st, _, back_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/workspace/select", {"workspace": str(ws1)}
    )
    back_data = json.loads(back_raw)
    assert st == 200
    assert back_data["workspace_name"] == "ws_alpha"
    assert back_data["model"] == "ollama_chat/qwen2.5-coder:14b"
    assert back_data["mode"] == "full-auto"
