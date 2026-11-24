"""Tests for enterprise SDLC scaffolding, --minimal mode, and AI /blueprint."""

import asyncio
import json

import pytest

from adk_coder.harness import AgentHarness
from adk_coder.scaffolder import STACKS
from adk_coder.security import security_scan


@pytest.mark.parametrize("stack", STACKS)
def test_enterprise_scaffold_generates_sdlc_solid_security_and_sbom(tmp_path, stack):
    h = AgentHarness(tmp_path, approve=lambda _: True)
    result = asyncio.run(h.execute(f"/scaffold {stack} billing-app"))
    assert "enterprise SDLC" in result

    dest = tmp_path / "billing-app"
    # Governance & planning artifacts
    assert (dest / "docs/PLAN.md").is_file()
    arch = (dest / "docs/ARCHITECTURE.md").read_text(encoding="utf8")
    assert "```mermaid" in arch
    assert (dest / "docs/adr/0001-initial-architecture.md").is_file()
    assert (dest / "docs/CODING_STANDARDS.md").is_file()
    assert (dest / "AGENTS.md").is_file()
    assert (dest / ".editorconfig").is_file()
    assert (dest / ".pre-commit-config.yaml").is_file()

    # CI pipelines (test, security, sbom)
    assert (dest / ".github/workflows/test.yml").is_file()
    sec_ci = (dest / ".github/workflows/security.yml").read_text(encoding="utf8")
    assert "gitleaks" in sec_ci
    sbom_ci = (dest / ".github/workflows/sbom.yml").read_text(encoding="utf8")
    assert "cyclonedx-json" in sbom_ci

    # SOLID Clean Architecture layers
    if stack in {"adk-python", "fastapi-service"}:
        assert (dest / "src/billing_app/domain/models.py").is_file()
        assert (dest / "src/billing_app/ports/repository.py").is_file()
        assert (dest / "src/billing_app/application/service.py").is_file()
        assert (dest / "src/billing_app/adapters/memory_repository.py").is_file()
        # Dogfood check: generated Python code passes our own SOLID & security scanner cleanly
        child = AgentHarness(dest, approve=lambda _: True)
        report = security_scan(child, "src")
        assert report["security_findings"] == []
        assert report["solid_findings"] == []
    elif stack == "adk-typescript":
        assert (dest / "tsconfig.json").is_file()
        assert (dest / "src/domain/models.ts").is_file()
        assert (dest / "src/ports/repository.ts").is_file()
        assert (dest / "src/application/service.ts").is_file()
        assert (dest / "src/adapters/memoryRepository.ts").is_file()
    elif stack == "java-spring":
        assert (dest / "pom.xml").is_file()
        assert (dest / "Dockerfile").is_file()
        assert (dest / "src/main/java/com/example/billing_app/Application.java").is_file()
        assert (dest / "src/main/java/com/example/billing_app/domain/WorkItem.java").is_file()
        assert (dest / "src/main/java/com/example/billing_app/ports/WorkItemRepository.java").is_file()
        assert (dest / "src/main/java/com/example/billing_app/application/WorkItemService.java").is_file()
        assert (dest / "src/main/java/com/example/billing_app/adapters/InMemoryWorkItemRepository.java").is_file()
        # Verify Maven SBOM discovery works on generated pom.xml
        child = AgentHarness(dest, approve=lambda _: True)
        asyncio.run(child.execute("/sbom"))
        sbom = json.loads((child.state / "sbom.cdx.json").read_text(encoding="utf8"))
        assert any("spring-boot-starter-web" in c["name"] for c in sbom["components"])
    else:
        assert (dest / ".golangci.yml").is_file()
        assert (dest / "internal/domain/models.go").is_file()
        assert (dest / "internal/ports/repository.go").is_file()
        assert (dest / "internal/application/service.go").is_file()
        assert (dest / "internal/adapters/memory_repository.go").is_file()


def test_scaffold_minimal_mode_skips_enterprise_docs(tmp_path):
    h = AgentHarness(tmp_path, approve=lambda _: True)
    result = asyncio.run(h.execute("/scaffold adk-python tiny-app --minimal"))
    assert "minimal" in result
    dest = tmp_path / "tiny-app"
    assert (dest / ".git").is_dir()
    assert (dest / ".github/workflows/test.yml").is_file()
    assert not (dest / "docs/PLAN.md").exists()
    assert not (dest / ".github/workflows/sbom.yml").exists()


def test_blueprint_synthesizes_custom_plan_architecture_and_adr(tmp_path, monkeypatch):
    h = AgentHarness(tmp_path, approve=lambda _: True)

    async def fake_ask(role, prompt):
        return json.dumps(
            {
                "plan_md": "# Plan for Order Service\n- Process orders reliably.",
                "architecture_md": "# Architecture\n```mermaid\nflowchart LR\nA-->B\n```",
                "adr_md": "# ADR-0001: Event-Driven Orders\nAccepted.",
            }
        )

    monkeypatch.setattr(h, "ask", fake_ask)
    asyncio.run(h.execute("/blueprint fastapi-service order-svc Build an order API"))
    dest = tmp_path / "order-svc"
    assert "Process orders reliably" in (dest / "docs/PLAN.md").read_text(encoding="utf8")
    assert "A-->B" in (dest / "docs/ARCHITECTURE.md").read_text(encoding="utf8")
    assert "Event-Driven Orders" in (
        dest / "docs/adr/0001-initial-architecture.md"
    ).read_text(encoding="utf8")
    assert (dest / ".github/workflows/security.yml").is_file()
    assert (dest / ".github/workflows/sbom.yml").is_file()


def test_requirements_intake_via_file_wizard_and_workflow(tmp_path, monkeypatch):
    from adk_coder.scaffolder import create_app

    (tmp_path / "requirements.md").write_text(
        "# Todo Microservice\nExpose CRUD endpoints for todos with priority and tags.\n",
        encoding="utf8",
    )
    h = AgentHarness(tmp_path, approve=lambda _: True)

    # 1. /scaffold --from requirements.md persists docs/REQUIREMENTS.md
    asyncio.run(h.execute("/scaffold java-spring todo-static --from requirements.md"))
    req_copy = (tmp_path / "todo-static/docs/REQUIREMENTS.md").read_text(encoding="utf8")
    assert "Expose CRUD endpoints for todos" in req_copy

    # 2. /blueprint @requirements.md reads file and writes docs/REQUIREMENTS.md + .agent/TASKS.md
    async def fake_ask(role, prompt):
        assert "Expose CRUD endpoints for todos" in prompt
        return json.dumps(
            {
                "plan_md": "# Todo Plan\n- CRUD APIs",
                "architecture_md": "# Arch\n```mermaid\nflowchart LR\nAPI-->Repo\n```",
                "adr_md": "# ADR-0001: Hexagonal Java\nAccepted.",
                "tasks": ["Create Todo domain entity", "Implement REST controller"],
            }
        )

    monkeypatch.setattr(h, "ask", fake_ask)
    asyncio.run(h.execute("/blueprint java-spring todo-ai @requirements.md"))
    ai_dest = tmp_path / "todo-ai"
    assert "Expose CRUD endpoints for todos" in (ai_dest / "docs/REQUIREMENTS.md").read_text(encoding="utf8")
    tasks_text = (ai_dest / ".agent/TASKS.md").read_text(encoding="utf8")
    assert "- [ ] Create Todo domain entity" in tasks_text
    assert "- [ ] Implement REST controller" in tasks_text

    # 3. /create interactive wizard with multi-line typed requirements
    answers = iter(
        [
            "java-spring",  # stack
            "todo-wizard",  # project name
            "apps",  # parent directory
            "n",  # don't reuse workspace requirements.md
            "",  # enter multi-line mode
            "Build a Todo REST API",
            "Support tags and due dates",
            "",  # end multi-line input
            "PostgreSQL in production",  # constraints
            "n",  # deterministic scaffold without LLM
        ]
    )
    asyncio.run(create_app(h, [], ask_input=lambda _: next(answers)))
    wiz_dest = tmp_path / "apps/todo-wizard"
    wiz_req = (wiz_dest / "docs/REQUIREMENTS.md").read_text(encoding="utf8")
    assert "Build a Todo REST API" in wiz_req
    assert "Support tags and due dates" in wiz_req
    assert "PostgreSQL in production" in wiz_req

    # 4. /workflow with no args auto-loads workspace requirements.md
    seen_workflow_req = []

    async def fake_workflow(req):
        seen_workflow_req.append(req)
        return "PASS"

    monkeypatch.setattr(h, "workflow", fake_workflow)
    assert asyncio.run(h.execute("/workflow")) == "PASS"
    assert "Expose CRUD endpoints for todos" in seen_workflow_req[0]
