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
    assert boot["version"] == "2.2.0"
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

    # Test Stop forcefully interrupts a blocking synchronous subprocess (like a long test run)
    import time

    def _run_blocking_turn(model, instruction, prompt, tools=(), **kwargs):
        time.sleep(10)
        return "too-late"

    async def blocking_generate(model, instruction, prompt, tools=(), **kwargs):
        return _run_blocking_turn(model, instruction, prompt, tools=tools, **kwargs)

    monkeypatch.setattr(harness_mod, "generate", blocking_generate)
    st5, _, _ = dispatch_desktop_request(ctrl, "POST", "/api/chat/send", {"message": "slow sync"})
    assert st5 == 200
    for _ in range(10):
        ev = q.get(timeout=3)
        if ev["type"] == "turn_start":
            break
    t0 = time.monotonic()
    st_stop5, _, stop5_raw = dispatch_desktop_request(ctrl, "POST", "/api/chat/abort", {})
    elapsed_stop = time.monotonic() - t0
    stop5 = json.loads(stop5_raw)
    assert st_stop5 == 200 and stop5["ok"] is True and stop5["busy"] is False
    assert stop5["state"]["busy"] is False
    assert elapsed_stop < 3.0  # Must unlock immediately even if worker is stuck in a blocking call!


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


