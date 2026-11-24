"""Token estimation, model context limits, and real session usage tracking."""

from dataclasses import dataclass

# Ordered prefix/substring table of approximate model context windows.
MODEL_WINDOWS = (
    ("gemini-2.5", 1_000_000),
    ("gemini-2.0", 1_000_000),
    ("gemini-1.5", 1_000_000),
    ("gemini-", 1_000_000),
    ("openai/gpt-4.1", 1_000_000),
    ("openai/gpt-4o", 128_000),
    ("openai/o1", 200_000),
    ("openai/o3", 200_000),
    ("openai/o4", 200_000),
    ("openai/gpt-4-turbo", 128_000),
    ("openai/gpt-4", 32_768),
    ("openai/gpt-3.5", 16_385),
    ("anthropic/claude", 200_000),
    ("vertex_ai/gemini", 1_000_000),
    ("vertex_ai/claude", 200_000),
    ("ollama", 32_768),
)

DEFAULT_COMPATIBLE_WINDOW = 32_768


def estimate(text: str) -> int:
    return (len(text.encode("utf8")) + 3) // 4


def capacity(model: str, override: int | None = None) -> int:
    if override and isinstance(override, int) and override > 0:
        return override
    lowered = (model or "").lower()
    for prefix, limit in MODEL_WINDOWS:
        if lowered.startswith(prefix):
            return limit
    return DEFAULT_COMPATIBLE_WINDOW


def needs_compaction(text: str, model: str, override: int | None = None) -> bool:
    return estimate(text) >= capacity(model, override) * 0.75


@dataclass
class TokenTracker:
    prompt_tokens: int = 0
    output_tokens: int = 0
    last_prompt_tokens: int = 0
    turns: int = 0

    def record(self, prompt_tokens: int = 0, output_tokens: int = 0) -> None:
        if prompt_tokens:
            self.prompt_tokens += prompt_tokens
            self.last_prompt_tokens = prompt_tokens
        if output_tokens:
            self.output_tokens += output_tokens
        if prompt_tokens or output_tokens:
            self.turns += 1

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens
