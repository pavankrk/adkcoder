# ADK Coder — System & Multi-Agent Architecture

`adk-coder` is a local-first, Google ADK 2.x coding assistant built around a central `AgentHarness`, an ADK `InMemoryRunner` + `Workflow` graph runtime, scoped workspace tools with safety/rollback guards, and specialized sub-agents for SDLC engineering, codebase analysis, and automated debugging.

---

## 1. High-Level System Architecture

```mermaid
flowchart TD
    subgraph Surfaces["User Surfaces & Packaging"]
        CLI["CLI REPL & Headless Mode<br/>(adk_coder/cli.py)"]
        CFG["Settings Web UI (Material 3)<br/>(adk_coder/config_ui.py)"]
        WEB["ADK Web Adapter<br/>(WorkspaceAgent / adk_coder_web)"]
        PKG["Install Bundles<br/>(uvx .whl · npx .tgz · install.ps1)"]
        PKG --> CLI
        PKG --> CFG
    end

    subgraph Core["Orchestration Core"]
        HARNESS["AgentHarness<br/>(adk_coder/harness.py)<br/>Commands · Modes · Context · Routing"]
        EXT["Extensions Loader<br/>(extensions.py)<br/>Commands · Skills · Hooks · MCP"]
        MAP["Repo Map & Token Manager<br/>(repo_map.py · token_manager.py)"]
        HARNESS --> EXT
        HARNESS --> MAP
    end

    subgraph ADK["Google ADK 2.x Agent Runtime (model_factory.py)"]
        RUNNER["InMemoryRunner & SessionService<br/>SSE Streaming · Abort · Usage"]
        CHAT_AG["Primary Agent<br/>coding_assistant (Agent)"]
        WF_SDLC["SDLC Graph<br/>coding_workflow (Workflow + JoinNode)"]
        WF_DBG["Debug Graph<br/>debug_subagent_workflow (Workflow)"]
        SUB_DBG["4 Debug Sub-Agents<br/>log_analyst · root_cause_investigator<br/>fix_engineer · verifier"]
        RUNNER --> CHAT_AG
        RUNNER --> WF_SDLC
        RUNNER --> WF_DBG
        WF_DBG --> SUB_DBG
    end

    subgraph Tools["Tooling, Security & Verification Layer"]
        WTOOLS["WorkspaceTools (tools.py)<br/>read · search · apply_diff · write · shell"]
        SCRUB["Secret Scrubber (scrubber.py)"]
        UNDO["Rollback Stack<br/>(.agent/edit_history.json)"]
        CHECK["Security · SBOM · Audit · Fixer<br/>(security.py · sbom.py · audit.py · fixer.py)"]
        MCP["External MCP Servers<br/>(ADK McpToolset)"]
        WTOOLS --> SCRUB
        WTOOLS --> UNDO
    end

    subgraph State["Persistent Workspace & User State"]
        DOTAGENT[".agent/ Blackboard<br/>config.json · context_summary.md<br/>TASKS.md · ANALYSIS.md · DEBUG_REPORT.md"]
        SQLITE[".agent/sessions.db (SQLite)<br/>Multi-session transcripts"]
        ENV["Global .env<br/>(~/.config or %APPDATA%/adk-coder/.env)"]
    end

    subgraph Models["LLM Providers (resolve_model)"]
        GEMINI["Google Gemini (Native ADK)"]
        LITELLM["LiteLLM Router<br/>OpenAI-compatible / Local · Ollama<br/>Anthropic · Groq · OpenRouter"]
    end

    CLI --> HARNESS
    WEB --> HARNESS
    CFG --> ENV
    HARNESS --> RUNNER
    HARNESS --> CHECK
    CHAT_AG --> WTOOLS
    CHAT_AG --> MCP
    WF_SDLC --> WTOOLS
    SUB_DBG --> WTOOLS
    HARNESS --> DOTAGENT
    HARNESS --> SQLITE
    HARNESS --> ENV
    RUNNER --> GEMINI
    RUNNER --> LITELLM
```

