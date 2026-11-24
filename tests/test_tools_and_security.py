"""Tests for coding tools, path protection, session approvals, and secret scrubbing."""

import pytest

from adk_coder.harness import AgentHarness
from adk_coder.scrubber import scrub


def test_discovery_and_search_tools(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text(
        "def greet(name: str) -> str:\n    return f'hello {name}'\n",
        encoding="utf8",
    )
    (tmp_path / "src/helper.py").write_text("VALUE = 42\n", encoding="utf8")
    (tmp_path / ".env").write_text("SECRET_KEY=supersecretvalue123\n", encoding="utf8")

    h = AgentHarness(tmp_path, approve=lambda _: True)

    listing = h.tools.list_dir(".")
    assert "src/" in listing
    assert "app.py" in listing
    assert ".env" not in listing

    found = h.tools.find_files("*.py")
    assert "src/app.py" in found
    assert "src/helper.py" in found

    hits = h.tools.search_files("VALUE = 42")
    assert "src/helper.py:1:" in hits
    assert "supersecretvalue123" not in h.tools.search_files("supersecret")


def test_read_file_ranges_and_binary_guard(tmp_path):
    lines = [f"line_{i}" for i in range(1, 21)]
    (tmp_path / "notes.txt").write_text("\n".join(lines) + "\n", encoding="utf8")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02binary")

    h = AgentHarness(tmp_path, approve=lambda _: True)
    snippet = h.tools.read_file("notes.txt", start_line=5, end_line=8)
    assert "5 | line_5" in snippet
    assert "8 | line_8" in snippet
    assert "line_4" not in snippet
    assert "showing lines 5-8 of 20" in snippet

    binary_msg = h.tools.read_file("blob.bin")
    assert "Binary file skipped" in binary_msg


def test_apply_diff_disambiguation_and_delete(tmp_path):
    target = tmp_path / "dup.py"
    target.write_text("x = 1\ny = 2\nx = 1\n", encoding="utf8")
    h = AgentHarness(tmp_path, approve=lambda _: True)

    ambiguous = h.tools.apply_diff("dup.py", "x = 1", "x = 9")
    assert "found 2 matches" in ambiguous

    ok = h.tools.apply_diff("dup.py", "x = 1", "x = 9", start_line=3)
    assert ok == "Saved."
    assert target.read_text(encoding="utf8") == "x = 1\ny = 2\nx = 9\n"

    assert h.tools.delete_file("dup.py") == "Deleted dup.py."
    assert not target.exists()

    assert h.tools.write_file("fresh/hello.txt", "hello world\n") == "Saved."
    assert (tmp_path / "fresh/hello.txt").read_text(encoding="utf8") == "hello world\n"


def test_shell_operators_missing_binary_and_session_approval(tmp_path):
    prompts = []

    def approve_always(msg):
        prompts.append(msg)
        return "always"

    h = AgentHarness(tmp_path, approve=approve_always)

    # Shell pipeline works when shell_mode=True
    out = h.tools.run_shell_command("printf 'a\\nb\\n' | wc -l")
    assert out.startswith("Exit 0")
    assert "2" in out
    assert len(prompts) == 1

    # Second call with same leading token reuses session 'always' approval
    out2 = h.tools.run_shell_command("printf 'ok\\n'")
    assert out2.startswith("Exit 0")
    assert len(prompts) == 1

    # Missing executable returns a clean error instead of raising
    missing = h.tools.run_shell_command("definitely_not_a_real_binary_xyz")
    assert "Command failed to start" in missing or "Exit" in missing


def test_expanded_protected_paths_and_scrubber(tmp_path):
    h = AgentHarness(tmp_path, approve=lambda _: True)
    for forbidden in (
        ".env.local",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "gcp-service-account.json",
        "id_rsa",
        "cert.p12",
    ):
        with pytest.raises(ValueError, match="Protected workspace path"):
            h.tools.path(forbidden)

    # Secrets must be redacted
    assert "[REDACTED]" in scrub("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def")
    assert "ghp_12345678901234567890" not in scrub("token=ghp_12345678901234567890")
    assert "github_pat_1234567890abcdef12345" not in scrub(
        "github_pat_1234567890abcdef12345"
    )
    assert "xoxb-1234567890-abcdef" not in scrub("xoxb-1234567890-abcdef")
    assert "mysecret123" not in scrub("api_key=mysecret123")

    # Legitimate Python type annotations & empty defaults should NOT be mangled
    code_sample = 'def login(password: str, api_key: Optional[str] = None):\n    secret = ""\n'
    assert scrub(code_sample) == code_sample
