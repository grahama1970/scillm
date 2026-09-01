"""Claude (Anthropic) provider via OAuth subscription tokens.

Translates OpenAI chat completion format to Anthropic Messages API format.
Uses OAuth tokens from ~/.pi/agent/auth.json (shared with Pi CLI).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from functools import lru_cache
import uuid
from typing import Any, AsyncIterator

import httpx
import openai
from loguru import logger

from scillm.proxy.errors import ProviderOAuthError, provider_error_code_for_status
from scillm.proxy.providers import make_chunk_id, sse_chunk, sse_done, sse_format, streaming_timeout
from scillm.proxy.providers.auth import get_anthropic_token

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_BETA = (
    "claude-code-20250219,"
    "oauth-2025-04-20,"
    "fine-grained-tool-streaming-2025-05-14,"
    "interleaved-thinking-2025-05-14"
)
# OAuth tokens with claude-code scope require this system prompt prefix
CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."

# Map friendly model names to Anthropic API model IDs
CLAUDE_MODEL_MAP = {
    "claude-opus-5": "claude-opus-5",
    "claude-sonnet-5": "claude-sonnet-5",
    "claude-fable-5": "claude-fable-5",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-opus-4-8": "claude-opus-4-8",
    "claude-opus-4-7": "claude-opus-4-7",
    "claude-opus-4-6": "claude-opus-4-6",
    "claude-opus-4-5": "claude-opus-4-5-20251101",
    "claude-haiku-4-5": "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5": "claude-sonnet-4-5-20250929",
}

_CLAUDE_THINKING_BUDGET_TOKENS = {
    "low": 1024,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
    "max": 32000,
}


def _oauth_error_message(status_code: int | None, provider: str) -> str:
    if status_code in (401, 403):
        return (
            f"{provider} OAuth authentication failed. Check /v1/scillm/auth, "
            "refresh the provider login on the host, then retry."
        )
    if status_code == 404:
        return (
            f"{provider} reported that the requested model is unavailable. "
            "Use /v1/scillm/models and request an exact supported model."
        )
    if status_code == 429:
        return f"{provider} rate limited the OAuth call. Reduce concurrency; do not batch OAuth models."
    return f"{provider} OAuth provider call failed. Inspect provider_error_code and details."


def _extract_provider_error(error_text: str) -> tuple[str | None, str]:
    try:
        payload = json.loads(error_text)
    except json.JSONDecodeError:
        return None, error_text[:500]
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("type") or err.get("code") or "") or None, str(err.get("message") or payload)[:500]
        if err:
            return None, str(err)[:500]
    return None, str(payload)[:500]


def _apply_reasoning_effort(body: dict[str, Any], effort: str | None) -> tuple[bool, str | None, str | None]:
    """Apply Claude thinking config for public reasoning effort values."""
    if not effort or effort == "none":
        return False, None, "none_requested" if effort == "none" else None
    budget = _CLAUDE_THINKING_BUDGET_TOKENS.get(effort)
    if budget is None:
        return False, None, "unsupported_effort_for_provider"
    body["thinking"] = {"type": "enabled", "budget_tokens": budget}
    return True, "thinking.budget_tokens", None


def _is_temperature_deprecated_error(status_code: int, error_text: str) -> bool:
    lowered = error_text.lower()
    return status_code == 400 and "temperature" in lowered and "deprecated" in lowered


@lru_cache(maxsize=1)
def _claude_cli_version() -> str:
    """Client version reported to Anthropic: the newest of the installed
    claude CLI and the npm registry's latest. Never hardcoded: a frozen
    version is exactly what Anthropic starts rejecting (claude-cli/2.1.75
    400s), and a stale installed CLI has the same problem."""
    candidates: list[tuple[int, ...]] = []
    try:
        out = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=10
        ).stdout
        m = re.search(r"\d+\.\d+\.\d+", out)
        if m:
            candidates.append(tuple(int(p) for p in m.group(0).split(".")))
    except Exception:
        pass
    try:
        resp = httpx.get(
            "https://registry.npmjs.org/@anthropic-ai/claude-code/latest", timeout=10
        )
        version = resp.json().get("version", "")
        if re.fullmatch(r"\d+\.\d+\.\d+", version):
            candidates.append(tuple(int(p) for p in version.split(".")))
    except Exception:
        pass
    if candidates:
        return ".".join(str(p) for p in max(candidates))
    raise ProviderOAuthError(
        "Cannot determine Claude Code client version: no claude CLI on PATH and "
        "npm registry unreachable. Install/update the claude CLI in this environment."
    )


def _anthropic_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-version": ANTHROPIC_VERSION,
        "anthropic-beta": ANTHROPIC_BETA,
        "user-agent": f"claude-cli/{_claude_cli_version()}",
        "x-app": "cli",
        "content-type": "application/json",
    }


def _openai_tools_to_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI tool definitions to Anthropic format.

    OpenAI: {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
    Anthropic: {"name": ..., "description": ..., "input_schema": ...}
    """
    anthropic_tools = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        fn = tool.get("function", {})
        anthropic_tools.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return anthropic_tools