---

## 2. How Many Agents Are Working?

`adk-coder` runs on **Google ADK 2.x** (`Agent`, `BaseAgent`, `Workflow`, `JoinNode`, `AgentTool`, `InMemoryRunner`). Depending on the command you invoke, work is handled by:

- **2 ADK `Workflow` Graph Orchestrators** (`coding_workflow`, `debug_subagent_workflow`)
- **4 Named Debug Sub-Agents** (`log_analyst`, `root_cause_investigator`, `fix_engineer`, `verifier`)
- **1 Primary Interactive Coding Agent** (`coding_assistant`, dynamically configured with role-scoped instructions and toolsets across 8 task modes)
- **1 ADK Web Adapter Agent** (`adk_coder_web` / `WorkspaceAgent` subclassing `google.adk.agents.BaseAgent`)

In total, there are **6 named ADK agent/workflow runtime entities** driving **14 specialized agent roles**:

| # | Agent / Sub-Agent Role | ADK Primitive | Module | Tool Access Scope | Triggered By |
| --- | --- | --- | --- | --- | --- |
| 1 | `coding_assistant` (Interactive Coder) | `Agent` | `model_factory.py` / `harness.py` | Full workspace tools + MCP (gated by mode) | Normal chat, `/feature`, `/tdd` |
| 2 | `coding_assistant` (Planner) | `Agent` | `harness.py` | Read-only (`list_dir`, `find_files`, `search_files`, `read_file`) | `/plan` |
| 3 | `coding_assistant` (Codebase Explorer) | `Agent` | `harness.py` | Read-only (`fast_model` when set) | `/explore` |
| 4 | `coding_assistant` (Architectural Analyst) | `Agent` | `analyzer.py` | None (bounded deterministic excerpts) | `/analyze`, `/analyse` |
| 5 | `coding_assistant` (Security/Code Auditor) | `Agent` | `audit.py` | None (structured JSON findings) | `/audit`, `/scan` |
| 6 | `coding_assistant` (Audit Patch Fixer) | `Agent` | `fixer.py` | Programmatic `apply_diff` + `run_shell_command` verification | `/fix` |
| 7 | `coding_workflow` (6-Stage SDLC Orchestrator) | `Workflow` + `JoinNode` | `harness.py` | Delegates across 7 stage roles (Req, Arch, Interfaces, Standards, Test Plan, Implement, Review) | `/workflow` |
| 8 | `debug_subagent_workflow` (Debug Orchestrator) | `Workflow` | `debugger.py` | Coordinates 4 debug sub-agents + retry/rollback | `/debug`, `/fix debug` |
| 9 | `log_analyst` | `Agent` (`output_key="log_analysis"`) | `debugger.py` | None (`fast_model`, scrubbed parsed log payload) | `/debug` Stage 1 |
| 10 | `root_cause_investigator` | `Agent` (`output_key="root_cause_report"`) + `AgentTool` | `debugger.py` | Read-only workspace tools + preloaded companion files | `/debug` Stage 2 & `AgentTool` |
| 11 | `fix_engineer` | `Agent` (`output_key="fix_summary"`) | `debugger.py` | Read + `apply_diff`, `apply_multi_diff`, `write_file`, `run_shell_command` (+ JSON patch fallback) | `/debug` Stage 3 |
| 12 | `verifier` | `Agent` (`output_key="verification_verdict"`) | `debugger.py` | Read + `run_shell_command` | `/debug` Stage 4 |
| 13 | `coding_assistant` (Blueprint / Reviewer) | `Agent` | `scaffolder.py` / `harness.py` | Read-only / git diff inspection | `/blueprint`, `/review`, `/commit` |
| 14 | `adk_coder_web` (`WorkspaceAgent`) | `BaseAgent` | `web_agents/adk_coder_web/agent.py` | Wraps `AgentHarness.execute()` in-process with an `asyncio.Lock` | `adk web` |

