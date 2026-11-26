# ADK Coder Release Notes

## v2.1.0 — Native Desktop Agent IDE & Developer Productivity Release

**Branch:** `adk-coder-v2.1` | **Tests:** 92 passed (`pytest`), `ruff` clean

### Highlights

#### 1. Native Standalone Desktop Agent IDE (`pywebview`)
- Launches a real native desktop window (**macOS WKWebView** / **Windows Edge WebView2**) via `adk-coder --app`, `adk-coder-app`, `./scripts/run-desktop.sh`, `.\scripts\run-desktop.ps1`, or the clickable macOS `ADK Coder.app` bundle.
- **Light / Dark / System Theme Toggle**: Respects OS appearance (`prefers-color-scheme`) with 1-click cycling in the top bar.
- **Inline `/` Command & `@file` Autocomplete**: Floating keyboard-navigable (`↑`/`↓`/`Tab`/`Enter`) menus for all built-in/custom slash commands and fuzzy workspace file attachment with removable chips.
- **Multimodal Image Paste (`⌘V`) & Drag-Drop**: Paste or drop error screenshots directly into the composer (saved under `.agent/uploads/` and sent with your prompt).
- **Message Queueing**: Submit follow-up instructions while the agent is still running — queued prompts execute automatically once the active turn finishes.
- **Live File-Write & Diff Preview Cards**: Coalesces streaming partial `write_file` / `apply_diff` / `apply_multi_diff` calls into single expandable code & diff cards with syntax highlighting, line numbers, `+N / -M` counts, `Writing → Written` status, and 1-click **Open in Drawer**. Completed turns retain a collapsible step summary in session history.
- **Offline Mermaid SVG Renderer**: Automatically renders `mermaid` blocks in chat and `/diagram` output in the Inspector as interactive SVG diagrams with **Zoom (`−`/`+`)**, **Code/Diagram toggle**, and **Export SVG**.

#### 2. Visual Workflows, Drawers & Session Management
- **Sidebar Session Management**: Auto-titles sessions from the first user prompt, plus live search and inline **Rename (`✎`)**, **Fork (`⑂`)**, and **Delete (`✕`)**.
- **Multi-Agent `/debug` Launcher & 4-Stage Pipeline**:
  - Interactive **Launch Multi-Agent Debug** modal (run a `--cmd`, pick a workspace `.log` file, paste a stack trace, or auto-detect, with optional `--analyze-only`).
  - Smart auto-detection in CLI & Desktop for project test runners (`pytest -q`, `mvn -B test`, `./gradlew test`, `npm test`, `go test ./...`, `cargo test`) and latest workspace `*.log` files.
  - Live 4-stage progress stepper (`Log Analyst → Root Cause → Fix Engineer → Verifier`) with 1-click **View `DEBUG_REPORT.md`**.
- **Plan-Before-Code Approval**: When in `Plan` mode, presents an inline **Approve Plan & Execute** bar to switch to `auto-edit` and execute the plan in 1 click.
- **Git Diff & AI Commit Drawer**: Per-file collapsible diffs with `+N -M` counts, per-file **Revert**, **AI Commit (`/commit`)**, custom commit message input, and **PR Review**.
- **Streaming Terminal & Live Watch Mode**: Streams stdout/stderr live with ANSI color rendering + **Stop**, plus an inline **Auto-Test Watch** mode (`👁 Watch` top-bar pill) with 1-click **Auto-Fix** on red tests.
- **Inspector & Checkpoints Timeline**: Offline Security/SOLID scan, SVG Architecture Diagram, CycloneDX SBOM, and Git **Checkpoints** (create, restore, delete snapshots, or undo recent edits).
- **Context & Token Meter**: Click the context badge in the composer to inspect prompt/output/total tokens, manage Pinned Files (Pin/Unpin), and run **Compact Context (`/compact`)**.
- **Tabbed Customizations & Automations**:
  - **Customizations → Extensions**: Manage workspace Skills, Custom `/commands`, `.agent/hooks.json` lifecycle hooks, and `.agent/mcp.json` MCP servers.
  - **Automations → New Project Scaffolder**: Wizard for `fastapi-service`, `java-spring`, `adk-python`, `adk-typescript`, and `go-a2a` with optional `requirements.md` and automatic workspace opening.

