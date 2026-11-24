"""Offline-capable CycloneDX 1.5 and SPDX 2.3 SBOM generator with license policy checks."""

from __future__ import annotations

import json
import re
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .spinner import spinning
from .tools import atomic_write
from .ui import console

PERMISSIVE_KEYWORDS = (
    "mit",
    "apache",
    "bsd",
    "isc",
    "psf",
    "python software foundation",
    "unlicense",
    "cc0",
    "0bsd",
    "zlib",
)
WEAK_COPYLEFT_KEYWORDS = ("lgpl", "mpl", "mozilla", "epl", "eclipse", "cddl")
STRONG_COPYLEFT_KEYWORDS = ("agpl", "gpl", "sspl")


def classify_license(license_str: str) -> str:
    cleaned = (license_str or "").strip().lower()
    if not cleaned or cleaned in {"unknown", "none", "n/a"}:
        return "UNKNOWN"
    if any(k in cleaned for k in WEAK_COPYLEFT_KEYWORDS):
        return "WEAK_COPYLEFT"
    if any(k in cleaned for k in STRONG_COPYLEFT_KEYWORDS):
        return "STRONG_COPYLEFT"
    if any(k in cleaned for k in PERMISSIVE_KEYWORDS):
        return "PERMISSIVE"
    return "OTHER"


def _normalize_py_name(raw: str) -> str:
    return re.sub(r"[-_.]+", "-", raw.strip()).lower()


def _parse_req_line(line: str) -> tuple[str, str] | None:
    stripped = line.split("#", 1)[0].strip()
    if not stripped or stripped.startswith(("-", "git+", "http://", "https://")):
        return None
    stripped = stripped.split(";", 1)[0].strip()
    match = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?\s*(.*)$", stripped)
    if not match:
        return None
    name, spec = match.group(1), match.group(2).strip()
    version = re.sub(r"^[=<>!~,\s]+", "", spec.split(",", 1)[0]).strip() or "unspecified"
    return name, version


def _scan_venv_metadata(root: Path) -> dict[str, tuple[str, str]]:
    """Read installed dist-info metadata from local .venv/venv if present."""
    found: dict[str, tuple[str, str]] = {}
    for env_name in (".venv", "venv"):
        env_dir = root / env_name
        if not env_dir.is_dir():
            continue
        for meta_path in env_dir.glob("**/site-packages/*.dist-info/METADATA"):
            name = version = license_val = ""
            try:
                for line in meta_path.read_text(encoding="utf8", errors="replace").splitlines():
                    if not line:
                        break
                    if line.startswith("Name:"):
                        name = line.split(":", 1)[1].strip()
                    elif line.startswith("Version:"):
                        version = line.split(":", 1)[1].strip()
                    elif line.startswith("License-Expression:") and not license_val:
                        license_val = line.split(":", 1)[1].strip()
                    elif line.startswith("License:") and not license_val:
                        candidate = line.split(":", 1)[1].strip()
                        if candidate and candidate.upper() != "UNKNOWN" and len(candidate) <= 80:
                            license_val = candidate
                    elif line.startswith("Classifier: License ::") and not license_val:
                        license_val = line.rsplit("::", 1)[-1].strip()
            except OSError:
                continue
            if name:
                found[_normalize_py_name(name)] = (version or "unspecified", license_val or "UNKNOWN")
    return found