def _openai_tool_choice_to_anthropic(tool_choice: Any) -> dict[str, Any] | None:
    """Convert OpenAI tool_choice to Anthropic format.

    OpenAI "auto" → {"type": "auto"}
    OpenAI "required" → {"type": "any"}
    OpenAI "none" → {"type": "none"}  (omit — Anthropic doesn't send tools if none)
    OpenAI {"type": "function", "function": {"name": "X"}} → {"type": "tool", "name": "X"}
    """
    if tool_choice is None or tool_choice == "auto":
        return {"type": "auto"}
    if tool_choice == "required":
        return {"type": "any"}
    if tool_choice == "none":
        return None  # Don't send tools at all
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function", {})
        name = fn.get("name", "")
        if name:
            return {"type": "tool", "name": name}
    return {"type": "auto"}


def _openai_to_anthropic_messages(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Convert OpenAI messages to Anthropic format.

    Returns (system_prompt, anthropic_messages).
    Handles tool_calls in assistant messages and tool results.
    """
    system_prompt: str | None = None
    anthropic_msgs: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "system":
            # Anthropic takes system as a top-level field, not in messages
            if isinstance(content, str):
                system_prompt = content
            continue

        # Tool result messages → Anthropic tool_result content block
        if role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            result_content = msg.get("content", "")
            anthropic_msgs.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call_id,
                        "content": result_content if isinstance(result_content, str) else json.dumps(result_content),
                    }
                ],
            })
            continue

        # Map OpenAI roles to Anthropic roles
        if role == "assistant":
            a_role = "assistant"
        else:
            a_role = "user"

        # Assistant messages with tool_calls → Anthropic tool_use content blocks
        tool_calls = msg.get("tool_calls")
        if role == "assistant" and tool_calls:
            a_parts: list[dict[str, Any]] = []
            # Include text content if present
            if content and isinstance(content, str):
                a_parts.append({"type": "text", "text": content})
            for tc in tool_calls:
                fn = tc.get("function", {})
                arguments = fn.get("arguments", "{}")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {"raw": arguments}
                a_parts.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": arguments,
                })
            anthropic_msgs.append({"role": "assistant", "content": a_parts})
            continue

        # Handle content types
        if isinstance(content, str):
            anthropic_msgs.append({"role": a_role, "content": content})
        elif isinstance(content, list):
            # Convert OpenAI content parts to Anthropic format
            a_parts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    a_parts.append({"type": "text", "text": part["text"]})
                elif part.get("type") == "document":
                    # Native document block (e.g., PDF) — pass through to Anthropic
                    source = part.get("source", {})
                    a_parts.append({
                        "type": "document",
                        "source": {
                            "type": source.get("type", "base64"),
                            "media_type": source.get("media_type", "application/pdf"),
                            "data": source.get("data", ""),
                        },
                    })
                elif part.get("type") == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    if url.startswith("data:"):
                        # data:image/png;base64,xxxxx or data:application/pdf;base64,xxxxx
                        header, data = url.split(",", 1)
                        media_type = header.split(":")[1].split(";")[0]
                        if media_type == "application/pdf":
                            # PDF → Anthropic document block
                            a_parts.append({
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": data,
                                },
                            })
                        else:
                            a_parts.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": data,
                                },
                            })
            if a_parts:
                anthropic_msgs.append({"role": a_role, "content": a_parts})

    return system_prompt, anthropic_msgs


def _anthropic_to_openai_response(
    data: dict[str, Any],
    model: str,
) -> openai.types.chat.ChatCompletion:
    """Wrap Anthropic Messages response in OpenAI ChatCompletion format."""
    content_blocks = data.get("content", [])
    text_parts = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
    text = "".join(text_parts) or None

    # Extract tool_use blocks → OpenAI tool_calls
    tool_calls_list = []
    for block in content_blocks:
        if block.get("type") == "tool_use":
            tool_calls_list.append(
                openai.types.chat.ChatCompletionMessageToolCall(
                    id=block.get("id", ""),
                    type="function",
                    function=openai.types.chat.chat_completion_message_tool_call.Function(
                        name=block.get("name", ""),
                        arguments=json.dumps(block.get("input", {})),
                    ),
                )
            )

    # Map stop reason
    stop_reason = data.get("stop_reason", "end_turn")
    if stop_reason == "max_tokens":
        finish_reason = "length"
    elif stop_reason == "tool_use":
        finish_reason = "tool_calls"
    else:
        finish_reason = "stop"

    # Usage
    usage = data.get("usage", {})

    return openai.types.chat.ChatCompletion(
        id=f"chatcmpl-claude-{uuid.uuid4().hex[:8]}",
        choices=[
            openai.types.chat.chat_completion.Choice(
                finish_reason=finish_reason,
                index=0,
                message=openai.types.chat.ChatCompletionMessage(
                    content=text,
                    role="assistant",
                    tool_calls=tool_calls_list if tool_calls_list else None,
                ),
            )
        ],
        created=int(time.time()),
        model=data.get("model", model),
        object="chat.completion",
        usage=openai.types.CompletionUsage(
            completion_tokens=usage.get("output_tokens", 0),
            prompt_tokens=usage.get("input_tokens", 0),
            total_tokens=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        ),
    )


async def claude_completion(
    model: str,
    messages: list[dict[str, Any]],
    **kwargs: Any,
) -> openai.types.chat.ChatCompletion:
    """Call Claude via Anthropic Messages API using OAuth token.

    Translates OpenAI format → Anthropic format → call → translate back.
    """
    token = get_anthropic_token()
    if not token:
        raise ProviderOAuthError(
            provider="anthropic-oauth",
            status_code=401,
            message="No Anthropic OAuth token available.",
            provider_error_code="PROVIDER_AUTH_FAILED",
            model_requested=model,
            provider_auth_status="not_configured_or_expired",
            project_agent_message="Run `claude auth login --claudeai`, then check /v1/scillm/auth before retrying.",
        )

    # Map friendly names to API model IDs
    api_model = CLAUDE_MODEL_MAP.get(model, model)
    reasoning_effort = kwargs.get("reasoning_effort")
    require_exact_model = bool(kwargs.get("require_exact_model")) or kwargs.get("allow_model_remap") is False

    system_prompt, anthropic_msgs = _openai_to_anthropic_messages(messages)

    # System prompt: OAuth requires the Claude Code prefix as the first block.
    # Caller's system prompt is appended as a second block (same as Pi CLI).
    system_blocks: list[dict[str, str]] = [
        {"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX},
    ]
    if system_prompt:
        system_blocks.append({"type": "text", "text": system_prompt})

    body: dict[str, Any] = {
        "model": api_model,
        "messages": anthropic_msgs,
        "max_tokens": kwargs.get("max_tokens", 4096),
        "system": system_blocks,
    }
    if "temperature" in kwargs:
        body["temperature"] = kwargs["temperature"]
    if "top_p" in kwargs:
        body["top_p"] = kwargs["top_p"]
    if "stop" in kwargs:
        stop = kwargs["stop"]
        body["stop_sequences"] = stop if isinstance(stop, list) else [stop]
    reasoning_forwarded, _, reasoning_ignored_reason = _apply_reasoning_effort(body, reasoning_effort)
    if reasoning_effort and not reasoning_forwarded and reasoning_ignored_reason == "unsupported_effort_for_provider":
        raise ProviderOAuthError(
            provider="anthropic-oauth",
            status_code=400,
            message=f"Claude OAuth does not support reasoning_effort={reasoning_effort!r}.",
            provider_error_code="PROVIDER_BAD_REQUEST",
            model_requested=model,
            provider_auth_status="configured",
            provider_error_type="unsupported_reasoning_effort",
            project_agent_message="Use one of: low, medium, high, xhigh, max; or omit reasoning_effort.",
        )

    # Tool use: translate OpenAI tools format to Anthropic
    if "tools" in kwargs and kwargs["tools"]:
        anthropic_tools = _openai_tools_to_anthropic(kwargs["tools"])
        if anthropic_tools:
            body["tools"] = anthropic_tools
            tc = _openai_tool_choice_to_anthropic(kwargs.get("tool_choice"))
            if tc is not None:
                body["tool_choice"] = tc

    headers = _anthropic_headers(token)

    logger.info("Claude OAuth call: model={}, {} messages, {} tools", model, len(anthropic_msgs), len(body.get("tools", [])))

    timeout = kwargs.get("timeout", 90)
    async with httpx.AsyncClient(timeout=float(timeout)) as client:
        resp = await client.post(ANTHROPIC_API_URL, json=body, headers=headers)
        if resp.status_code in (401, 403):
            refreshed_token = get_anthropic_token(force_refresh=True)
            if refreshed_token:
                logger.info("Retrying Claude OAuth call after forced token refresh")
                headers = _anthropic_headers(refreshed_token)
                resp = await client.post(
                    ANTHROPIC_API_URL,
                    json=body,
                    headers=headers,
                )
        if "temperature" in body and _is_temperature_deprecated_error(resp.status_code, resp.text):
            retry_body = dict(body)
            retry_body.pop("temperature", None)
            logger.info("Retrying Claude OAuth call without deprecated temperature parameter")
            resp = await client.post(ANTHROPIC_API_URL, json=retry_body, headers=headers)

    if resp.status_code != 200:
        error_body = resp.text
        provider_error_type, provider_message = _extract_provider_error(error_body)
        # Detect Anthropic OAuth gate changes — fail fast to cascade
        if "third-party" in error_body.lower() or "extra usage" in error_body.lower():
            logger.error(
                "Claude OAuth BLOCKED — Anthropic changed third-party policy. "
                "Check headers (user-agent, x-app, anthropic-beta). Error: {}",
                error_body[:300],
            )
        logger.warning("Claude API error {}: {}", resp.status_code, error_body[:500])
        provider_error_code = provider_error_code_for_status(resp.status_code)
        raise ProviderOAuthError(
            provider="anthropic-oauth",
            status_code=resp.status_code if resp.status_code in (400, 401, 403, 404, 429) else 502,
            message=f"Claude API {resp.status_code}: {provider_message}",
            provider_error_code=provider_error_code,
            provider_status_code=resp.status_code,
            model_requested=model,
            provider_auth_status="provider_rejected" if resp.status_code in (401, 403) else "configured",
            provider_error_type=provider_error_type,
            project_agent_message=_oauth_error_message(resp.status_code, "Claude"),
        )

    data = resp.json()
    completion = _anthropic_to_openai_response(data, model)
    if require_exact_model and completion.model != api_model:
        raise ProviderOAuthError(
            provider="anthropic-oauth",
            status_code=502,
            message=f"Claude served model '{completion.model}' for requested model '{api_model}'.",
            provider_error_code="PROVIDER_MODEL_MISMATCH",
            model_requested=api_model,
            model_served=completion.model,
            provider_auth_status="configured",
            project_agent_message="Requested and served Claude model differ; fail closed or choose an exact supported model.",
        )
    return completion


async def claude_completion_stream(
    model: str,
    messages: list[dict[str, Any]],
    **kwargs: Any,
) -> AsyncIterator[bytes]:
    """Stream Claude via Anthropic Messages API, yielding OpenAI SSE bytes.

    Translates Anthropic SSE events (content_block_delta, message_delta,
    message_stop) into OpenAI-compatible ``chat.completion.chunk`` format.
    """
    token = get_anthropic_token()
    if not token:
        err = sse_format({"error": {
            "message": "No Anthropic OAuth token available.",
            "type": "provider_auth_error",
            "provider_error_code": "PROVIDER_AUTH_FAILED",
            "provider": "anthropic-oauth",
            "model_requested": model,
            "provider_auth_status": "not_configured_or_expired",
            "project_agent_message": "Run `claude auth login --claudeai`, then check /v1/scillm/auth before retrying.",
        }})
        yield err.encode()
        yield sse_done().encode()
        return

    api_model = CLAUDE_MODEL_MAP.get(model, model)
    reasoning_effort = kwargs.get("reasoning_effort")
    require_exact_model = bool(kwargs.get("require_exact_model")) or kwargs.get("allow_model_remap") is False
    system_prompt, anthropic_msgs = _openai_to_anthropic_messages(messages)

    system_blocks: list[dict[str, str]] = [
        {"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX},
    ]
    if system_prompt:
        system_blocks.append({"type": "text", "text": system_prompt})

    body: dict[str, Any] = {
        "model": api_model,
        "messages": anthropic_msgs,
        "max_tokens": kwargs.get("max_tokens", 4096),
        "system": system_blocks,
        "stream": True,
    }
    if "temperature" in kwargs:
        body["temperature"] = kwargs["temperature"]
    if "top_p" in kwargs:
        body["top_p"] = kwargs["top_p"]
    if "stop" in kwargs:
        stop = kwargs["stop"]
        body["stop_sequences"] = stop if isinstance(stop, list) else [stop]
    reasoning_forwarded, _, reasoning_ignored_reason = _apply_reasoning_effort(body, reasoning_effort)
    if reasoning_effort and not reasoning_forwarded and reasoning_ignored_reason == "unsupported_effort_for_provider":
        err = sse_format({"error": {
            "message": f"Claude OAuth does not support reasoning_effort={reasoning_effort!r}.",
            "type": "provider_error",
            "provider_error_code": "PROVIDER_BAD_REQUEST",
            "provider": "anthropic-oauth",
            "model_requested": model,
            "provider_auth_status": "configured",
            "provider_error_type": "unsupported_reasoning_effort",
            "project_agent_message": "Use one of: low, medium, high, xhigh, max; or omit reasoning_effort.",
        }})
        yield err.encode()
        yield sse_done().encode()
        return

    # Tool use: translate OpenAI tools format to Anthropic
    if "tools" in kwargs and kwargs["tools"]:
        anthropic_tools = _openai_tools_to_anthropic(kwargs["tools"])
        if anthropic_tools:
            body["tools"] = anthropic_tools
            tc = _openai_tool_choice_to_anthropic(kwargs.get("tool_choice"))
            if tc is not None:
                body["tool_choice"] = tc

    headers = _anthropic_headers(token)

    logger.info("Claude OAuth stream: model={}, {} messages, {} tools", model, len(anthropic_msgs), len(body.get("tools", [])))
    chunk_id = make_chunk_id()
    timeout = kwargs.get("timeout", 90)
    tool_call_index = 0  # tracks which tool_call we're streaming

    try:
        async with httpx.AsyncClient(timeout=streaming_timeout(timeout)) as client:
            async with client.stream("POST", ANTHROPIC_API_URL, json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    error_text = error_body.decode("utf-8", errors="replace")
                    provider_error_type, provider_message = _extract_provider_error(error_text)
                    logger.warning("Claude stream error {}: {}", resp.status_code, error_text[:500])
                    provider_error_code = provider_error_code_for_status(resp.status_code)
                    err = sse_format({"error": {
                        "message": f"Claude API {resp.status_code}: {provider_message[:300]}",
                        "type": "provider_auth_error" if provider_error_code == "PROVIDER_AUTH_FAILED" else "provider_error",
                        "provider_error_code": provider_error_code,
                        "provider_status_code": resp.status_code,
                        "provider": "anthropic-oauth",
                        "provider_error_type": provider_error_type,
                        "model_requested": model,
                        "project_agent_message": _oauth_error_message(resp.status_code, "Claude"),
                    }})
                    yield err.encode()
                    yield sse_done().encode()
                    return

                buffer = ""
                async for text_chunk in resp.aiter_text():
                    buffer += text_chunk
                    while "\n\n" in buffer:
                        event_str, buffer = buffer.split("\n\n", 1)
                        # Parse SSE event
                        event_type = ""
                        event_data = ""
                        for line in event_str.split("\n"):
                            if line.startswith("event: "):
                                event_type = line[7:].strip()
                            elif line.startswith("data: "):
                                event_data = line[6:]

                        if not event_data:
                            continue
                        try:
                            data = json.loads(event_data)
                        except json.JSONDecodeError:
                            continue

                        if event_type == "content_block_start":
                            # Tool use block starts here — emit initial tool_call chunk
                            block = data.get("content_block", {})
                            if block.get("type") == "tool_use":
                                tc = [{
                                    "index": tool_call_index,
                                    "id": block.get("id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": block.get("name", ""),
                                        "arguments": "",
                                    },
                                }]
                                chunk = sse_chunk(chunk_id, model, tool_calls=tc)
                                yield sse_format(chunk).encode()
                                tool_call_index += 1

                        elif event_type == "content_block_delta":
                            delta = data.get("delta", {})
                            if delta.get("type") == "text_delta":
                                chunk = sse_chunk(chunk_id, model, content_delta=delta.get("text", ""))
                                yield sse_format(chunk).encode()
                            elif delta.get("type") == "input_json_delta":
                                # Stream tool call arguments incrementally
                                tc = [{
                                    "index": tool_call_index - 1,
                                    "function": {
                                        "arguments": delta.get("partial_json", ""),
                                    },
                                }]
                                chunk = sse_chunk(chunk_id, model, tool_calls=tc)
                                yield sse_format(chunk).encode()

                        elif event_type == "message_delta":
                            delta = data.get("delta", {})
                            actual_model = data.get("message", {}).get("model") or api_model
                            if require_exact_model and actual_model != api_model:
                                err = sse_format({"error": {
                                    "message": f"Claude served model '{actual_model}' for requested model '{api_model}'.",
                                    "type": "provider_error",
                                    "provider_error_code": "PROVIDER_MODEL_MISMATCH",
                                    "provider": "anthropic-oauth",
                                    "model_requested": api_model,
                                    "model_served": actual_model,
                                    "project_agent_message": "Requested and served Claude model differ; fail closed.",
                                }})
                                yield err.encode()
                                yield sse_done().encode()
                                return
                            stop_reason = delta.get("stop_reason", "end_turn")
                            if stop_reason == "max_tokens":
                                finish = "length"
                            elif stop_reason == "tool_use":
                                finish = "tool_calls"
                            else:
                                finish = "stop"
                            usage_data = data.get("usage", {})
                            usage = None
                            if usage_data:
                                usage = {
                                    "prompt_tokens": usage_data.get("input_tokens", 0),
                                    "completion_tokens": usage_data.get("output_tokens", 0),
                                    "total_tokens": usage_data.get("input_tokens", 0) + usage_data.get("output_tokens", 0),
                                }
                            chunk = sse_chunk(chunk_id, model, finish_reason=finish, usage=usage)
                            yield sse_format(chunk).encode()

                        elif event_type == "message_stop":
                            yield sse_done().encode()
                            return

    except Exception as exc:
        logger.error("Claude stream interrupted: {}", exc)
        err = sse_format({"error": {"message": f"stream_interrupted: {exc}", "type": "stream_error"}})
        yield err.encode()
        yield sse_done().encode()