def test_desktop_v21_sessions_queue_image_terminal_watch_git_checkpoints_context_extensions(tmp_path):
    import base64
    import subprocess
    import time

    env_file = tmp_path / ".env"
    ws = tmp_path / "v21_repo"
    ws.mkdir()
    (ws / "app.py").write_text("def greet():\n    return 'hi'\n", encoding="utf8")

    # Initialize a real git repo for per-file diff, revert, commit, and checkpoints
    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=ws, check=True)
    subprocess.run(["git", "add", "."], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=ws, check=True, capture_output=True)

    ctrl = DesktopController(env_file, ws)

    # 1. Session rename, fork, search
    st, _, boot_raw = dispatch_desktop_request(ctrl, "GET", "/api/bootstrap", {})
    boot = json.loads(boot_raw)
    sid = boot["active_session_id"]
    assert any(c["name"] == "/debug" for c in boot["commands"])

    st, _, ren_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/session/rename", {"session_id": sid, "name": "Payment Refactor"}
    )
    assert st == 200 and json.loads(ren_raw)["ok"] is True

    st, _, srch_raw = dispatch_desktop_request(ctrl, "GET", "/api/sessions/search?q=payment", {})
    assert st == 200 and json.loads(srch_raw)["sessions"][0]["name"] == "Payment Refactor"

    st, _, fork_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/session/fork", {"session_id": sid, "name": "Payment Experiment"}
    )
    assert st == 200 and json.loads(fork_raw)["session"]["name"] == "Payment Experiment"

    # 2. Image paste upload
    png_1x1 = base64.b64encode(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    ).decode()
    st, _, img_raw = dispatch_desktop_request(
        ctrl,
        "POST",
        "/api/chat/upload-image",
        {"filename": "err.png", "data_url": f"data:image/png;base64,{png_1x1}"},
    )
    img_data = json.loads(img_raw)
    assert st == 200 and img_data["ok"] is True and img_data["path"].startswith(".agent/uploads/")
    assert (ws / img_data["path"]).is_file()

    # 3. Context pin / unpin
    st, _, pin_raw = dispatch_desktop_request(ctrl, "POST", "/api/context/pin", {"path": "app.py"})
    assert st == 200 and "app.py" in json.loads(pin_raw)["state"]["pinned"]
    st, _, unpin_raw = dispatch_desktop_request(ctrl, "POST", "/api/context/unpin", {"path": "app.py"})
    assert st == 200 and "app.py" not in json.loads(unpin_raw)["state"]["pinned"]

    # 4. Git per-file diff, revert, commit, and checkpoints
    (ws / "app.py").write_text("def greet():\n    return 'hello world'\n", encoding="utf8")
    st, _, diff_raw = dispatch_desktop_request(ctrl, "GET", "/api/diff", {})
    diff_data = json.loads(diff_raw)
    assert st == 200 and any(f["path"] == "app.py" and f["added"] >= 1 for f in diff_data["files"])

    st, _, chk_c_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/checkpoints/create", {"label": "before-revert"}
    )
    chk_id = json.loads(chk_c_raw)["id"]
    assert st == 200 and chk_id.startswith("chk_")

    st, _, chk_l_raw = dispatch_desktop_request(ctrl, "GET", "/api/checkpoints", {})
    assert st == 200 and any(c["id"] == chk_id for c in json.loads(chk_l_raw)["checkpoints"])

    st, _, _ = dispatch_desktop_request(ctrl, "POST", "/api/git/revert-file", {"path": "app.py"})
    assert st == 200 and "'hi'" in (ws / "app.py").read_text(encoding="utf8")

    st, _, chk_d_raw = dispatch_desktop_request(ctrl, "POST", "/api/checkpoints/delete", {"id": chk_id})
    assert st == 200 and json.loads(chk_d_raw)["ok"] is True

    # 5. Streaming terminal start / status / stop
    st, _, tstart = dispatch_desktop_request(
        ctrl,
        "POST",
        "/api/terminal/start",
        {"command": f'"{sys.executable}" -c "print(\'stream-ok\')"'},
    )
    assert st == 200 and json.loads(tstart)["ok"] is True
    for _ in range(30):
        _, _, tst_raw = dispatch_desktop_request(ctrl, "GET", "/api/terminal/status", {})
        tst = json.loads(tst_raw)
        if not tst["running"]:
            break
        time.sleep(0.1)
    assert tst["exit_code"] == 0 and "stream-ok" in tst["output"]

    # 6. Watch mode start / status / stop
    st, _, wst_raw = dispatch_desktop_request(
        ctrl,
        "POST",
        "/api/watch/start",
        {"command": f'"{sys.executable}" -c "print(1)"', "interval": 0.4},
    )
    assert st == 200 and json.loads(wst_raw)["watch"]["running"] is True
    st, _, wstop_raw = dispatch_desktop_request(ctrl, "POST", "/api/watch/stop", {})
    assert st == 200 and json.loads(wstop_raw)["watch"]["running"] is False

    # 7. Extensions CRUD (custom command, hooks, MCP add/remove)
    st, _, cmd_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/extensions/command", {"name": "release", "body": "Ship $ARGUMENTS"}
    )
    assert st == 200 and json.loads(cmd_raw)["ok"] is True
    st, _, hk_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/extensions/hooks", {"hooks": {"post_edit": ["echo edited"]}}
    )
    assert st == 200 and json.loads(hk_raw)["hooks"]["post_edit"] == ["echo edited"]
    st, _, ma_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/extensions/mcp/add", {"name": "demo", "command": "npx", "args": "-y demo"}
    )
    assert st == 200 and json.loads(ma_raw)["ok"] is True
    st, _, ext_raw = dispatch_desktop_request(ctrl, "GET", "/api/extensions", {})
    ext_data = json.loads(ext_raw)
    assert "demo" in ext_data["mcp_servers"]
    assert any(c["name"] == "release" for c in ext_data["commands"])
    st, _, mr_raw = dispatch_desktop_request(ctrl, "POST", "/api/extensions/mcp/remove", {"name": "demo"})
    assert st == 200 and json.loads(mr_raw)["ok"] is True

    # 8. Scaffold wizard with requirements & open_after
    st, _, sc_raw = dispatch_desktop_request(
        ctrl,
        "POST",
        "/api/tools/scaffold",
        {
            "template": "fastapi-service",
            "name": "mini-api",
            "parent_dir": str(tmp_path),
            "requirements": "Provide /health endpoint.",
            "minimal": True,
            "open_after": True,
        },
    )
    sc_data = json.loads(sc_raw)
    assert st == 200 and sc_data["ok"] is True and sc_data["state"]["workspace_name"] == "mini-api"

    # 9. Verify v2.1 UI features present in app.html
    html = load_desktop_html()
    for marker in (
        "themeToggleBtn",
        "renderMermaidSvg",
        "highlightCodeBlock",
        "renderDebugStepper",
        "approvePlanBtn",
        "sessionSearchInput",
        "acMenu",
        "watchPill",
    ):
        assert marker in html


