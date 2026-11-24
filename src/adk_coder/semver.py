import re


def next_version(tag, messages):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag)
    if not match:
        raise ValueError(f"Invalid version tag {tag}")
    major, minor, patch = map(int, match.groups())
    if re.search(r"^(?:BREAKING[ -]CHANGE:|\w+(?:\([^)]*\))?!:)", messages, re.MULTILINE):
        return f"v{major + 1}.0.0"
    if re.search(r"^feat(?:\([^)]*\))?:", messages, re.MULTILINE):
        return f"v{major}.{minor + 1}.0"
    return f"v{major}.{minor}.{patch + 1}"
