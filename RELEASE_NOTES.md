# ADK Coder Release Notes

## v2.2.0 — Power-User IDE, Auto-Verify, Persistent Memory, Smart Routing & API Tester

**Branch:** `v2.2` | **Tests:** 94 passed (`pytest`), `ruff` clean, JS syntax verified (`node --check`)

### Highlights

#### 1. Command Palette (`⌘K` / `Ctrl+K`), Inline File Editor & Per-Hunk Diff Review
- **Command Palette (`⌘K` / `Ctrl+K`)**: Instant fuzzy search with keyboard navigation (`↑`/`↓`/`Enter`/`Esc`) across quick actions, drawers, workspaces, saved LLM profiles, slash commands, and workspace files.
- **Editable Files Drawer**: Click **✎ Edit** on any open file in the Files drawer to edit inline with a monospace editor and save directly (`⌘S` / `Ctrl+S` via `POST /api/file/save`). Every save records an undo snapshot so **Undo Last Edit** can revert manual edits too.
- **Per-Hunk Diff Accept / Reject**: The Git Diff drawer splits every modified file into individual `@@` hunks (`GET /api/diff` → `hunks[]`) with per-hunk **Revert Hunk** buttons (`POST /api/git/revert-hunk`) alongside **Revert File**, letting you selectively discard one change while keeping the rest of the file intact.

#### 2. Auto-Verify Loop & Persistent Project Memory (`.agent/MEMORY.md`)
- **Auto-Verify Toggle (`✓ Auto-Verify`)**: One-click toggle in the composer bar (`POST /api/auto-verify`, persisted per workspace). Whenever the agent edits code, ADK Coder automatically runs your configured (or auto-detected) test suite and—if tests fail—runs an automatic self-fix pass and re-verifies, rendering a live pass/fixed/fail badge right on the turn card.
- **Persistent Project Memory (`🧠 Project Memory`)**: Sidebar modal (`GET/POST /api/memory`) to add, delete, or edit durable project conventions in `.agent/MEMORY.md`. Automatically injected into every chat session alongside `AGENTS.md`, `CLAUDE.md`, and `.agent/RULES.md`.

#### 3. Smart Dual-Model Routing & Corporate CA / Proxy Settings UI
- **Fast Profile Routing**: Designate a dedicated **Fast** profile in **Customizations → Provider & Profiles** (`POST /api/profiles/fast` or `/profile fast <name|none>`) so background tasks like context compaction (`/compact`), commit message drafting (`/commit`), and log triage automatically route to a fast/cheap model while coding stays on your primary model—safely restoring primary credentials in `os.environ` after each fast call.
- **Network & Certificates Tab**: New **Customizations → Network & Certificates** tab (`GET/POST /api/network`) to configure corporate TLS CA bundles (`SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`) and proxy variables (`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`) with live file existence verification, `.env` persistence, and automatic adoption by `_ssl_context()` (used by Test Connection and the REST Tester).

#### 4. Interactive API / REST Tester Drawer & One-Click Session Export
- **Interactive API / REST Tester (`🌐`)**: Auto-populates clickable route chips from your codebase (`GET /api/rest/routes` via `diagram.analyze_structure` for FastAPI, Flask, Spring, Express, and Go), executes live HTTP requests (`POST /api/rest/send`) with custom headers/JSON body, displays status/latency/pretty-printed JSON, copies ready-to-run `curl` commands, and provides a 1-click **Send Error to `/debug`** button whenever an endpoint returns an error (`4xx`/`5xx` or connection error).
- **One-Click Session Export (`⬇`)**: Top-bar button (`GET /api/session/export`) downloads the active conversation transcript (including workspace/branch/model metadata, tasks, step summaries, and Auto-Verify status) as `<workspace>_<session>.md` and copies it to your clipboard.

### Install & Update (`v2.2.0`)

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
npx --package .\adk-coder-2.2.0.tgz adk-coder-app

# Install or update globally via npm (auto-updates wheel on next launch):
npm install -g .\adk-coder-2.2.0.tgz
adk-coder-app

# Or install/update in place via PowerShell installer:
.\install.ps1 -Wheel .\adk_coder-2.2.0-py3-none-any.whl
```

---

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
