import re


def format_result(command, code, output):
    patterns = {
        "pytest": r"\d+ (?:passed|failed|skipped|errors?)",
        "ruff": r"(?:Found \d+ errors?|All checks passed)",
        "eslint": r"\d+ (?:problems?|errors?|warnings?)",
    }
    pattern = next((p for name, p in patterns.items() if name in command), None)
    summary = ", ".join(re.findall(pattern, output)) if pattern else ""
    return f"Exit {code} {summary}\n{output}".rstrip()
