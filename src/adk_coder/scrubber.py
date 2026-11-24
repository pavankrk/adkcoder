"""Remove common credential formats before model calls and logging."""

import re

PATTERNS = [
    # Raw or JSON-escaped PEM private keys
    r"-----BEGIN [^-]*PRIVATE KEY-----(?:[\s\S]*?|\\n[\s\S]*?)-----END [^-]*PRIVATE KEY-----",
    # Well-known provider tokens (Google, OpenAI/Anthropic, GitHub classic & fine-grained, AWS, Slack)
    r"\b(?:AIza[\w-]{30,}|sk-[\w-]{12,}|gh[pousr]_[\w]{16,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}|xox[baprs]-[\w-]{10,})",
    # JWTs
    r"\beyJ[\w-]+\.[\w-]+\.[\w-]+",
    # Authorization: Bearer tokens
    r"(?i)\bAuthorization\s*:\s*Bearer\s+[A-Za-z0-9._~+/=-]{8,}",
    # Database / cache URLs with embedded credentials
    r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s/@]+:[^\s/@]+@",
    # Key/value assignments for secrets, avoiding type annotations like `password: str` or empty strings
    r"""(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|secret)(?:\s*=\s*["']?(?!["']|None\b|True\b|False\b|os\.|env\b|getenv\b)[^\s,"'}{)(]{4,}["']?|\s*:\s*["'](?!(?:str|int|bool|float|bytes|Any|Optional)\b)[^\s,"'}{]{4,}["'])""",
]


def scrub(text: str) -> str:
    if not text:
        return text
    for pattern in PATTERNS:
        text = re.sub(pattern, "[REDACTED]", text)
    return text