def test_desktop_v22_file_save_per_hunk_revert_memory_network_rest_and_export(tmp_path, monkeypatch):
    import asyncio
    import io
    import os
    import subprocess
    import urllib.error
    import urllib.request

    monkeypatch.setattr(os, "environ", dict(os.environ))
    env_file = tmp_path / ".env"
    monkeypatch.setenv("ADK_CODER_ENV_FILE", str(env_file))

    ws = tmp_path / "v22_repo"
    ws.mkdir()
    lines = [f"line_{i} = {i}" for i in range(1, 25)]
    (ws / "multi.py").write_text("\n".join(lines) + "\n", encoding="utf8")
    (ws / "routes.py").write_text('@app.get("/api/ping")\ndef ping(): return {"ok": True}\n', encoding="utf8")

    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=ws, check=True)
    subprocess.run(["git", "add", "."], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=ws, check=True, capture_output=True)

    ctrl = DesktopController(env_file, ws)

    # 1. Editable Files Drawer (/api/file/save) + undo support
    st, _, save_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/file/save", {"path": "routes.py", "content": '@app.get("/api/ping")\ndef ping(): return {"ok": 2}\n'}
    )
    save_data = json.loads(save_raw)
    assert st == 200 and save_data["ok"] is True and save_data["state"]["edits_count"] >= 1
    assert '{"ok": 2}' in (ws / "routes.py").read_text(encoding="utf8")
    st, _, _undo_raw = dispatch_desktop_request(ctrl, "POST", "/api/checkpoint/undo", {"count": 1})
    assert st == 200 and '{"ok": True}' in (ws / "routes.py").read_text(encoding="utf8")

    # 2. Per-Hunk Diff Review & Revert (/api/diff hunks + /api/git/revert-hunk)
    edited_lines = list(lines)
    edited_lines[1] = "line_2 = 222"   # hunk 0 (near top)
    edited_lines[21] = "line_22 = 999"  # hunk 1 (near bottom, separated by >10 unchanged lines)
    (ws / "multi.py").write_text("\n".join(edited_lines) + "\n", encoding="utf8")

    st, _, diff_raw = dispatch_desktop_request(ctrl, "GET", "/api/diff", {})
    diff_data = json.loads(diff_raw)
    multi_entry = next(f for f in diff_data["files"] if f["path"] == "multi.py")
    assert st == 200 and len(multi_entry["hunks"]) == 2

    # Revert ONLY hunk 0 (line_2); hunk 1 (line_22 = 999) MUST remain!
    st, _, rh_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/git/revert-hunk", {"path": "multi.py", "hunk_index": 0}
    )
    assert st == 200 and json.loads(rh_raw)["ok"] is True
    after_hunk_revert = (ws / "multi.py").read_text(encoding="utf8")
    assert "line_2 = 2\n" in after_hunk_revert
    assert "line_22 = 999\n" in after_hunk_revert

    # 3. Persistent Project Memory (.agent/MEMORY.md)
    st, _, mem_add_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/memory", {"add_rule": "Always use Pydantic v2 models"}
    )
    mem_add = json.loads(mem_add_raw)
    assert st == 200 and mem_add["count"] == 1 and mem_add["state"]["memory_count"] == 1
    h = ctrl.get_harness()
    rule_sources, rule_text = h.load_project_rules()
    assert ".agent/MEMORY.md" in rule_sources
    assert "Always use Pydantic v2 models" in rule_text

    st, _, mem_rm_raw = dispatch_desktop_request(ctrl, "POST", "/api/memory", {"remove_index": 0})
    assert st == 200 and json.loads(mem_rm_raw)["count"] == 0

    # 4. Corporate CA / Proxy Settings (/api/network)
    ca_file = tmp_path / "corp-ca.pem"
    ca_file.write_text("-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n", encoding="utf8")
    st, _, net_raw = dispatch_desktop_request(
        ctrl,
        "POST",
        "/api/network",
        {
            "ssl_cert_file": str(ca_file),
            "http_proxy": "http://proxy.corp:8080",
            "https_proxy": "http://proxy.corp:8080",
            "no_proxy": "localhost,127.0.0.1",
        },
    )
    net_data = json.loads(net_raw)
    assert st == 200 and net_data["ca_exists"] is True
    assert os.environ["SSL_CERT_FILE"] == str(ca_file.resolve())
    assert os.environ["HTTP_PROXY"] == "http://proxy.corp:8080"

    st, _, net_get_raw = dispatch_desktop_request(ctrl, "GET", "/api/network", {})
    assert st == 200 and json.loads(net_get_raw)["https_proxy"] == "http://proxy.corp:8080"

    # Clear custom CA/proxy settings
    st, _, _ = dispatch_desktop_request(
        ctrl, "POST", "/api/network", {"ssl_cert_file": "", "http_proxy": "", "https_proxy": "", "no_proxy": ""}
    )
    assert st == 200 and "SSL_CERT_FILE" not in os.environ

    # 5. Smart Dual-Model Fast Profile Routing (/api/profiles/fast + harness.ask(fast=True))
    dispatch_desktop_request(
        ctrl,
        "POST",
        "/api/profiles/save",
        {"name": "PrimaryProf", "provider": "openai-compatible", "model": "big-coder", "base_url": "http://127.0.0.1:9001/v1"},
    )
    dispatch_desktop_request(
        ctrl,
        "POST",
        "/api/profiles/save",
        {"name": "FastProf", "provider": "openai-compatible", "model": "mini-fast", "base_url": "http://127.0.0.1:9002/v1"},
    )
    dispatch_desktop_request(ctrl, "POST", "/api/profiles/use", {"name": "PrimaryProf", "workspace": str(ws)})
    st, _, fast_raw = dispatch_desktop_request(ctrl, "POST", "/api/profiles/fast", {"name": "FastProf"})
    assert st == 200 and json.loads(fast_raw)["fast"] == "FastProf"

    seen_calls: list[tuple[str, str | None]] = []

    async def capture_generate(model, instruction, prompt, tools=(), **kwargs):
        seen_calls.append((model, kwargs.get("api_base")))
        return "ok"

    monkeypatch.setattr(harness_mod, "generate", capture_generate)
    h = ctrl.get_harness()
    asyncio.run(h.ask("summarize", "hello", fast=True))
    asyncio.run(h.ask("code", "hello", fast=False))
    assert seen_calls[0] == ("openai/mini-fast", "http://127.0.0.1:9002/v1")
    assert seen_calls[1] == ("openai/big-coder", "http://127.0.0.1:9001/v1")
    assert os.environ.get("OPENAI_API_BASE") == "http://127.0.0.1:9001/v1"

    # 6. Interactive API / REST Tester (/api/rest/routes + /api/rest/send)
    st, _, rroutes_raw = dispatch_desktop_request(ctrl, "GET", "/api/rest/routes", {})
    assert st == 200 and any(r["path"] == "/api/ping" for r in json.loads(rroutes_raw)["routes"])

    class _FakeResp:
        def __init__(self, status: int, body: bytes):
            self.status = status
            self.reason = "OK"
            self.headers = {"Content-Type": "application/json"}
            self._body = body

        def read(self, _n: int = -1) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def fake_urlopen(req, timeout=30.0, context=None):
        if req.full_url.endswith("/api/ping"):
            return _FakeResp(200, json.dumps({"pong": True}).encode())
        raise urllib.error.HTTPError(
            req.full_url, 500, "Internal Server Error", {}, io.BytesIO(json.dumps({"detail": "boom"}).encode())
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    st, _, ok_resp_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/rest/send", {"method": "GET", "url": "http://127.0.0.1:8000/api/ping"}
    )
    ok_resp = json.loads(ok_resp_raw)
    assert st == 200 and ok_resp["ok"] is True and ok_resp["status"] == 200
    assert '"pong": true' in ok_resp["body"] and "curl" in ok_resp["curl"]

    st, _, err_resp_raw = dispatch_desktop_request(
        ctrl, "POST", "/api/rest/send", {"method": "GET", "url": "http://127.0.0.1:8000/fail"}
    )
    err_resp = json.loads(err_resp_raw)
    assert st == 200 and err_resp["ok"] is False and err_resp["status"] == 500
    assert "boom" in err_resp["body"]

    # 7. Session Export (.md)
    h._save_history([{"role": "user", "text": "Build health route"}, {"role": "model", "text": "Done!"}])
    st, _, exp_raw = dispatch_desktop_request(ctrl, "GET", "/api/session/export", {})
    exp = json.loads(exp_raw)
    assert st == 200 and exp["ok"] is True and exp["filename"].endswith(".md")
    assert "Build health route" in exp["markdown"] and "Done!" in exp["markdown"]

    # 8. Verify v2.2 UI markers in app.html
    html = load_desktop_html()
    for marker in (
        "openCommandPalette",
        "openMemoryModal",
        "exportSessionMarkdown",
        "autoVerifyBtn",
        "pFastSel",
        "netCaInput",
        "data-revert-hunk-path",
        "fileEditorTa",
        "restSendBtn",
        "restDebugBtn",
    ):
        assert marker in html


