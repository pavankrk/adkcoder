"""Create enterprise-ready or minimal project starters with SOLID, security, and SBOM CI."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .audit import response_json
from .scrubber import scrub
from .spinner import spinning
from .ui import console, pause_live

# Each stack supplies its CI runtime, prerequisite, installation, test and launch commands.
STACKS = {
    "adk-python": (
        "python",
        "3.12",
        "python -m pip install -e '.[dev]'",
        "python -m pytest",
        "adk run assistant",
    ),
    "fastapi-service": (
        "python",
        "3.12",
        "python -m pip install -r requirements-dev.txt",
        "python -m pytest",
        "python -m uvicorn main:app",
    ),
    "adk-typescript": ("node", "20.19", "npm install", "npm test", "npm start"),
    "go-a2a": ("go", "1.25", "go mod tidy", "go test ./...", "go run ."),
    "java-spring": (
        "java",
        "21",
        "mvn -B -q test-compile",
        "mvn -B test",
        "mvn spring-boot:run",
    ),
}


def _architecture_diagram(stack: str, name: str) -> str:
    if stack == "adk-python":
        return f"""```mermaid
flowchart LR
    User["User / Caller"] --> Agent["ADK Agent & Workflow (assistant/)"]
    Agent --> App["Application Services (src/{name.replace('-', '_')}/application/)"]
    App --> Ports["Ports / Protocols (src/{name.replace('-', '_')}/ports/)"]
    App --> Domain["Domain Models (src/{name.replace('-', '_')}/domain/)"]
    Adapters["Adapters / Tools (src/{name.replace('-', '_')}/adapters/)"] -.implements.-> Ports
```"""
    if stack == "fastapi-service":
        return f"""```mermaid
flowchart LR
    Client["HTTP Client"] --> API["FastAPI Routes (main.py)"]
    API --> App["Application Use Cases (src/{name.replace('-', '_')}/application/)"]
    App --> Ports["Ports / Protocols (src/{name.replace('-', '_')}/ports/)"]
    App --> Domain["Domain Entities (src/{name.replace('-', '_')}/domain/)"]
    Adapters["Infrastructure Adapters (src/{name.replace('-', '_')}/adapters/)"] -.implements.-> Ports
```"""
    if stack == "adk-typescript":
        return """```mermaid
flowchart LR
    User["User / Caller"] --> Agent["ADK LlmAgent (agent.ts)"]
    Agent --> App["Application Services (src/application/)"]
    App --> Ports["Ports / Interfaces (src/ports/)"]
    App --> Domain["Domain Models (src/domain/)"]
    Adapters["Adapters (src/adapters/)"] -.implements.-> Ports
```"""
    if stack == "java-spring":
        return """```mermaid
flowchart LR
    Client["HTTP Client"] --> Web["Spring REST Adapters (adapters/in/web)"]
    Web --> App["Application Services (application/)"]
    App --> Ports["Port Interfaces (ports/)"]
    App --> Domain["Domain Entities (domain/)"]
    Persist["Persistence Adapters (adapters/out/persistence)"] -.implements.-> Ports
```"""
    return """```mermaid
flowchart LR
    Caller["A2A / HTTP Caller"] --> Server["ADK A2A Server (main.go)"]
    Server --> App["Application Services (internal/application/)"]
    App --> Ports["Ports / Interfaces (internal/ports/)"]
    App --> Domain["Domain Entities (internal/domain/)"]
    Adapters["Infrastructure Adapters (internal/adapters/)"] -.implements.-> Ports
```"""


def _solid_skeleton_files(stack: str, identifier: str) -> dict[str, str]:
    if stack in {"adk-python", "fastapi-service"}:
        base = f"src/{identifier}"
        return {
            f"{base}/__init__.py": f'"""Core Clean Architecture package for {identifier}."""\n',
            f"{base}/domain/__init__.py": "",
            f"{base}/domain/models.py": (
                '"""Pure domain entities and value objects (no framework or I/O imports)."""\n\n'
                "from dataclasses import dataclass\n\n\n"
                "@dataclass(frozen=True, slots=True)\n"
                "class WorkItem:\n"
                "    id: str\n"
                "    title: str\n"
                "    completed: bool = False\n"
            ),
            f"{base}/ports/__init__.py": "",
            f"{base}/ports/repository.py": (
                '"""Abstract ports (Dependency Inversion & Interface Segregation)."""\n\n'
                "from typing import Protocol\n\n"
                f"from {identifier}.domain.models import WorkItem\n\n\n"
                "class WorkItemRepository(Protocol):\n"
                "    def save(self, item: WorkItem) -> None: ...\n"
                "    def get(self, item_id: str) -> WorkItem | None: ...\n"
                "    def list_all(self) -> list[WorkItem]: ...\n"
            ),
            f"{base}/application/__init__.py": "",
            f"{base}/application/service.py": (
                '"""Single-responsibility application use cases depending only on ports."""\n\n'
                f"from {identifier}.domain.models import WorkItem\n"
                f"from {identifier}.ports.repository import WorkItemRepository\n\n\n"
                "class WorkItemService:\n"
                "    def __init__(self, repository: WorkItemRepository) -> None:\n"
                "        self._repository = repository\n\n"
                "    def create(self, item_id: str, title: str) -> WorkItem:\n"
                "        clean_title = title.strip()\n"
                "        if not clean_title:\n"
                '            raise ValueError("Title must not be empty.")\n'
                "        item = WorkItem(id=item_id, title=clean_title)\n"
                "        self._repository.save(item)\n"
                "        return item\n\n"
                "    def list_items(self) -> list[WorkItem]:\n"
                "        return self._repository.list_all()\n"
            ),
            f"{base}/adapters/__init__.py": "",
            f"{base}/adapters/memory_repository.py": (
                '"""Concrete in-memory adapter implementing WorkItemRepository."""\n\n'
                f"from {identifier}.domain.models import WorkItem\n\n\n"
                "class InMemoryWorkItemRepository:\n"
                "    def __init__(self) -> None:\n"
                "        self._items: dict[str, WorkItem] = {}\n\n"
                "    def save(self, item: WorkItem) -> None:\n"
                "        self._items[item.id] = item\n\n"
                "    def get(self, item_id: str) -> WorkItem | None:\n"
                "        return self._items.get(item_id)\n\n"
                "    def list_all(self) -> list[WorkItem]:\n"
                "        return list(self._items.values())\n"
            ),
        }

    if stack == "adk-typescript":
        return {
            "tsconfig.json": json.dumps(
                {
                    "compilerOptions": {
                        "target": "ES2022",
                        "module": "NodeNext",
                        "moduleResolution": "NodeNext",
                        "strict": True,
                        "noUncheckedIndexedAccess": True,
                        "noImplicitOverride": True,
                        "skipLibCheck": True,
                        "outDir": "dist",
                    },
                    "include": ["*.ts", "src/**/*.ts"],
                },
                indent=2,
            )
            + "\n",
            "src/domain/models.ts": (
                "// Pure domain types (zero framework or I/O dependencies).\n"
                "export interface WorkItem {\n"
                "  readonly id: string;\n"
                "  readonly title: string;\n"
                "  readonly completed: boolean;\n"
                "}\n"
            ),
            "src/ports/repository.ts": (
                "import type { WorkItem } from '../domain/models.js';\n\n"
                "// Dependency Inversion port interface.\n"
                "export interface WorkItemRepository {\n"
                "  save(item: WorkItem): void;\n"
                "  get(id: string): WorkItem | undefined;\n"
                "  listAll(): WorkItem[];\n"
                "}\n"
            ),
            "src/application/service.ts": (
                "import type { WorkItem } from '../domain/models.js';\n"
                "import type { WorkItemRepository } from '../ports/repository.js';\n\n"
                "export class WorkItemService {\n"
                "  constructor(private readonly repository: WorkItemRepository) {}\n\n"
                "  create(id: string, title: string): WorkItem {\n"
                "    const trimmed = title.trim();\n"
                "    if (!trimmed) {\n"
                "      throw new Error('Title must not be empty.');\n"
                "    }\n"
                "    const item: WorkItem = { id, title: trimmed, completed: false };\n"
                "    this.repository.save(item);\n"
                "    return item;\n"
                "  }\n\n"
                "  listItems(): WorkItem[] {\n"
                "    return this.repository.listAll();\n"
                "  }\n"
                "}\n"
            ),
            "src/adapters/memoryRepository.ts": (
                "import type { WorkItem } from '../domain/models.js';\n"
                "import type { WorkItemRepository } from '../ports/repository.js';\n\n"
                "export class InMemoryWorkItemRepository implements WorkItemRepository {\n"
                "  private readonly store = new Map<string, WorkItem>();\n\n"
                "  save(item: WorkItem): void {\n"
                "    this.store.set(item.id, item);\n"
                "  }\n\n"
                "  get(id: string): WorkItem | undefined {\n"
                "    return this.store.get(id);\n"
                "  }\n\n"
                "  listAll(): WorkItem[] {\n"
                "    return Array.from(this.store.values());\n"
                "  }\n"
                "}\n"
            ),
        }

    if stack == "java-spring":
        pkg = f"com.example.{identifier}"
        base = f"src/main/java/com/example/{identifier}"
        return {
            f"{base}/domain/WorkItem.java": (
                f"package {pkg}.domain;\n\n"
                "// Pure domain record with zero framework dependencies.\n"
                "public record WorkItem(String id, String title, boolean completed) {}\n"
            ),
            f"{base}/ports/WorkItemRepository.java": (
                f"package {pkg}.ports;\n\n"
                f"import {pkg}.domain.WorkItem;\n"
                "import java.util.List;\n"
                "import java.util.Optional;\n\n"
                "public interface WorkItemRepository {\n"
                "    void save(WorkItem item);\n"
                "    Optional<WorkItem> findById(String id);\n"
                "    List<WorkItem> findAll();\n"
                "}\n"
            ),
            f"{base}/application/WorkItemService.java": (
                f"package {pkg}.application;\n\n"
                f"import {pkg}.domain.WorkItem;\n"
                f"import {pkg}.ports.WorkItemRepository;\n"
                "import java.util.List;\n\n"
                "public class WorkItemService {\n"
                "    private final WorkItemRepository repository;\n\n"
                "    public WorkItemService(WorkItemRepository repository) {\n"
                "        this.repository = repository;\n"
                "    }\n\n"
                "    public WorkItem create(String id, String title) {\n"
                "        String clean = title == null ? \"\" : title.trim();\n"
                "        if (clean.isEmpty()) {\n"
                "            throw new IllegalArgumentException(\"Title must not be empty\");\n"
                "        }\n"
                "        WorkItem item = new WorkItem(id, clean, false);\n"
                "        repository.save(item);\n"
                "        return item;\n"
                "    }\n\n"
                "    public List<WorkItem> listAll() {\n"
                "        return repository.findAll();\n"
                "    }\n"
                "}\n"
            ),
            f"{base}/adapters/InMemoryWorkItemRepository.java": (
                f"package {pkg}.adapters;\n\n"
                f"import {pkg}.domain.WorkItem;\n"
                f"import {pkg}.ports.WorkItemRepository;\n"
                "import java.util.ArrayList;\n"
                "import java.util.List;\n"
                "import java.util.Map;\n"
                "import java.util.Optional;\n"
                "import java.util.concurrent.ConcurrentHashMap;\n\n"
                "public class InMemoryWorkItemRepository implements WorkItemRepository {\n"
                "    private final Map<String, WorkItem> store = new ConcurrentHashMap<>();\n\n"
                "    @Override\n"
                "    public void save(WorkItem item) {\n"
                "        store.put(item.id(), item);\n"
                "    }\n\n"
                "    @Override\n"
                "    public Optional<WorkItem> findById(String id) {\n"
                "        return Optional.ofNullable(store.get(id));\n"
                "    }\n\n"
                "    @Override\n"
                "    public List<WorkItem> findAll() {\n"
                "        return new ArrayList<>(store.values());\n"
                "    }\n"
                "}\n"
            ),
        }

    # go-a2a
    return {
        ".golangci.yml": (
            "linters:\n"
            "  enable:\n"
            "    - errcheck\n"
            "    - gocyclo\n"
            "    - gosec\n"
            "    - govet\n"
            "    - revive\n"
            "    - staticcheck\n"
            "linters-settings:\n"
            "  gocyclo:\n"
            "    min-complexity: 10\n"
        ),
        "internal/domain/models.go": (
            "package domain\n\n"
            "// WorkItem is a pure domain entity with zero infrastructure dependencies.\n"
            "type WorkItem struct {\n"
            "\tID        string\n"
            "\tTitle     string\n"
            "\tCompleted bool\n"
            "}\n"
        ),
        "internal/ports/repository.go": (
            "package ports\n\n"
            f'import "{identifier}/internal/domain"\n\n'
            "// WorkItemRepository defines the persistence port (Dependency Inversion).\n"
            "type WorkItemRepository interface {\n"
            "\tSave(item domain.WorkItem)\n"
            "\tGet(id string) (domain.WorkItem, bool)\n"
            "\tListAll() []domain.WorkItem\n"
            "}\n"
        ),
        "internal/application/service.go": (
            "package application\n\n"
            "import (\n"
            '\t"errors"\n'
            '\t"strings"\n\n'
            f'\t"{identifier}/internal/domain"\n'
            f'\t"{identifier}/internal/ports"\n'
            ")\n\n"
            "type WorkItemService struct {\n"
            "\trepo ports.WorkItemRepository\n"
            "}\n\n"
            "func NewWorkItemService(repo ports.WorkItemRepository) *WorkItemService {\n"
            "\treturn &WorkItemService{repo: repo}\n"
            "}\n\n"
            "func (s *WorkItemService) Create(id, title string) (domain.WorkItem, error) {\n"
            "\ttrimmed := strings.TrimSpace(title)\n"
            '\tif trimmed == "" {\n'
            '\t\treturn domain.WorkItem{}, errors.New("title must not be empty")\n'
            "\t}\n"
            "\titem := domain.WorkItem{ID: id, Title: trimmed}\n"
            "\ts.repo.Save(item)\n"
            "\treturn item, nil\n"
            "}\n"
        ),
        "internal/adapters/memory_repository.go": (
            "package adapters\n\n"
            f'import "{identifier}/internal/domain"\n\n'
            "type InMemoryWorkItemRepository struct {\n"
            "\titems map[string]domain.WorkItem\n"
            "}\n\n"
            "func NewInMemoryWorkItemRepository() *InMemoryWorkItemRepository {\n"
            "\treturn &InMemoryWorkItemRepository{items: make(map[string]domain.WorkItem)}\n"
            "}\n\n"
            "func (r *InMemoryWorkItemRepository) Save(item domain.WorkItem) {\n"
            "\tr.items[item.ID] = item\n"
            "}\n\n"
            "func (r *InMemoryWorkItemRepository) Get(id string) (domain.WorkItem, bool) {\n"
            "\titem, ok := r.items[id]\n"
            "\treturn item, ok\n"
            "}\n\n"
            "func (r *InMemoryWorkItemRepository) ListAll() []domain.WorkItem {\n"
            "\tout := make([]domain.WorkItem, 0, len(r.items))\n"
            "\tfor _, item := range r.items {\n"
            "\t\tout = append(out, item)\n"
            "\t}\n"
            "\treturn out\n"
            "}\n"
        ),
    }


def _security_workflow(stack: str, runtime: str, version: str, install: str) -> str:
    if runtime == "python":
        sast_steps = (
            "      - name: Python SAST & dependency CVE audit\n"
            "        run: |\n"
            "          python -m pip install ruff pip-audit\n"
            "          ruff check --select S,C90 .\n"
            "          pip-audit\n"
        )
    elif runtime == "node":
        sast_steps = (
            "      - name: Node dependency CVE audit\n"
            "        run: npm audit --audit-level=high || true\n"
        )
    elif runtime == "java":
        sast_steps = (
            "      - name: Maven verify & CycloneDX SBOM check\n"
            "        run: mvn -B -q test\n"
        )
    else:
        sast_steps = (
            "      - name: Go vet & vulnerability audit\n"
            "        run: |\n"
            "          go vet ./...\n"
            "          go install golang.org/x/vuln/cmd/govulncheck@latest\n"
            "          govulncheck ./...\n"
        )
    docker_step = (
        "      - name: Validate Dockerfile hardening\n"
        "        uses: hadolint/hadolint-action@v3.1.0\n"
        "        with:\n"
        "          dockerfile: Dockerfile\n"
        if stack in {"fastapi-service", "java-spring"}
        else ""
    )
    setup_extra = (
        "          distribution: 'temurin'\n" if runtime == "java" else ""
    )
    return f"""name: Security Scans