def discover_components(root: Path) -> list[dict]:
    components: list[dict] = []
    seen: set[tuple[str, str]] = set()
    venv_meta = _scan_venv_metadata(root)

    def add(ecosystem: str, name: str, version: str, license_id: str, scope: str = "required") -> None:
        key = (ecosystem, name.lower())
        if key in seen:
            return
        seen.add(key)
        purl_type = {"python": "pypi", "node": "npm", "go": "golang", "java": "maven"}.get(ecosystem, ecosystem)
        ver = version or "unspecified"
        purl = f"pkg:{purl_type}/{name}@{ver}" if ver != "unspecified" else f"pkg:{purl_type}/{name}"
        lic = (license_id or "UNKNOWN").strip() or "UNKNOWN"
        components.append(
            {
                "name": name,
                "version": ver,
                "ecosystem": ecosystem,
                "purl": purl,
                "license": lic,
                "license_policy": classify_license(lic),
                "scope": scope,
            }
        )

    # 1. Python pyproject.toml
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf8"))
            project = data.get("project", {})
            for dep in project.get("dependencies", []) or []:
                parsed = _parse_req_line(dep)
                if parsed:
                    name, ver = parsed
                    meta_ver, meta_lic = venv_meta.get(_normalize_py_name(name), (ver, "UNKNOWN"))
                    add("python", name, meta_ver if ver == "unspecified" else ver, meta_lic, "required")
            for group, deps in (project.get("optional-dependencies", {}) or {}).items():
                for dep in deps or []:
                    parsed = _parse_req_line(dep)
                    if parsed:
                        name, ver = parsed
                        meta_ver, meta_lic = venv_meta.get(_normalize_py_name(name), (ver, "UNKNOWN"))
                        add("python", name, meta_ver if ver == "unspecified" else ver, meta_lic, f"optional:{group}")
        except (OSError, tomllib.TOMLDecodeError):
            pass

    # 2. Python requirements*.txt
    for req_file in sorted(root.glob("requirements*.txt")):
        scope = "optional:dev" if "dev" in req_file.name else "required"
        try:
            for line in req_file.read_text(encoding="utf8").splitlines():
                parsed = _parse_req_line(line)
                if parsed:
                    name, ver = parsed
                    meta_ver, meta_lic = venv_meta.get(_normalize_py_name(name), (ver, "UNKNOWN"))
                    add("python", name, meta_ver if ver == "unspecified" else ver, meta_lic, scope)
        except OSError:
            continue

    # 3. Node package-lock.json / package.json
    pkg_lock = root / "package-lock.json"
    if pkg_lock.is_file():
        try:
            lock = json.loads(pkg_lock.read_text(encoding="utf8"))
            for pkg_path, info in (lock.get("packages") or {}).items():
                if not pkg_path or not isinstance(info, dict):
                    continue
                name = info.get("name") or pkg_path.rsplit("node_modules/", 1)[-1]
                ver = str(info.get("version") or "unspecified")
                lic = str(info.get("license") or "UNKNOWN")
                scope = "optional:dev" if info.get("dev") else "required"
                add("node", name, ver, lic, scope)
        except (OSError, json.JSONDecodeError):
            pass

    pkg_json = root / "package.json"
    if pkg_json.is_file():
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf8"))
            for field, scope in (("dependencies", "required"), ("devDependencies", "optional:dev")):
                for name, ver in (pkg.get(field) or {}).items():
                    clean_ver = str(ver).lstrip("^~>=< ")
                    add("node", name, clean_ver or "unspecified", "UNKNOWN", scope)
        except (OSError, json.JSONDecodeError):
            pass

    # 4. Go go.mod
    go_mod = root / "go.mod"
    if go_mod.is_file():
        try:
            in_require = False
            for raw_line in go_mod.read_text(encoding="utf8").splitlines():
                line = raw_line.split("//", 1)[0].strip()
                if line.startswith("require ("):
                    in_require = True
                    continue
                if in_require and line == ")":
                    in_require = False
                    continue
                if line.startswith("require "):
                    parts = line[len("require ") :].split()
                elif in_require and line:
                    parts = line.split()
                else:
                    continue
                if len(parts) >= 2:
                    add("go", parts[0], parts[1], "UNKNOWN", "required")
        except OSError:
            pass

    # 5. Java Maven pom.xml
    pom_xml = root / "pom.xml"
    if pom_xml.is_file():
        try:
            text = pom_xml.read_text(encoding="utf8")
            for block in re.findall(r"<dependency>([\s\S]*?)</dependency>", text):
                gid = re.search(r"<groupId>\s*([^<]+?)\s*</groupId>", block)
                aid = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", block)
                ver = re.search(r"<version>\s*([^<]+?)\s*</version>", block)
                scope_m = re.search(r"<scope>\s*([^<]+?)\s*</scope>", block)
                if gid and aid:
                    dep_scope = (
                        f"optional:{scope_m.group(1)}"
                        if scope_m and scope_m.group(1) != "compile"
                        else "required"
                    )
                    add(
                        "java",
                        f"{gid.group(1)}/{aid.group(1)}",
                        ver.group(1) if ver else "managed",
                        "UNKNOWN",
                        dep_scope,
                    )
        except OSError:
            pass

    components.sort(key=lambda c: (c["ecosystem"], c["name"].lower()))
    return components


