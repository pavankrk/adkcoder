import asyncio
import json

import pytest

from adk_coder.harness import AgentHarness
from adk_coder.scaffolder import STACKS


@pytest.mark.parametrize("stack", STACKS)
def test_new(tmp_path, stack):
    harness = AgentHarness(tmp_path, approve=lambda _: True)
    command = f"/new {stack} example-app"
    asyncio.run(harness.execute(command))
    dest = tmp_path / "example-app"
    assert (dest / ".git").is_dir()
    assert (dest / ".github/workflows/test.yml").is_file()
    assert AgentHarness(dest).test_command() == STACKS[stack][3]
    with pytest.raises(FileExistsError):
        asyncio.run(harness.execute(command))


@pytest.mark.parametrize("passes", [False, True])
def test_scan_fix(tmp_path, monkeypatch, passes):
    harness = AgentHarness(tmp_path, approve=lambda _: True)
    harness.config["test_command"] = "verify"
    source = tmp_path / "example.py"
    source.write_bytes(b"# pending edits\r\nx = (\r\n")

    async def ask(role, prompt):
        if "Review" in role:
            return "invalid response"
        return json.dumps({"search_block": "x = (", "replace_block": "x = 1"})

    monkeypatch.setattr(harness, "ask", ask)
    monkeypatch.setattr("adk_coder.audit.shutil.which", lambda _: None)
    monkeypatch.setattr(
        harness.tools, "run_shell_command", lambda *_: "Exit 0" if passes else "Exit 1"
    )
    asyncio.run(harness.execute("/scan example.py"))
    asyncio.run(harness.execute("/fix 1"))
    assert source.read_bytes() == (
        b"# pending edits\r\nx = 1\r\n" if passes else b"# pending edits\r\nx = (\r\n"
    )
    assert len(json.loads(harness.read("audit_cache.json"))["findings"]) == int(
        not passes
    )