#### 3. First-Class Azure OpenAI Provider & Enterprise Proxy Support
- **Dedicated `azure` Provider**: Added **Azure OpenAI** across Web Config UI (`adk-coder --config`), Desktop **Customizations**, saved Profiles, and CLI (`AZURE_API_BASE`, `AZURE_API_KEY`, `AZURE_API_VERSION`, `azure/<deployment>`).
- **Automatic `max_completion_tokens` Routing**: Sends `max_completion_tokens` + `drop_params=True` automatically for Azure endpoints and OpenAI `o1` / `o3` / `o4` / `gpt-5` models, with transparent HTTP 400 retry + session caching for custom corporate gateways.
- **Smart Azure URL Normalization**: Supports pasting a clean Azure resource root (`https://<resource>.openai.azure.com`), a full deployment URL (`.../openai/deployments/<dep>[/chat/completions][?api-version=...]`), or an Azure OpenAI v1 URL (`.../openai/v1`) without path duplication or `404 Resource not found` errors.
- **Corporate SSL Proxy Friendly**: Sets `LITELLM_LOCAL_MODEL_COST_MAP=True` by default to eliminate slow remote cost-map retries and `UnknownIssuer` SSL warnings behind corporate proxies.

#### 4. Reliability & Bug Fixes
- **Instant Stop / Abort (`■`)**: 3-layer cancellation kills active shell subprocesses (`subprocess.Popen`), races LLM stream reads against `abort_signal` with bounded `runner.close()` timeout, clears queued messages, and cleanly detaches lingering sync calls while preserving partial output (`*(Stopped by user)*`) in session history.
- **Reliable Chat Turn Completion**: Added `GET /api/chat/status` live polling alongside SSE so turns never stall in WKWebView, and persisted slash commands, stopped turns, and errors to `session_history.json`.
- **Workspace & Profile Sync**: Replaced WKWebView-incompatible `window.prompt()`/`alert()` dialogs with an in-app **Switch or Open Workspace** modal (path input + native folder picker + recent workspaces), synced active global profiles/`.env` across all switched workspaces, and fixed profile **Use** / **Delete** button handling in both Web Config UI (`adk-coder --config`) and Desktop Customizations.

#### 5. Cross-Platform Packaging, Install Bundle & In-Place Upgrades
- **Portable All-in-One Bundle**: `./scripts/build-bundle.sh` builds `dist/adk-coder-2.1.0-install-bundle.zip` (Python wheel, npm `.tgz`, `install.ps1`, `create-windows-shortcut.ps1`, `run-desktop.ps1`, `run-desktop.sh`, and `RELEASE_NOTES.md`).
- **macOS App Bundle**: `./scripts/build-macos-app.sh` builds a clickable `dist/ADK Coder.app` (launches natively with no terminal window).
- **Windows In-Place Upgrades & `npx` Support**: `scripts/install.ps1` now installs `[local,desktop]` and updates existing `%LOCALAPPDATA%\adk-coder\venv` installations cleanly in place; `npm-package/bin/runner.js` auto-updates wheels via `.wheel-stamp` and auto-installs `pywebview` on `adk-coder-app` / `--app`.

---

### Install & Update

**macOS / Linux:**
```bash
# Clickable native app:
./scripts/build-macos-app.sh && open "dist/ADK Coder.app"

# Or from terminal:
./scripts/run-desktop.sh
```

**Windows (PowerShell / `cmd.exe`):**
```powershell
# Direct with npx using the bundled tarball:
npx --package .\adk-coder-2.1.0.tgz adk-coder-app

# Install or update globally via npm (auto-updates wheel on next launch):
npm install -g .\adk-coder-2.1.0.tgz
adk-coder-app

# Or install/update in place via PowerShell installer:
.\install.ps1 -Wheel .\adk_coder-2.1.0-py3-none-any.whl
```

---

## v2.0.0 — Initial Native Desktop App (`adk-coder-v2`)
- Introduced standalone `pywebview` native desktop shell (`adk-coder --app` / `adk-coder-app`), SSE chat streaming, interactive tool approvals, multi-workspace sidebar, Files/Git Diff/Inspector/Terminal drawers, and desktop launcher scripts.

## v1.0.0 — Initial Standalone CLI & Web Settings Release (`main`)
- Multi-provider Google ADK coding assistant CLI (`gemini`, `openai-compatible`, `ollama`, `anthropic`, `openrouter`, `groq`), Web Settings UI (`adk-coder --config`), named profiles with auto-fallback, 4-sub-agent `/debug`, `/analyze`, `/pr-review`, `/testgen`, `/diagram`, `/security`, `/sbom`, `/resolve-conflicts`, `/watch`, `/scaffold`, dual Python wheel + npm/npx packaging, and Windows `cmd.exe` support.