def build_cyclonedx(root: Path, components: list[dict]) -> dict:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(UTC).isoformat(),
            "tools": [{"vendor": "adk-coder", "name": "adk-coder-sbom", "version": "0.1.0"}],
            "component": {"type": "application", "name": root.name},
        },
        "components": [
            {
                "type": "library",
                "name": comp["name"],
                "version": comp["version"],
                "purl": comp["purl"],
                "scope": "optional" if comp["scope"].startswith("optional") else "required",
                "licenses": (
                    [{"license": {"name": comp["license"]}}]
                    if comp["license"] != "UNKNOWN"
                    else []
                ),
                "properties": [
                    {"name": "adk_coder:ecosystem", "value": comp["ecosystem"]},
                    {"name": "adk_coder:license_policy", "value": comp["license_policy"]},
                ],
            }
            for comp in components
        ],
    }


def build_spdx(root: Path, components: list[dict]) -> dict:
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{root.name}-sbom",
        "documentNamespace": f"https://adk-coder.local/spdx/{root.name}-{uuid.uuid4()}",
        "creationInfo": {
            "created": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "creators": ["Tool: adk-coder-0.1.0"],
        },
        "packages": [
            {
                "name": comp["name"],
                "SPDXID": f"SPDXRef-Package-{idx}",
                "versionInfo": comp["version"],
                "downloadLocation": "NOASSERTION",
                "licenseConcluded": (
                    comp["license"] if comp["license"] != "UNKNOWN" else "NOASSERTION"
                ),
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": comp["purl"],
                    }
                ],
            }
            for idx, comp in enumerate(components, 1)
        ],
    }


def generate_sbom(h, args: list[str]) -> str:
    fmt = "cyclonedx"
    out_arg = ""
    for arg in args:
        lowered = arg.lower()
        if lowered in {"cyclonedx", "cdx"}:
            fmt = "cyclonedx"
        elif lowered == "spdx":
            fmt = "spdx"
        elif not out_arg:
            out_arg = arg
        else:
            raise ValueError("Usage: /sbom [cyclonedx|spdx] [output-path]")

    with spinning(f"Generating {fmt.upper()} SBOM"):
        components = discover_components(h.root)
        document = (
            build_spdx(h.root, components)
            if fmt == "spdx"
            else build_cyclonedx(h.root, components)
        )
    default_name = "sbom.spdx.json" if fmt == "spdx" else "sbom.cdx.json"
    destination = h.tools.path(out_arg) if out_arg else (h.state / default_name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        destination,
        json.dumps(document, indent=2) + "\n",
        mode=0o600 if not out_arg else None,
    )

    counts = {"PERMISSIVE": 0, "WEAK_COPYLEFT": 0, "STRONG_COPYLEFT": 0, "OTHER": 0, "UNKNOWN": 0}
    flagged: list[dict] = []
    for comp in components:
        counts[comp["license_policy"]] = counts.get(comp["license_policy"], 0) + 1
        if comp["license_policy"] in {"STRONG_COPYLEFT", "WEAK_COPYLEFT"}:
            flagged.append(comp)

    table = Table(title=f"SBOM ({fmt.upper()}) — {len(components)} components", border_style="cyan")
    for col in ("Ecosystem", "Package", "Version", "Scope", "License", "Policy"):
        table.add_column(col)
    for comp in components[:40]:
        policy = comp["license_policy"]
        style = {
            "PERMISSIVE": "green",
            "WEAK_COPYLEFT": "yellow",
            "STRONG_COPYLEFT": "bold red",
            "UNKNOWN": "dim",
        }.get(policy, "white")
        table.add_row(
            comp["ecosystem"],
            comp["name"],
            comp["version"],
            comp["scope"],
            comp["license"],
            Text(policy, style=style),
        )
    if len(components) > 40:
        table.add_row("...", f"+{len(components) - 40} more", "", "", "", "")

    summary_lines = [
        f"Saved SBOM: {destination}",
        (
            f"Licenses: {counts['PERMISSIVE']} permissive, "
            f"{counts['WEAK_COPYLEFT']} weak-copyleft, "
            f"{counts['STRONG_COPYLEFT']} strong-copyleft, "
            f"{counts['UNKNOWN']} unknown."
        ),
    ]
    if flagged:
        summary_lines.append(
            "Review copyleft dependencies: "
            + ", ".join(f"{c['name']} ({c['license']})" for c in flagged[:10])
        )
    if console.is_terminal:
        console.print(table)
        console.print(Panel("\n".join(summary_lines), border_style="cyan"))
        return ""
    return "\n".join(summary_lines)
