"""Git subprocess wrapper and transactional workspace checkpoints."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path


class Git:
    def __init__(self, root):
        self.root = root

    def raw(self, *args, env=None):
        try:
            proc = subprocess.run(
                ["git", *args], cwd=self.root, env=env, capture_output=True, check=False
            )
        except FileNotFoundError as exc:
            raise ValueError("Git is not installed or not on PATH.") from exc
        if proc.returncode:
            raise ValueError(
                proc.stderr.decode(errors="replace").strip() or "Git command failed"
            )
        return proc.stdout

    def run(self, *args):
        return self.raw(*args).decode(errors="replace").rstrip()

    def is_repo(self) -> bool:
        try:
            return self.run("rev-parse", "--is-inside-work-tree") == "true"
        except ValueError:
            return False

    def default_branch(self) -> str:
        for candidate in ("main", "master"):
            try:
                self.run("show-ref", "--verify", f"refs/heads/{candidate}")
                return candidate
            except ValueError:
                continue
        return ""


class Checkpoints:
    def __init__(self, root, git):
        self.root = root
        self.state = root / ".agent"
        self.directory = self.state / "checkpoints"
        self.git = git

    def _validate_key(self, key: str) -> Path:
        if not re.fullmatch(r"chk_\d{8}_\d{6}_[0-9a-f]{8}", key):
            raise ValueError("Invalid checkpoint ID")
        snap = self.directory / key
        if not snap.is_dir():
            raise ValueError(f"Checkpoint not found: {key}")
        return snap

    def create(self, label, model, tokens):
        base = self.git.run("rev-parse", "HEAD")
        if Path(self.git.run("rev-parse", "--show-toplevel")).resolve() != self.root:
            raise ValueError("Start adk-coder at the Git repository root.")
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        key = f"chk_{stamp}_{uuid.uuid4().hex[:8]}"
        with tempfile.TemporaryDirectory(dir=self.directory) as tmp:
            staging = Path(tmp)
            env = dict(os.environ, GIT_INDEX_FILE=str(staging / "index"))
            paths = ["--", ".", ":(exclude).agent"]
            self.git.raw("read-tree", base, env=env)
            self.git.raw("add", "-A", *paths, env=env)
            options = [
                "diff",
                "--cached",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
                base,
            ]
            work = self.git.raw(*options, *paths, env=env)
            index = self.git.raw(*options, *paths)
            snap = staging / "snapshot"
            snap.mkdir()
            (snap / "working_tree.patch").write_bytes(work)
            (snap / "index.patch").write_bytes(index)
            meta = {
                "label": label,
                "commit": base,
                "branch": self.git.run("branch", "--show-current"),
                "model": model,
                "tokens": tokens,
            }
            (snap / "manifest.json").write_text(
                json.dumps(meta, indent=2), encoding="utf8"
            )
            for name in ("context_summary.md", "TASKS.md"):
                (snap / name).write_bytes((self.state / name).read_bytes())
            snap.rename(self.directory / key)
        return key

    def delete(self, key, approve):
        snap = self._validate_key(key)
        if not approve(f"Delete checkpoint {key}?"):
            return "Cancelled."
        shutil.rmtree(snap)
        return f"Deleted {key}"

    def restore(self, key, approve):
        snap = self._validate_key(key)
        meta = json.loads((snap / "manifest.json").read_text(encoding="utf8"))
        if self.git.run("rev-parse", "HEAD") != meta["commit"]:
            raise ValueError("Switch to the checkpoint's base commit first.")
        if self.git.run("status", "--porcelain", "--", ".", ":(exclude).agent"):
            raise ValueError("Commit or stash current changes before restoring.")
        plans = []
        for filename, flags in [
            ("working_tree.patch", []),
            ("index.patch", ["--cached"]),
        ]:
            patch = snap / filename
            if patch.stat().st_size:
                self.git.raw("apply", "--check", *flags, str(patch))
                plans.append([*flags, str(patch)])
        memory = {
            n: (snap / n).read_text(encoding="utf8")
            for n in ("context_summary.md", "TASKS.md")
        }
        if not approve(f"Restore code, index and memory from {key}?"):
            return "Cancelled."
        from .tools import atomic_write

        original = {
            name: (self.state / name).read_text(encoding="utf8") for name in memory
        }
        written = []
        applied = []
        try:
            for args in plans:
                self.git.raw("apply", *args)
                applied.append(args)
            for name, value in memory.items():
                atomic_write(self.state / name, value, mode=0o600)
                written.append(name)
        except Exception:
            for name in written:
                atomic_write(self.state / name, original[name], mode=0o600)
            for args in reversed(applied):
                self.git.raw("apply", "--reverse", *args)
            raise
        return f"Restored {key}"