def test_desktop_v22_auto_verify_pass_and_self_fix(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    ws = tmp_path / "av_repo"
    ws.mkdir()
    (ws / "calc.py").write_text("VALUE = 0\n", encoding="utf8")

    ctrl = DesktopController(env_file, ws)
    h = ctrl.get_harness()
    h.set_mode("full-auto")
    # Configure test command: passes only when calc.py contains VALUE = 42
    h.config["test_command"] = f'"{sys.executable}" -c "import calc; assert calc.VALUE == 42"'
    h.write("config.json", json.dumps(h.config, indent=2) + "\n")

    st, _, av_raw = dispatch_desktop_request(ctrl, "POST", "/api/auto-verify", {"enabled": True})
    assert st == 200 and json.loads(av_raw)["auto_verify"] is True

    q = ctrl.subscribe()
    call_num = {"n": 0}

    async def fake_generate(model, instruction, prompt, tools=(), **kwargs):
        call_num["n"] += 1
        if call_num["n"] == 1:
            # First edit introduces a bug (VALUE = 7) -> Auto-Verify fails -> triggers self-fix!
            h.tools.write_file("calc.py", "VALUE = 7\n")
            return "Updated calc.py"
        # Self-fix pass corrects VALUE = 42 -> Auto-Verify re-runs and passes!
        h.tools.write_file("calc.py", "VALUE = 42\n")
        return "Fixed calc.py to 42"

    monkeypatch.setattr(harness_mod, "generate", fake_generate)

    st, _, _ = dispatch_desktop_request(ctrl, "POST", "/api/chat/send", {"message": "set calc value"})
    assert st == 200

    done_ev = None
    for _ in range(30):
        ev = q.get(timeout=8)
        if ev["type"] == "turn_done":
            done_ev = ev
            break
    assert done_ev is not None
    assert done_ev["auto_verify"]["status"] == "fixed"
    assert done_ev["auto_verify"]["attempts"] == 2
    assert "VALUE = 42" in (ws / "calc.py").read_text(encoding="utf8")
    assert done_ev["state"]["history"][-1]["auto_verify"]["status"] == "fixed"