on: [push, pull_request]
permissions:
  contents: read
jobs:
  security:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Secret scanning (gitleaks)
        uses: gitleaks/gitleaks-action@v2
        env:
          GITHUB_TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}
      - uses: actions/setup-{runtime}@v5
        with:
          {runtime}-version: '{version}'
{setup_extra}      - run: {install}
{sast_steps}{docker_step}"""


def _sbom_workflow(name: str) -> str:
    return f"""name: SBOM Generation
on:
  push:
    branches: [main]
  pull_request:
  release:
    types: [published]
permissions:
  contents: read
jobs:
  sbom:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Generate CycloneDX SBOM
        uses: anchore/sbom-action@v0
        with:
          format: cyclonedx-json
          output-file: {name}-sbom.cdx.json
      - name: Upload SBOM artifact
        uses: actions/upload-artifact@v4
        with:
          name: {name}-sbom
          path: {name}-sbom.cdx.json
"""


def _enterprise_files(
    stack: str,
    name: str,
    identifier: str,
    runtime: str,
    version: str,
    install: str,
    test: str,
    run_cmd: str,
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    custom = overrides or {}
    plan_md = custom.get("plan_md") or (
        f"# Project Plan — {name}\n\n"
        "## 1. Objective\n"
        f"Describe the core user problem `{name}` solves and measurable success criteria.\n\n"
        "## 2. Scope & Non-Goals\n"
        "- **In Scope**: Core domain workflows, unit/integration tests, security & SBOM automation.\n"
        "- **Out of Scope**: Unvalidated experimental dependencies or ad-hoc production shortcuts.\n\n"
        "## 3. Milestones\n"
        "- [ ] M1: Define domain entities & port interfaces (`domain/`, `ports/`).\n"
        "- [ ] M2: Implement core application use cases & unit tests (`application/`, `tests/`).\n"
        "- [ ] M3: Wire adapters, observability, security scan (`/security`), and SBOM (`/sbom`).\n\n"
        "## 4. Acceptance Criteria\n"
        f"- `{test}` passes with zero failures.\n"
        "- `/security` reports zero CRITICAL/HIGH vulnerabilities or hardcoded secrets.\n"
        "- `/sbom` generates a valid CycloneDX SBOM with approved dependency licenses.\n"
    )
    architecture_md = custom.get("architecture_md") or (
        f"# Architecture — {name}\n\n"
        "## High-Level Design\n\n"
        f"{_architecture_diagram(stack, name)}\n\n"
        "## Layer Boundaries (SOLID / Clean Architecture)\n"
        "1. **Domain (`domain/`)**: Pure business entities and invariants. Zero framework, HTTP, or database imports.\n"
        "2. **Ports (`ports/`)**: Small, focused interfaces/protocols defining external contracts (Interface Segregation & Dependency Inversion).\n"
        "3. **Application (`application/`)**: Use-case orchestration depending exclusively on `domain/` and `ports/`.\n"
        "4. **Adapters (`adapters/` + entrypoints)**: Concrete frameworks, persistence, LLM/ADK tools, and external APIs.\n\n"
        "## Security & Trust Boundaries\n"
        "- Validate all external inputs at adapter boundaries.\n"
        "- Never commit credentials; load secrets via environment variables or secret managers.\n"
        "- Keep dependencies minimal and auditable via automated SBOM generation.\n"
    )
    adr_md = custom.get("adr_md") or (
        f"# ADR-0001: Initial Architecture for {name}\n\n"
        "- **Status**: Accepted\n"
        f"- **Stack**: `{stack}` ({runtime} {version})\n\n"
        "## Context\n"
        f"`{name}` requires a maintainable, testable foundation with clear separation of concerns and automated security/supply-chain governance.\n\n"
        "## Decision\n"
        "Adopt a layered Clean Architecture aligned with SOLID principles (`domain`, `ports`, `application`, `adapters`), automated SAST/secret/CVE CI checks, and CycloneDX SBOM generation.\n\n"
        "## Consequences\n"
        "- Domain logic can be unit-tested without external infrastructure.\n"
        "- Concrete adapters can be swapped without modifying core business rules.\n"
        "- Security regressions and unapproved licenses fail early in CI.\n"
    )
    coding_standards_md = (
        f"# Coding Standards & SOLID Checklist — {name}\n\n"
        "## SOLID Principles\n"
        "- **S — Single Responsibility**: Keep modules <= 400 lines, classes <= 12 methods, functions <= 60 lines.\n"
        "- **O — Open/Closed**: Extend behavior through new port implementations rather than giant `if/elif` chains.\n"
        "- **L — Liskov Substitution**: Adapter implementations must honor port contracts and error semantics.\n"
        "- **I — Interface Segregation**: Prefer small, cohesive interfaces and <= 6 parameters per function.\n"
        "- **D — Dependency Inversion**: `domain/` and `application/` depend only on abstractions (`ports/`), never concrete I/O libraries.\n\n"
        "## Quality & Security Gates\n"
        "- Keep cyclomatic complexity <= 10 per function.\n"
        "- Never use `eval`/`exec`, `pickle.loads`, `yaml.load` without SafeLoader, `subprocess(..., shell=True)`, or string-interpolated SQL.\n"
        "- Run `adk-coder /security` and `adk-coder /sbom` before opening a pull request.\n"
    )
    agents_md = (
        f"# Agent Instructions — {name}\n\n"
        "Read `docs/PLAN.md`, `docs/ARCHITECTURE.md`, and `docs/CODING_STANDARDS.md` before proposing changes.\n\n"
        "## Commands\n"
        f"- Install: `{install}`\n"
        f"- Test: `{test}`\n"
        f"- Run: `{run_cmd}`\n"
        "- Security & SOLID scan: `adk-coder /security`\n"
        "- SBOM generation: `adk-coder /sbom`\n\n"
        "## Rules\n"
        "- Preserve SOLID boundaries: never import concrete HTTP/DB/framework packages in `domain/`.\n"
        "- Add or update tests for every behavioral change.\n"
        "- Never hardcode secrets or API keys.\n"
    )
    editorconfig = (
        "root = true\n\n"
        "[*]\n"
        "charset = utf-8\n"
        "end_of_line = lf\n"
        "insert_final_newline = true\n"
        "trim_trailing_whitespace = true\n"
        "indent_style = space\n"
        "indent_size = 4\n\n"
        "[*.{ts,tsx,js,jsx,json,yml,yaml}]\n"
        "indent_size = 2\n\n"
        "[*.go]\n"
        "indent_style = tab\n"
    )
    pre_commit = (
        "repos:\n"
        "  - repo: https://github.com/pre-commit/pre-commit-hooks\n"
        "    rev: v5.0.0\n"
        "    hooks:\n"
        "      - id: check-merge-conflict\n"
        "      - id: end-of-file-fixer\n"
        "      - id: trailing-whitespace\n"
        "      - id: detect-private-key\n"
        "  - repo: https://github.com/gitleaks/gitleaks\n"
        "    rev: v8.24.2\n"
        "    hooks:\n"
        "      - id: gitleaks\n"
    )

    files: dict[str, str] = {
        "docs/PLAN.md": plan_md,
        "docs/ARCHITECTURE.md": architecture_md,
        "docs/adr/0001-initial-architecture.md": adr_md,
        "docs/CODING_STANDARDS.md": coding_standards_md,
        "AGENTS.md": agents_md,
        ".editorconfig": editorconfig,
        ".pre-commit-config.yaml": pre_commit,
        ".github/workflows/security.yml": _security_workflow(stack, runtime, version, install),
        ".github/workflows/sbom.yml": _sbom_workflow(name),
    }
    if custom.get("requirements_md"):
        files["docs/REQUIREMENTS.md"] = custom["requirements_md"]
    if custom.get("tasks_md"):
        files[".agent/TASKS.md"] = custom["tasks_md"]
    files.update(_solid_skeleton_files(stack, identifier))
    return files


def resolve_requirements(h, raw: str) -> str:
    """Load requirement text from `@file`, `--from <file>`, a file path, or inline text."""
    text = (raw or "").strip()
    if not text:
        for candidate in ("requirements.md", "REQUIREMENTS.md", "docs/REQUIREMENTS.md"):
            path = h.root / candidate
            if path.is_file():
                return path.read_text(encoding="utf8").strip()
        return ""
    if text.startswith("--from "):
        target = text[len("--from ") :].strip().strip('"').strip("'")
        return h.tools.path(target).read_text(encoding="utf8").strip()
    if text.startswith("@") and " " not in text:
        return h.tools.path(text[1:]).read_text(encoding="utf8").strip()
    if "\n" not in text and " " not in text and text.endswith((".md", ".txt")):
        candidate_path = h.root / text
        if candidate_path.is_file():
            return h.tools.path(text).read_text(encoding="utf8").strip()
    return text


def scaffold(h, args: list[str], *, overrides: dict[str, str] | None = None) -> str:
    minimal = "--minimal" in args
    req_file = ""
    clean_args: list[str] = []
    idx = 0
    while idx < len(args):
        arg = args[idx]
        if arg == "--minimal":
            idx += 1
            continue
        if arg == "--from" and idx + 1 < len(args):
            req_file = args[idx + 1]
            idx += 2
            continue
        clean_args.append(arg)
        idx += 1

    if not clean_args:
        return (
            "Usage: /scaffold <template> <project-name> [parent-directory] [--from <requirements.md>] [--minimal]\n"
            + "\n".join(STACKS)
        )
    if len(clean_args) not in (2, 3) or clean_args[0] not in STACKS:
        raise ValueError("Use /scaffold to list templates and arguments.")
    stack, name = clean_args[:2]
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name):
        raise ValueError("Use a lowercase project name starting with a letter.")
    dest = h.tools.path(Path(clean_args[2] if len(clean_args) == 3 else ".") / name)
    if dest.exists():
        raise FileExistsError(dest)

    merged_overrides = dict(overrides or {})
    if req_file and "requirements_md" not in merged_overrides:
        req_text = h.tools.path(req_file).read_text(encoding="utf8").strip()
        merged_overrides["requirements_md"] = (
            f"# Requirements — {name}\n\n{scrub(req_text)}\n"
        )

    runtime, version, install, test, run_cmd = STACKS[stack]
    identifier = name.replace("-", "_")
    template = Path(__file__).with_name("templates") / stack
    files = {
        str(p.relative_to(template).with_suffix("")).replace(
            "com/example/app/", f"com/example/{identifier}/"
        ): p.read_text(encoding="utf8")
        for p in template.rglob("*.tmpl")
    }
    if not files:
        raise RuntimeError(f"Missing packaged template {stack}")

    prerequisite = f"{runtime} {version}"
    if stack not in {"fastapi-service", "java-spring"}:
        key = "GEMINI_API_KEY" if stack == "adk-typescript" else "GOOGLE_API_KEY"
        prerequisite += f" and {key} exported for live requests"
        files[".env.example"] = f"{key}=\n"

    config = {k: h.config[k] for k in ("model", "openai_api_base") if k in h.config}
    config.update(workspace_root=str(dest), notifications=False, test_command=test)

    readme_extra = (
        "\n## Engineering Blueprint\n"
        "- Planning: `docs/PLAN.md`\n"
        "- Architecture & ADRs: `docs/ARCHITECTURE.md`, `docs/adr/0001-initial-architecture.md`\n"
        "- SOLID & Coding Standards: `docs/CODING_STANDARDS.md`, `AGENTS.md`\n"
        "- Security & SBOM CI: `.github/workflows/security.yml`, `.github/workflows/sbom.yml`\n"
        if not minimal
        else ""
    )
    setup_dist = "          distribution: 'temurin'\n" if runtime == "java" else ""

    files.update(
        {
            ".agent/config.json": json.dumps(config, indent=2) + "\n",
            ".gitignore": ".agent/\n.env\n.venv/\n__pycache__/\n.pytest_cache/\n*.egg-info/\nnode_modules/\ndist/\ntarget/\n",
            "README.md": (
                f"# {name}\n\nRequires {prerequisite}. From this directory run\n\n"
                f"```sh\n{install}\n{test}\n{run_cmd}\n```\n{readme_extra}"
            ),
            ".github/workflows/test.yml": f"""name: Tests