> **Deterministic (Non-LLM) Engines** also run offline alongside the agents without calling an LLM: `security.py` (AST SAST + secrets + SOLID checks), `sbom.py` (CycloneDX/SPDX generator), `repo_map.py` (AST symbol index), `debugger.py::parse_logs` (multi-runtime stack-trace parser), and `doctor.py` (environment diagnostics).

---

## 3. How Agents Communicate

Agents do **not** talk over raw ad-hoc prompts or uncontrolled infinite transfer loops. They communicate through **5 structured channels**:

### Channel A — ADK `Workflow` Graph Edges & `Event(route, output)`
Used by `/debug` (`debug_subagent_workflow`) and `/workflow` (`coding_workflow`).
- Each node executes a specialized sub-agent and returns either a payload string (passed as `node_input` to the next node) or a Google ADK `Event(route="...", output=...)` to branch conditionally.
- Parallel branches fan back together deterministically using ADK's `JoinNode`.

### Channel B — Multi-Sub-Agent `/debug` Communication Flow

```mermaid
sequenceDiagram
    actor User
    participant Parser as Deterministic Parser<br/>(parse_logs + Companion Discovery)
    participant S1 as Sub-Agent 1<br/>log_analyst (fast)
    participant S2 as Sub-Agent 2<br/>root_cause_investigator (read-only)
    participant Gate as Report & Approval Gate<br/>(.agent/DEBUG_REPORT.md)
    participant S3 as Sub-Agent 3<br/>fix_engineer (write + shell)
    participant FB as JSON Patch Fallback<br/>(if 0 edits made)
    participant S4 as Sub-Agent 4<br/>verifier (shell / review)

    User->>Parser: /debug logfile | --cmd "mvn test"
    Parser->>Parser: Scrub secrets, dedup errors,<br/>map frames -> prod companion files
    Parser->>S1: Structured JSON log payload
    S1-->>S2: Sanitized Failure Brief (log_analysis)
    Note over S2: Inspects preloaded test + prod files<br/>and git diff/log via read-only tools
    S2-->>Gate: Root-Cause Analysis (root_cause_report)
    Gate->>User: Render RCA Panel & save DEBUG_REPORT.md
    User-->>Gate: Approve fix delegation? (y/N)

    alt Approved (and not --analyze-only / plan)
        loop Up to --max-retries (default 2)
            Gate->>S3: Failure Brief + RCA + fresh line-numbered files + retry feedback
            S3->>S3: apply_diff / write_file / run_shell_command
            opt 0 file edits applied by S3
                S3->>FB: Strict JSON patch request
                FB->>FB: Fuzzy-match & apply_diff
            end
            S3-->>S4: Edit count + git diff stat + summary
            S4->>S4: Run verify_cmd (or skip if 0 edits)
            alt Exit 0 (PASS)
                S4-->>User: Verified fix + updated DEBUG_REPORT.md
            else Failed & retries left (RETRY)
                S4-->>S3: Parsed top error signatures + tail
            else Failed & retries exhausted (ROLLBACK)
                S4->>S4: tools.undo_last_edit(edits_added)
                S4-->>User: Auto-rolled back + failure report
            end
        end
    else Declined / --analyze-only
        Gate-->>User: Analysis report saved (no edits)
    end
```

### Channel C — 6-Stage `/workflow` Parallel Fan-Out & Review Loop

```mermaid
flowchart TD
    START([START]) --> REQ["1. Requirements Sub-Agent<br/>(read-only)"]
    REQ --> ARCH["2. Architecture Sub-Agent<br/>(read-only)"]
    ARCH --> INTF["3. Interfaces Sub-Agent<br/>(read-only)"]
    INTF --> GATE{"Human Design<br/>Approval Gate"}
    GATE -- declined --> STOP([Stop at Design Review])
    GATE -- approved --> STD["4a. Coding Standards<br/>(parallel branch)"]
    GATE -- approved --> TST["4b. Test Plan<br/>(parallel branch)"]
    STD --> JOIN["ADK JoinNode<br/>(join_plans)"]
    TST --> JOIN
    JOIN --> SPEC["Merged Specification"]
    SPEC --> IMPL["5. Implementer Agent<br/>(AgentHarness.chat + write tools)"]
    IMPL --> REV{"6. Reviewer Sub-Agent<br/>+ test_command<br/>(up to 3 passes)"}
    REV -- "REVISE (feedback)" --> IMPL
    REV -- "PASS" --> FINISH([Save to context_summary.md & Return])
```

