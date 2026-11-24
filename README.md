# adk-coder

A Python CLI for developing inside a local workspace with Google ADK agents. Start a conversation for individual tasks or use the guided workflow for requirements, architecture, implementation, and review.

## Install and run

### Launch from the checkout

With uv and Git installed, run these commands from the repository root (the directory containing `run-adk-coder.sh`).

```bash
./run-adk-coder.sh                       # Start the interactive CLI in this directory
./run-adk-coder.sh --workspace /path/to/project
./run-adk-coder.sh --workspace /path/to/project /status
```

The launcher reuses the CLI installed in `adk-coder/.venv` if present. Otherwise uv installs the package and its LiteLLM extra on first use. It forwards CLI arguments and defaults to the directory where you invoke it. Enter `/help` for commands or `/exit` to quit. For Gemini, set `GOOGLE_API_KEY` before starting. For a running Ollama server, select its installed model with `/model ollama_chat/<model-name>`.

### Install the standalone command with pip

Requires Python 3.11+ and Git. From the `adk-coder/` package directory run:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
export GOOGLE_GENAI_USE_VERTEXAI=FALSE
# Set GOOGLE_API_KEY in your environment for Gemini.
adk-coder --workspace /path/to/project
```

Run a single command with `adk-coder --workspace /path/to/project /status`, or pass a quoted natural-language request. For Ollama, install `pip install -e '.[local]'`, start a local Ollama server, pull a tool-capable model, and enter `/model ollama_chat/qwen2.5-coder:14b`. Use `/model gemini-2.5-pro` or `/model gemini-2.5-flash` to switch back.

### OpenAI and OpenAI-compatible endpoints

Install the LiteLLM extra with `python -m pip install -e '.[local]'` if you installed with pip. The launcher’s uv installation includes it. Set `OPENAI_API_KEY` in the shell that starts the CLI. For the hosted OpenAI API, leave `OPENAI_API_BASE` unset. For a compatible Chat Completions server, set it to that server’s API root, for example `http://localhost:8000/v1`, and use a key for that server. If your local server disables authentication, a placeholder key satisfies the client.

From the repository root, select the model and start the CLI in the same workspace:

```bash
./run-adk-coder.sh --workspace /path/to/project /model openai/gpt-4o-mini
./run-adk-coder.sh --workspace /path/to/project
```

Replace `gpt-4o-mini` with the exact model ID your endpoint serves. The `openai/` prefix selects LiteLLM’s OpenAI protocol adapter. The workspace saves the model selection. To persist a custom endpoint for one workspace, set `openai_api_base` in its `.agent/config.json`. This setting overrides `OPENAI_API_BASE` and uses `ADK_CODER_ENDPOINT_API_KEY` for authentication, defaulting to a placeholder for servers without authentication. Choose a model and server that support tool calls for coding tasks.

## Command reference

| Command | Action |
| --- | --- |
| `/init`, `/status`, `/help` | Inspect workspace setup, task list, estimated memory usage, or available commands. |
| `/task [text]`, `/compact`, `/changelog` | Add or list tasks, summarize conversation memory, or inspect recent activity. |
| `/workflow <requirement>` | Review requirements and designs, approve architecture, then run standards and test planning concurrently before implementation and up to three review passes. |
| `/diff [--staged] [file]`, `/review` | Display a Git diff or ask the model to review tracked changes against HEAD. |
| `/branch [name]`, `/merge [target] [--squash\|--rebase]` | List or create branches. Integrate the current branch into the target (default main). Merge requires Git supporting merge-tree --write-tree. Squash leaves the result staged. |
| `/stash list\|push\|show\|apply\|pop\|drop` | Manage stashes; push accepts a message and show/apply/pop/drop accept a stash reference. |
| `/revert <file>`, `/undo <file>` | Preview and confirm restoring a tracked file from HEAD. |
| `/checkpoint create [label]\|list\|restore <id>` | Snapshot staged, unstaged and nonignored untracked content, tasks, and conversation memory. Restore from a clean tree at the same base commit. |
| `/sync`, `/sync --release [auto\|vX.Y.Z]` | Draft a PR with the model, or generate release notes from Git history. Interactive PR creation requires an authenticated gh CLI. Local tagging requires confirmation. |
| `/exit` | Close the session. |

## Workspace and terminal

Run Git commands from the target repository root. State is created under `.agent/` on first use. Keep this directory private and add it to the target repository’s ignore rules. Checkpoint patches contain original file contents. Configure desktop alerts with `"notifications": true` and a pre-merge test command with `"test_command": "python -m pytest"` in `.agent/config.json`.

The prompt displays the branch, dirty flag, and estimated context utilization. Ctrl+R searches history, Ctrl+K clears the screen, and Ctrl+S shows status. Install system completion with `adk-coder --install-completion bash` or `zsh`.

Edits and command execution require interactive approval. Commands execute with your local account permissions. Common credentials are masked in model inputs and audit entries. Review the data you share with your configured provider.

## Development

Install the dev extra, then run `python -m pytest` and `ruff check src tests` from this directory. Build a wheel with `python -m pip wheel --no-deps .`.

### Audit, repair, and project starters

Run these commands at the ADK Coder prompt with the target repository as the
workspace. Configure a model for semantic review and patch generation.

```text
/audit src
/fix 1
/scaffold adk-python customer-support-agent
```

`/audit [path]` (alias `/scan`) checks up to 100 source files. Python syntax
checks run locally. Install Ruff on PATH for Python lint and security checks,
or configure ESLint for JavaScript and TypeScript. The model reviews up to ten
excerpts, prioritizing static findings. The numbered report is saved under
`.agent/audit_cache.json`.

`/fix [all|index|file]` proposes exact replacements using the last report.
Approve each diff to run the workspace test command. Failed or declined tests
restore the file's pre-patch contents if it still matches the approved edit.
Set `test_command` in `.agent/config.json` for custom test suites.

`/scaffold` (alias `/new`) lists four starters: `adk-python`, `fastapi-service`,
`adk-typescript`, and `go-a2a`. Use `/new <template> <project-name> [parent-directory]`
to create a fresh Git project inside the workspace. Each generated README
contains the runtime prerequisites and install, test, and start commands.

### Run ADK Coder in Google ADK Web

From the repository root, use the existing local installation or install uv.
The launcher uses Python 3.11 or newer. Start it in the terminal that has your
model credentials and endpoint environment variables.

```sh
./run-adk-coder-web.sh "$PWD" 8000
```

Replace the first argument with an existing project directory to use that
workspace's saved model and files. Both arguments are optional. Open
`http://127.0.0.1:8000`, select `adk_coder_web`, and send `/status` or a coding
request. Approval prompts and intermediate output appear in the launching
terminal. Completed command output appears in the browser. Press Ctrl+C in
the terminal to stop the server.
