"""OpenAI Codex provider via OAuth subscription tokens.

Calls chatgpt.com/backend-api/codex/responses with SSE streaming.
Uses OAuth tokens from ~/.codex/auth.json (Codex CLI) or ~/.pi/agent/auth.json (Pi CLI).
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx
import openai
from loguru import logger

from scillm.proxy.providers.auth import get_codex_credentials

CODEX_API_URL = "https://chatgpt.com/backend-api/codex/responses"


def _openai_messages_to_codex_input(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Convert OpenAI messages to Codex Responses API input format.

    Returns (system_instructions, input_items).
    """
    instructions: str | None = None
    input_items: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "system":
            if isinstance(content, str):
                instructions = content
            continue

        # Codex uses the same role names as OpenAI
        if isinstance(content, str):
            input_items.append({"role": role, "type": "message", "content": content})
        elif isinstance(content, list):
            # Flatten content parts to text for Codex
            text_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part["text"])
            if text_parts:
                input_items.append({
                    "role": role,
                    "type": "message",
                    "content": "\n".join(text_parts),
                })

    return instructions, input_items


def _parse_codex_response(events: list[dict[str, Any]], model: str) -> openai.types.chat.ChatCompletion:
    """Parse Codex SSE events into OpenAI ChatCompletion format."""
    text_parts: list[str] = []
    usage_input = 0
    usage_output = 0
    actual_model = model

    for event in events:
        event_type = event.get("type", "")

        if event_type == "response.output_text.delta":
            delta = event.get("delta", "")
            if delta:
                text_parts.append(delta)

        elif event_type == "response.completed":
            resp = event.get("response", {})
            actual_model = resp.get("model", model)
            resp_usage = resp.get("usage", {})
            usage_input = resp_usage.get("input_tokens", 0)
            usage_output = resp_usage.get("output_tokens", 0)
            # Also extract full text from output items if we missed deltas
            if not text_parts:
                for item in resp.get("output", []):
                    if item.get("type") == "message":
                        for c in item.get("content", []):
                            if c.get("type") == "output_text":
                                text_parts.append(c.get("text", ""))

    text = "".join(text_parts)

    return openai.types.chat.ChatCompletion(
        id=f"chatcmpl-codex-{uuid.uuid4().hex[:8]}",
        choices=[
            openai.types.chat.chat_completion.Choice(
                finish_reason="stop",
                index=0,
                message=openai.types.chat.ChatCompletionMessage(
                    content=text,
                    role="assistant",
                ),
            )
        ],
        created=int(time.time()),
        model=actual_model,
        object="chat.completion",
        usage=openai.types.CompletionUsage(
            completion_tokens=usage_output,
            prompt_tokens=usage_input,
            total_tokens=usage_input + usage_output,
        ),
    )


async def codex_completion(
    model: str,
    messages: list[dict[str, Any]],
    **kwargs: Any,
) -> openai.types.chat.ChatCompletion:
    """Call Codex via Responses API using OAuth token.

    Uses SSE streaming internally, collects all events, returns as
    a single OpenAI ChatCompletion.
    """
    creds = get_codex_credentials()
    if not creds:
        raise Exception("No Codex OAuth token available. Run Pi CLI /login first.")

    access_token, account_id = creds
    instructions, input_items = _openai_messages_to_codex_input(messages)

    body: dict[str, Any] = {
        "model": model,
        "input": input_items,
        "instructions": instructions or "You are a helpful assistant.",
        "stream": True,
        "store": False,
    }
    # ChatGPT Codex backend does NOT support: temperature, max_output_tokens, top_p

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "chatgpt-account-id": account_id,
    }

    logger.info("Codex OAuth call: model={}, {} input items", model, len(input_items))

    timeout = kwargs.get("timeout", 120)
    events: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=float(timeout)) as client:
        async with client.stream("POST", CODEX_API_URL, json=body, headers=headers) as resp:
            if resp.status_code != 200:
                error_body = await resp.aread()
                error_text = error_body.decode("utf-8", errors="replace")
                logger.warning("Codex API error {}: {}", resp.status_code, error_text[:500])
                raise Exception(f"Codex API {resp.status_code}: {error_text[:500]}")

            # Parse SSE events
            buffer = ""
            async for chunk in resp.aiter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    event_str, buffer = buffer.split("\n\n", 1)
                    for line in event_str.split("\n"):
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str.strip() == "[DONE]":
                                break
                            try:
                                events.append(json.loads(data_str))
                            except json.JSONDecodeError:
                                pass

    return _parse_codex_response(events, model)
