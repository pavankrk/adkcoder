"""Tests for /sbom (CycloneDX/SPDX + license policy) and /security (SAST, secrets, SOLID)."""

import asyncio
import json

from adk_coder.harness import AgentHarness
from adk_coder.sbom import classify_license


def test_license_policy_classification():
    assert classify_license("MIT") == "PERMISSIVE"
    assert classify_license("Apache-2.0") == "PERMISSIVE"
    assert classify_license("MPL-2.0") == "WEAK_COPYLEFT"
    assert classify_license("GPL-3.0-only") == "STRONG_COPYLEFT"
    assert classify_license("AGPL-3.0") == "STRONG_COPYLEFT"
    assert classify_license("") == "UNKNOWN"


def test_sbom_cyclonedx_and_spdx_multi_ecosystem(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["fastapi>=0.115", "gplpkg==1.0.0"]\n',
        encoding="utf8",
    )
    meta = tmp_path / ".venv/lib/python3.13/site-packages/gplpkg-1.0.0.dist-info"
    meta.mkdir(parents=True)
    (meta / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: gplpkg\nVersion: 1.0.0\nLicense: GPL-3.0\n",
        encoding="utf8",
    )
    (tmp_path / "package-lock.json").write_text(
        json.dumps(
            {
                "packages": {
                    "": {"name": "demo-web"},
                    "node_modules/zod": {"version": "3.24.0", "license": "MIT"},
                }
            }
        ),
        encoding="utf8",
    )
    (tmp_path / "go.mod").write_text(
        "module demo\n\nrequire (\n\tgithub.com/example/lib v1.2.3\n)\n",
        encoding="utf8",
    )

    h = AgentHarness(tmp_path, approve=lambda _: True)

    cdx_summary = asyncio.run(h.execute("/sbom cyclonedx"))
    assert "strong-copyleft" in cdx_summary.lower()
    assert "gplpkg" in cdx_summary
    cdx = json.loads((h.state / "sbom.cdx.json").read_text(encoding="utf8"))
    assert cdx["bomFormat"] == "CycloneDX"
    assert cdx["specVersion"] == "1.5"
    names = {c["name"] for c in cdx["components"]}
    assert {"fastapi", "gplpkg", "zod", "github.com/example/lib"} <= names

    asyncio.run(h.execute("/sbom spdx docs/sbom.spdx.json"))
    spdx = json.loads((tmp_path / "docs/sbom.spdx.json").read_text(encoding="utf8"))
    assert spdx["spdxVersion"] == "SPDX-2.3"
    assert len(spdx["packages"]) >= 4


def test_security_sast_secrets_and_solid_checks(tmp_path, monkeypatch):
    (tmp_path / "domain").mkdir()
    (tmp_path / "domain/user.py").write_text(
        "import requests\n"
        "import pickle\n"
        "import subprocess\n\n"
        "AWS_KEY = 'AKIA1234567890ABCDEF'\n\n"
        "def risky(a, b, c, d, e, f, g, cur):\n"
        "    if a and b and c and d and e and f and g:\n"
        "        for i in range(3):\n"
        "            if i > 0:\n"
        "                if i == 1:\n"
        "                    if i != 2:\n"
        "                        eval('1+1')\n"
        "    pickle.loads(b'data')\n"
        "    subprocess.run('ls', shell=True)\n"
        "    cur.execute(f'SELECT * FROM users WHERE id = {a}')\n"
        "    return requests\n",
        encoding="utf8",
    )

    h = AgentHarness(tmp_path, approve=lambda _: True)
    monkeypatch.setattr("adk_coder.security.shutil.which", lambda _: None)

    asyncio.run(h.execute("/security"))
    report = json.loads((h.state / "security_report.json").read_text(encoding="utf8"))
    codes = {item["code"] for item in report["findings"]}

    # Security findings
    assert "SEC-SECRET-AWS" in codes
    assert "SAST-EXEC" in codes
    assert "SAST-DESERIALIZE" in codes
    assert "SAST-SHELL" in codes
    assert "SAST-SQLI" in codes

    # SOLID & coding standards findings
    assert "SOLID-DIP" in codes
    assert "SOLID-ISP-ARGS" in codes
    assert "SOLID-COMPLEXITY" in codes

    # Findings are also persisted to audit_cache.json for /fix compatibility
    cache = json.loads((h.state / "audit_cache.json").read_text(encoding="utf8"))
    assert len(cache["findings"]) == len(report["findings"])