### Channel D — ADK Session Memory, Blackboard Files & `AgentTool`
1. **Multi-Turn ADK Session Seeding (`model_factory.py`)**: Before each `chat()` call, `run_agent()` seeds ADK's `InMemorySessionService` with prior `user` / `model` turns via `append_event(...)`, keeping multi-turn reasoning coherent across calls while persisting transcripts to `.agent/sessions.db` (SQLite).
2. **Workspace Blackboard (`.agent/`)**: Agents share durable state through workspace artifacts rather than blowing up token budgets:
   - `.agent/ANALYSIS.md` (written by `/analyze`, automatically read by `/feature`)
   - `.agent/DEBUG_REPORT.md` (written by `/debug`, automatically read by `/fix debug`)
   - `.agent/audit_cache.json` (written by `/audit` & `/security`, consumed & retired item-by-item by `/fix`)
   - `.agent/PLAN.md`, `.agent/TASKS.md`, `.agent/context_summary.md`, `.agent/pinned.json`
   - `.agent/edit_history.json` (pre-edit file snapshots shared across all write-capable agents for `/undo-edit` and automatic rollback)
3. **ADK `AgentTool` Delegation (`debugger.py::build_debug_agent_tool`)**: Wraps `root_cause_investigator` as a Google ADK `AgentTool` so parent agents can invoke it directly as a tool call when needed.

### Channel E — Live Event & Callback Bus (Streaming UI + Guardrails)
Inside `model_factory.py::run_agent`, ADK `Event` streams are dispatched in real time to callbacks:
- `on_thought(chunk)` → dimmed reasoning stream (`StreamDisplay`)
- `on_text(chunk)` → pauses spinner, streams live Rich Markdown + `tok/s` footer
- `on_tool_call(name, args)` / `on_tool_result(name, preview)` → prints tool badge and resumes the `Thinking` spinner between tool turns
- `on_usage(prompt_tok, output_tok)` → updates `TokenTracker` and `/status`
- `abort_signal` (`asyncio.Event`) → propagates `Ctrl+C` cancellation cleanly across ADK runner turns

---

## 4. Safety, Isolation & Mode Matrix

All agents share the same security perimeter enforced inside `WorkspaceTools` (`tools.py`), `scrubber.py`, and `extensions.py`:

| Mode (`/mode` or `Shift+Tab`) | Read / Search | File Edits (`apply_diff`, `write_file`) | Shell Commands (`run_shell_command`) |
| --- | --- | --- | --- |
| `plan` | Auto-allowed | Blocked (read-only) | Blocked |
| `ask` *(default)* | Auto-allowed | Prompt `[y/N/a]` | Prompt `[y/N/a]` |
| `auto-edit` | Auto-allowed | Auto-allowed (with rollback snapshot) | Prompt `[y/N/a]` |
| `full-auto` | Auto-allowed | Auto-allowed | Auto-allowed |

- **Secret Scrubbing**: Every prompt, tool result, history entry, log line, and report passes through `scrubber.scrub()` before leaving the local machine.
- **Protected Paths**: `.env*`, `.git/`, `.agent/`, `*.pem`, `*.key`, `id_rsa*`, and service-account JSON files are blocked from agent reads/writes.
- **Lifecycle Hooks**: `.agent/hooks.json` supports `pre_edit`, `post_edit`, `pre_shell`, and `post_shell` automation hooks.
- **Optional Shell Sandbox**: On macOS, `sandbox_shell: true` wraps shell execution in Apple Seatbelt (`sandbox-exec`).
