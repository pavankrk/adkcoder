# adk-coder (npm / npx wrapper)

Interactive Google ADK coding assistant CLI and lightweight Settings UI.

## Quick Start

Requires Python 3.11+ (or `uv`) installed on your machine. On first run, an isolated environment is created under `~/.local/share/adk-coder/` (or `%LOCALAPPDATA%\adk-coder\` on Windows).

```bash
# 1. Configure your LLM provider (OpenAI/Local, Ollama, Gemini, Anthropic) & test connectivity
npx adk-coder --config

# 2. Launch the interactive CLI in any workspace
npx adk-coder --workspace /path/to/project
```

### Windows PowerShell

```powershell
# Run directly with npx
npx .\adk-coder-0.1.0.tgz --config

# Or install globally with npm
npm install -g .\adk-coder-0.1.0.tgz
adk-coder --config
adk-coder
```

Or install globally (macOS / Linux):

```bash
npm install -g adk-coder
adk-coder --config
adk-coder
```