on: [push, pull_request]
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-{runtime}@v5
        with:
          {runtime}-version: '{version}'
{setup_dist}      - run: {install}
      - run: {test}
""",
        }
    )

    if not minimal:
        files.update(
            _enterprise_files(
                stack,
                name,
                identifier,
                runtime,
                version,
                install,
                test,
                run_cmd,
                overrides=merged_overrides,
            )
        )
    elif merged_overrides.get("requirements_md"):
        files["docs/REQUIREMENTS.md"] = merged_overrides["requirements_md"]

    mode_label = "minimal" if minimal else "enterprise SDLC"
    if not h.approve(
        f"Create {dest} ({mode_label} {stack} template, {len(files)} files) and initialize Git?"
    ):
        return "Scaffold declined."

    dest.mkdir(parents=True, exist_ok=False)
    try:
        for rel, content in files.items():
            path = dest / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            rendered = content.replace("{name}", name).replace("{identifier}", identifier)
            path.write_text(rendered, encoding="utf8")
        subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    except BaseException:
        shutil.rmtree(dest)
        raise
    return (
        f"Created {dest} ({mode_label} blueprint, {len(files)} files)\n"
        f"See {dest / 'README.md'} for install, test, security, and SBOM commands."
    )


async def blueprint(h, args: list[str], *, parent: str = "") -> str:
    """AI-guided planning, architecture, ADR, tasks, and enterprise scaffold generator."""
    if len(args) < 2 or args[0] not in STACKS:
        raise ValueError(
            "Usage: /blueprint <template> <project-name> [requirement|@requirements.md|--from <file>]\n"
            "Templates: " + ", ".join(STACKS)
        )
    stack, name = args[0], args[1]
    raw_req = " ".join(args[2:]).strip()
    requirement = resolve_requirements(h, raw_req)
    if not requirement:
        raise ValueError(
            "Provide a requirement, `@requirements.md`, `--from <file>`, or create `requirements.md` in the workspace."
        )
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name):
        raise ValueError("Use a lowercase project name starting with a letter.")

    prompt = (
        f"Project name: {name}\n"
        f"Stack: {stack}\n"
        f"Requirements:\n{requirement}\n\n"
        "Return a JSON object with keys:\n"
        "- `plan_md`: structured project plan with milestones and acceptance criteria\n"
        "- `architecture_md`: Mermaid flowchart + SOLID Clean Architecture layer mapping "
        "(domain/ports/application/adapters) + security/threat boundaries\n"
        "- `adr_md`: ADR-0001 documenting key architectural decisions and tradeoffs\n"
        "- `tasks`: list of 5-10 concrete implementation task strings"
    )
    overrides: dict[str, str] = {
        "requirements_md": f"# Requirements — {name}\n\n{scrub(requirement)}\n"
    }
    with spinning(f"Drafting architecture blueprint for {name}"):
        raw = await h.ask(
            "You are a principal software architect. Design a clean SOLID architecture, "
            "actionable plan, ADR-0001, and ordered implementation tasks. "
            "Return valid JSON with keys plan_md, architecture_md, adr_md, tasks.",
            prompt,
        )
    try:
        parsed = response_json(raw)
        for key in ("plan_md", "architecture_md", "adr_md"):
            if isinstance(parsed.get(key), str) and parsed[key].strip():
                overrides[key] = scrub(parsed[key].strip()) + "\n"
        tasks = parsed.get("tasks")
        if isinstance(tasks, list):
            task_lines = [
                f"- [ ] {scrub(str(item).strip())}"
                for item in tasks
                if str(item).strip()
            ]
            if task_lines:
                overrides["tasks_md"] = "# Tasks\n" + "\n".join(task_lines) + "\n"
    except (ValueError, TypeError):
        overrides["plan_md"] = f"# Project Plan — {name}\n\n{scrub(raw).strip()}\n"

    if overrides.get("plan_md"):
        console.print(overrides["plan_md"][:1200])
    target_args = [stack, name, parent] if parent else [stack, name]
    return scaffold(h, target_args, overrides=overrides)


def _default_ask_input(prompt_text: str) -> str:
    with pause_live():
        return input(prompt_text)


async def create_app(h, args: list[str], ask_input=_default_ask_input) -> str:
    """Interactive wizard to capture requirements and scaffold a new application from scratch."""
    from_file = ""
    idx = 0
    while idx < len(args):
        if args[idx] == "--from" and idx + 1 < len(args):
            from_file = args[idx + 1]
            idx += 2
        elif args[idx].startswith("@"):
            from_file = args[idx][1:]
            idx += 1
        else:
            idx += 1

    stack_list = list(STACKS.keys())
    console.print("[bold cyan]New Application Wizard[/]")
    for num, stack_name in enumerate(stack_list, 1):
        runtime, version, *_ = STACKS[stack_name]
        console.print(f"  [bold]{num}.[/] [cyan]{stack_name}[/] [dim]({runtime} {version})[/]")

    stack_choice = ask_input(f"Select stack [1-{len(stack_list)} or name, default 1]: ").strip()
    if not stack_choice:
        stack = stack_list[0]
    elif stack_choice.isdecimal() and 1 <= int(stack_choice) <= len(stack_list):
        stack = stack_list[int(stack_choice) - 1]
    elif stack_choice in STACKS:
        stack = stack_choice
    else:
        raise ValueError(f"Unknown stack {stack_choice!r}. Choose one of: {', '.join(stack_list)}.")

    name = ask_input("Project name (lowercase, e.g. todo-service): ").strip()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name):
        raise ValueError("Use a lowercase project name starting with a letter (a-z, 0-9, -).")

    parent = ask_input("Parent directory [default .]: ").strip()

    requirement = ""
    if from_file:
        requirement = h.tools.path(from_file).read_text(encoding="utf8").strip()
        console.print(f"[green]Loaded requirements from {from_file} ({len(requirement)} chars).[/]")
    else:
        detected = next(
            (c for c in ("requirements.md", "REQUIREMENTS.md") if (h.root / c).is_file()),
            "",
        )
        if detected:
            use_detected = ask_input(f"Found {detected}. Use it as requirements? [Y/n]: ").strip().lower()
            if use_detected in {"", "y", "yes"}:
                requirement = (h.root / detected).read_text(encoding="utf8").strip()

        if not requirement:
            req_input = ask_input(
                "Requirements (enter a file path like requirements.md, or press Enter to type multi-line): "
            ).strip()
            if req_input:
                requirement = resolve_requirements(h, req_input)
            else:
                console.print(
                    "[dim]Enter your requirements below. Submit an empty line when finished:[/]"
                )
                lines: list[str] = []
                while True:
                    try:
                        entry = ask_input("> ")
                    except EOFError:
                        break
                    if not entry.strip():
                        break
                    lines.append(entry)
                requirement = "\n".join(lines).strip()

    constraints = ask_input(
        "Optional constraints (DB, auth, ports, non-functional reqs; Enter to skip): "
    ).strip()
    if constraints:
        requirement = (
            f"{requirement}\n\nAdditional Constraints:\n{constraints}"
            if requirement
            else constraints
        )

    use_ai = ask_input(
        "Generate AI-tailored PLAN.md, ARCHITECTURE.md, ADR & tasks from requirements? [Y/n]: "
    ).strip().lower()

    if use_ai in {"", "y", "yes"} and requirement:
        return await blueprint(h, [stack, name, requirement], parent=parent)

    scaffold_args = [stack, name]
    if parent:
        scaffold_args.append(parent)
    overrides = (
        {"requirements_md": f"# Requirements — {name}\n\n{scrub(requirement)}\n"}
        if requirement
        else None
    )
    return scaffold(h, scaffold_args, overrides=overrides)
