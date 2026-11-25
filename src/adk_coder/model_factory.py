"""ADK session, model routing, SSE streaming, tool events, abort handling, and usage tracking."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from .scrubber import scrub

LITELLM_PREFIXES = (
    "ollama/",
    "ollama_chat/",
    "openai/",
    "anthropic/",
    "vertex_ai/",
    "azure/",
    "groq/",
    "mistral/",
    "openrouter/",
)
DEFAULT_LITELLM_MAX_TOKENS = 8192
DEFAULT_LITELLM_TIMEOUT = 120


def resolve_model(
    model: str,
    *,
    api_base: str | None = None,
    max_tokens: int | None = None,
    timeout: int | None = None,
) -> Any:
    """Resolve a model string into either a native Gemini model ID or configured LiteLlm instance."""
    if not model.startswith(LITELLM_PREFIXES):
        return model
    from google.adk.models.lite_llm import LiteLlm

    options: dict[str, Any] = {
        "max_tokens": (
            max_tokens
            if isinstance(max_tokens, int) and max_tokens > 0
            else DEFAULT_LITELLM_MAX_TOKENS
        ),
        "timeout": (
            timeout
            if isinstance(timeout, int) and timeout > 0
            else DEFAULT_LITELLM_TIMEOUT
        ),
        "num_retries": 1,
    }
    effective_base = api_base
    if not effective_base and model.startswith("openai/"):
        effective_base = os.environ.get("OPENAI_API_BASE") or os.environ.get(
            "OPENAI_BASE_URL"
        )
    elif not effective_base and model.startswith(("ollama/", "ollama_chat/")):
        ollama_base = os.environ.get("OLLAMA_API_BASE") or os.environ.get(
            "OLLAMA_HOST"
        )
        if ollama_base:
            effective_base = (
                ollama_base
                if ollama_base.startswith(("http://", "https://"))
                else f"http://{ollama_base}"
            )
    if effective_base:
        options["api_base"] = effective_base
        if model.startswith("openai/"):
            options["api_key"] = (
                os.environ.get("ADK_CODER_ENDPOINT_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
                or "local"
            )
    return LiteLlm(model=model, **options)


async def generate(
    model: str,
    instruction: str,
    prompt: str,
    tools=(),
    *,
    api_base: str | None = None,
    max_tokens: int | None = None,
    timeout: int | None = None,
    output_schema: Any = None,
    history: list[dict[str, str]] | None = None,
    attachments: list[tuple[bytes, str]] | None = None,
    on_text: Callable[[str], None] | None = None,
    on_thought: Callable[[str], None] | None = None,
    on_tool_call: Callable[[str, dict], None] | None = None,
    on_tool_result: Callable[[str, str], None] | None = None,
    on_usage: Callable[[int, int], None] | None = None,
    abort_signal: asyncio.Event | None = None,
) -> str:
    from google.adk.agents import Agent

    resolved_model = resolve_model(
        model, api_base=api_base, max_tokens=max_tokens, timeout=timeout
    )

    agent_kwargs: dict[str, Any] = {
        "name": "coding_assistant",
        "model": resolved_model,
        "instruction": instruction,
        "tools": list(tools),
    }
    if output_schema is not None:
        agent_kwargs["output_schema"] = output_schema

    agent = Agent(**agent_kwargs)

    run_kwargs: dict[str, Any] = {}
    if run_agent is _DEFAULT_RUN_AGENT:
        if history:
            run_kwargs["history"] = history
        if attachments:
            run_kwargs["attachments"] = attachments
        if on_text is not None:
            run_kwargs["on_text"] = on_text
        if on_thought is not None:
            run_kwargs["on_thought"] = on_thought
        if on_tool_call is not None:
            run_kwargs["on_tool_call"] = on_tool_call
        if on_tool_result is not None:
            run_kwargs["on_tool_result"] = on_tool_result
        if on_usage is not None:
            run_kwargs["on_usage"] = on_usage
        if abort_signal is not None:
            run_kwargs["abort_signal"] = abort_signal

    if run_kwargs:
        return await run_agent(agent, prompt, **run_kwargs)
    return await run_agent(agent, prompt)


async def run_agent(
    agent,
    prompt: str,
    graph: bool = False,
    *,
    history: list[dict[str, str]] | None = None,
    attachments: list[tuple[bytes, str]] | None = None,
    on_text: Callable[[str], None] | None = None,
    on_thought: Callable[[str], None] | None = None,
    on_tool_call: Callable[[str, dict], None] | None = None,
    on_tool_result: Callable[[str, str], None] | None = None,
    on_usage: Callable[[int, int], None] | None = None,
    abort_signal: asyncio.Event | None = None,
) -> str:
    from google.adk.agents import RunConfig
    from google.adk.agents.run_config import StreamingMode
    from google.adk.events import Event
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    runner = InMemoryRunner(
        app_name="adk_coder", **{"node" if graph else "agent": agent}
    )
    session = await runner.session_service.create_session(
        app_name="adk_coder", user_id="developer", session_id=uuid.uuid4().hex
    )

    if history and not graph:
        for turn in history:
            role = turn.get("role")
            text = turn.get("text", "")
            if role in {"user", "model"} and text:
                author = (
                    "user"
                    if role == "user"
                    else getattr(agent, "name", "coding_assistant")
                )
                prior = Event(
                    invocation_id=f"history_{uuid.uuid4().hex[:8]}",
                    author=author,
                    content=types.Content(
                        role=role, parts=[types.Part(text=scrub(text))]
                    ),
                )
                await runner.session_service.append_event(session, prior)

    msg_parts = [types.Part(text=scrub(prompt))]
    for blob, mime_type in attachments or []:
        msg_parts.append(types.Part.from_bytes(data=blob, mime_type=mime_type))
    message = types.Content(role="user", parts=msg_parts)
    stream_enabled = (on_text is not None or on_thought is not None) and not graph
    run_config = (
        RunConfig(streaming_mode=StreamingMode.SSE) if stream_enabled else None
    )

    run_params: dict[str, Any] = {
        "user_id": "developer",
        "session_id": session.id,
        "new_message": message,
    }
    if run_config is not None:
        run_params["run_config"] = run_config
    if abort_signal is not None:
        run_params["abort_signal"] = abort_signal

    answer: list[str] = []
    streamed_chunks: list[str] = []
    fallback_thoughts: list[str] = []
    aiter = runner.run_async(**run_params).__aiter__()
    abort_waiter: asyncio.Task[bool] | None = (
        asyncio.create_task(abort_signal.wait()) if abort_signal is not None else None
    )
    try:
        while True:
            if abort_signal is not None and abort_signal.is_set():
                break
            next_task: asyncio.Task[Any] = asyncio.create_task(aiter.__anext__())
            if abort_waiter is not None:
                done, _ = await asyncio.wait(
                    {next_task, abort_waiter}, return_when=asyncio.FIRST_COMPLETED
                )
                if abort_waiter in done:
                    next_task.cancel()
                    with suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                        await next_task
                    break
            try:
                event = await next_task
            except StopAsyncIteration:
                break

            if abort_signal is not None and abort_signal.is_set():
                break

            if on_usage is not None and getattr(event, "usage_metadata", None):
                meta = event.usage_metadata
                p_tok = getattr(meta, "prompt_token_count", 0) or 0
                o_tok = getattr(meta, "candidates_token_count", 0) or 0
                if p_tok or o_tok:
                    on_usage(int(p_tok), int(o_tok))

            is_partial = bool(getattr(event, "partial", False))

            if on_tool_call is not None and hasattr(event, "get_function_calls"):
                for call in event.get_function_calls() or []:
                    call_args = dict(getattr(call, "args", None) or {})
                    if is_partial and not call_args:
                        continue
                    on_tool_call(
                        getattr(call, "name", "tool"),
                        call_args,
                    )

            if on_tool_result is not None and hasattr(event, "get_function_responses"):
                for resp in event.get_function_responses() or []:
                    payload = getattr(resp, "response", "")
                    preview = (
                        payload.get("result", str(payload))
                        if isinstance(payload, dict)
                        else str(payload)
                    )
                    on_tool_result(getattr(resp, "name", "tool"), str(preview))

            if is_partial and event.content:
                for part in event.content.parts or []:
                    chunk = getattr(part, "text", None)
                    if not chunk:
                        continue
                    if getattr(part, "thought", False):
                        if on_thought is not None:
                            on_thought(scrub(chunk))
                        fallback_thoughts.append(chunk)
                    else:
                        if on_text is not None:
                            on_text(scrub(chunk))
                        streamed_chunks.append(chunk)
                continue

            if event.is_final_response() and event.content:
                for part in event.content.parts or []:
                    text = getattr(part, "text", None)
                    if not text or not text.strip():
                        continue
                    if getattr(part, "thought", False):
                        if on_thought is not None and not fallback_thoughts:
                            on_thought(scrub(text))
                        fallback_thoughts.append(text.strip())
                    else:
                        answer.append(text)
    except asyncio.CancelledError:
        if abort_signal is not None:
            abort_signal.set()
        raise
    finally:
        if abort_waiter is not None and not abort_waiter.done():
            abort_waiter.cancel()
        with suppress(TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(runner.close(), timeout=2.0)

    if abort_signal is not None and abort_signal.is_set():
        partial_text = "".join(streamed_chunks).strip()
        return scrub(partial_text) if partial_text else "Cancelled."

    if not answer and streamed_chunks:
        answer = ["".join(streamed_chunks)]
    if not answer and fallback_thoughts:
        answer = fallback_thoughts
    if not answer:
        raise RuntimeError("The model returned no final response.")
    return scrub("\n".join(answer))


_DEFAULT_RUN_AGENT = run_agent
