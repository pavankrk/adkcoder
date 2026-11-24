import asyncio
import subprocess

import pytest

from adk_coder.harness import AgentHarness
from adk_coder.semver import next_version


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root)


@pytest.fixture
def workspace(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "code").write_text("base\n")
    git(tmp_path, "add", "code")
    git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.org",
        "commit",
        "-qm",
        "feat: start",
    )
    return AgentHarness(tmp_path, lambda _: True)


def run(h, line):
    return asyncio.run(h.execute(line))


def test_snapshot(workspace):
    h = workspace
    code = h.root / "code"
    code.write_text("staged\n")
    git(h.root, "add", "code")
    code.write_text("unstaged\n")
    (h.root / "new").write_bytes(b"\x00\xff")
    run(h, "/task first")
    key = run(h, "/checkpoint create initial")
    run(h, "/stash push temp")
    run(h, "/task second")
    run(h, "/checkpoint restore " + key)
    assert git(h.root, "show", ":code") == b"staged\n"
    assert code.read_text() == "unstaged\n"
    assert (h.root / "new").read_bytes() == b"\x00\xff"
    assert "second" not in run(h, "/task")


def test_tools(workspace):
    h = workspace
    with pytest.raises(ValueError):
        h.tools.read_file("../outside")
    h.tools.approve = lambda _: False
    assert h.tools.apply_diff("code", "base", "changed") == "Edit declined."
    assert h.tools.run_shell_command("whoami") == "Command declined."
    h.tools.approve = lambda _: True
    assert h.tools.apply_diff("sub/new.txt", "", "api_key=abcdef") == "Saved."
    assert "abcdef" not in h.tools.read_file("sub/new.txt")


def test_workflow(workspace, monkeypatch):
    calls = []

    async def fake(model, instruction, prompt, tools):
        calls.append(instruction)
        return "PASS verified" if instruction.startswith("Review") else "artifact"

    monkeypatch.setattr("adk_coder.harness.generate", fake)
    workspace.approve = lambda _: False
    assert "stopped" in run(workspace, "/workflow demo")
    assert len(calls) == 3
    workspace.approve = lambda _: True
    assert run(workspace, "/workflow demo").startswith("PASS")
    assert len(calls) == 10


def test_release(workspace):
    workspace.approve = lambda _: False
    run(workspace, "/sync --release auto")
    assert "v0.1.0" in workspace.read("RELEASE_NOTES.md")
    assert not git(workspace.root, "tag")
    assert next_version("v1.2.3", "feat(api)!: redesign") == "v2.0.0"


def test_workflow_retry_limit(workspace, monkeypatch):
    attempts = []

    async def revise(model, instruction, prompt, tools):
        if instruction.startswith("Review"):
            attempts.append(prompt)
            return "REVISE add coverage"
        return "artifact"

    monkeypatch.setattr("adk_coder.harness.generate", revise)
    assert run(workspace, "/workflow demo") == "REVISE add coverage"
    assert len(attempts) == 3


def test_merge_gate(workspace):
    h = workspace
    target = git(h.root, "branch", "--show-current").decode().strip()
    run(h, "/branch feature")
    (h.root / "change").write_text("new")
    git(h.root, "add", "change")
    git(
        h.root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.org",
        "commit",
        "-qm",
        "feat: change",
    )
    h.config["test_command"] = "false"
    with pytest.raises(ValueError, match="verification"):
        run(h, "/merge " + target)
    assert git(h.root, "branch", "--show-current").strip() == b"feature"
    h.config["test_command"] = "true"
    run(h, "/merge " + target)
    assert git(h.root, "branch", "--show-current").decode().strip() == target
    assert (h.root / "change").read_text() == "new"


def test_adk_model_runner(workspace, monkeypatch):
    from google.adk import agents
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    class FixtureModel(BaseLlm):
        async def generate_content_async(self, llm_request, stream=False):
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part(text="Ready")])
            )

    original = agents.Agent

    def with_model(**kwargs):
        kwargs["model"] = FixtureModel(model="fixture")
        return original(**kwargs)

    monkeypatch.setattr(agents, "Agent", with_model)
    assert run(workspace, "Hello") == "Ready"
    assert "Ready" in workspace.read("context_summary.md")


def test_openai_model_selection_and_routing(workspace, monkeypatch):
    pytest.importorskip("litellm")
    from google.adk.models.lite_llm import LiteLlm

    model_id = "openai/org/custom-model"
    run(workspace, "/model " + model_id)
    reopened = AgentHarness(workspace.root)
    assert reopened.model == model_id

    async def inspect_agent(agent, prompt):
        assert isinstance(agent.model, LiteLlm)
        assert agent.model.model == model_id
        return "Ready"

    monkeypatch.setattr("adk_coder.model_factory.run_agent", inspect_agent)
    assert run(reopened, "Hello") == "Ready"
