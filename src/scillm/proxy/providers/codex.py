"""OpenAI Codex provider via OAuth subscription tokens.

Calls chatgpt.com/backend-api/codex/responses with SSE streaming.
Uses OAuth tokens from ~/.codex/auth.json (Codex CLI) or ~/.pi/agent/auth.json (Pi CLI).
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

import httpx
import openai
from loguru import logger

from scillm.proxy.providers import make_chunk_id, sse_chunk, sse_done, sse_format
from scillm.proxy.providers.auth import get_codex_credentials

CODEX_API_URL = "https://chatgpt.com/backend-api/codex/responses"


def _openai_tools_to_codex(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI Chat tool definitions to Codex Responses API format.

    OpenAI Chat: {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
    Codex Responses: {"type": "function", "name": ..., "description": ..., "parameters": ...}
    """
    codex_tools = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        fn = tool.get("function", {})
        codex_tool: dict[str, Any] = {
            "type": "function",
            "name": fn.get("name", ""),
        }
        if "description" in fn:
            codex_tool["description"] = fn["description"]
        if "parameters" in fn:
            codex_tool["parameters"] = fn["parameters"]
        codex_tools.append(codex_tool)
    return codex_tools


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

        # Tool result messages → Codex function_call_output
        if role == "tool":
            input_items.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id", ""),
                "output": content if isinstance(content, str) else json.dumps(content),
            })
            continue

        # Assistant messages with tool_calls → Codex function_call items
        tool_calls = msg.get("tool_calls")
        if role == "assistant" and tool_calls:
            # First add any text content as a message
            if content and isinstance(content, str):
                input_items.append({"role": "assistant", "type": "message", "content": content})
            # Then add each tool call
            for tc in tool_calls:
                fn = tc.get("function", {})
                input_items.append({
                    "type": "function_call",
                    "call_id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", "{}"),
                })
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
    """Parse Codex SSE events into OpenAI ChatCompletion format.

    For tool calls, we prefer the response.completed event's output items
    which have complete call_id and name. Delta events may have empty call_ids.
    """
    text_parts: list[str] = []
    tool_calls_map: dict[str, dict[str, Any]] = {}  # call_id → {name, arguments}
    usage_input = 0
    usage_output = 0
    actual_model = model

    for event in events:
        event_type = event.get("type", "")

        if event_type == "response.output_text.delta":
            delta = event.get("delta", "")
            if delta:
                text_parts.append(delta)

        elif event_type == "response.output_item.added":
            # Track function_call items as they start (has call_id + name)
            item = event.get("item", {})
            if item.get("type") == "function_call":
                call_id = item.get("call_id", item.get("id", ""))
                if call_id:
                    tool_calls_map[call_id] = {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", ""),
                    }

        elif event_type == "response.output_item.done":
            # Finalize function_call with complete data
            item = event.get("item", {})
            if item.get("type") == "function_call":
                call_id = item.get("call_id", item.get("id", ""))
                if call_id:
                    tool_calls_map[call_id] = {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", ""),
                    }

        elif event_type == "response.function_call_arguments.done":
            call_id = event.get("call_id", "")
            name = event.get("name", "")
            arguments = event.get("arguments", "")
            if call_id:
                tool_calls_map[call_id] = {"name": name, "arguments": arguments}

        elif event_type == "response.completed":
            resp = event.get("response", {})
            actual_model = resp.get("model", model)
            resp_usage = resp.get("usage", {})
            usage_input = resp_usage.get("input_tokens", 0)
            usage_output = resp_usage.get("output_tokens", 0)
            # Extract from output items — authoritative source for tool calls
            for item in resp.get("output", []):
                if item.get("type") == "message":
                    if not text_parts:
                        for c in item.get("content", []):
                            if c.get("type") == "output_text":
                                text_parts.append(c.get("text", ""))
                elif item.get("type") == "function_call":
                    call_id = item.get("call_id", item.get("id", ""))
                    tool_calls_map[call_id] = {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", ""),
                    }

    text = "".join(text_parts) or None

    # Build OpenAI tool_calls list
    tool_calls_list = []
    for call_id, tc_data in tool_calls_map.items():
        tool_calls_list.append(
            openai.types.chat.ChatCompletionMessageToolCall(
                id=call_id,
                type="function",
                function=openai.types.chat.chat_completion_message_tool_call.Function(
                    name=tc_data["name"],
                    arguments=tc_data["arguments"],
                ),
            )
        )

    finish_reason = "tool_calls" if tool_calls_list else "stop"

    return openai.types.chat.ChatCompletion(
        id=f"chatcmpl-codex-{uuid.uuid4().hex[:8]}",
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

    # Tool use: translate OpenAI Chat tools to Codex Responses API format
    # OpenAI Chat: {"type": "function", "function": {"name": ..., "parameters": ...}}
    # Codex Responses: {"type": "function", "name": ..., "parameters": ..., "description": ...}
    if "tools" in kwargs and kwargs["tools"]:
        body["tools"] = _openai_tools_to_codex(kwargs["tools"])
    # Codex Responses API only supports tool_choice: "auto" (Pi hardcodes this)
    if "tools" in body:
        body["tool_choice"] = "auto"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "chatgpt-account-id": account_id,
    }

    logger.info("Codex OAuth call: model={}, {} input items, {} tools", model, len(input_items), len(body.get("tools", [])))

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


async def codex_completion_stream(
    model: str,
    messages: list[dict[str, Any]],
    **kwargs: Any,
) -> AsyncIterator[bytes]:
    """Stream Codex via Responses API, yielding OpenAI SSE bytes.

    Translates Codex SSE events (response.output_text.delta,
    response.completed) into OpenAI-compatible ``chat.completion.chunk`` format.
    """
    creds = get_codex_credentials()
    if not creds:
        raise Exception("No Codex OAuth token available.")

    access_token, account_id = creds
    instructions, input_items = _openai_messages_to_codex_input(messages)

    body: dict[str, Any] = {
        "model": model,
        "input": input_items,
        "instructions": instructions or "You are a helpful assistant.",
        "stream": True,
        "store": False,
    }

    # Tool use: translate OpenAI Chat tools to Codex Responses API format
    if "tools" in kwargs and kwargs["tools"]:
        body["tools"] = _openai_tools_to_codex(kwargs["tools"])
    # Codex Responses API only supports tool_choice: "auto" (Pi hardcodes this)
    if "tools" in body:
        body["tool_choice"] = "auto"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "chatgpt-account-id": account_id,
    }

    logger.info("Codex OAuth stream: model={}, {} input items, {} tools", model, len(input_items), len(body.get("tools", [])))
    chunk_id = make_chunk_id()
    timeout = kwargs.get("timeout", 120)
    tool_call_index = 0
    tool_call_ids: dict[str, int] = {}  # call_id → index

    try:
        async with httpx.AsyncClient(timeout=float(timeout)) as client:
            async with client.stream("POST", CODEX_API_URL, json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    error_text = error_body.decode("utf-8", errors="replace")
                    logger.warning("Codex stream error {}: {}", resp.status_code, error_text[:500])
                    err = sse_format({"error": {"message": f"Codex API {resp.status_code}: {error_text[:300]}", "type": "provider_error"}})
                    yield err.encode()
                    yield sse_done().encode()
                    return

                buffer = ""
                async for text_chunk in resp.aiter_text():
                    buffer += text_chunk
                    while "\n\n" in buffer:
                        event_str, buffer = buffer.split("\n\n", 1)
                        for line in event_str.split("\n"):
                            if not line.startswith("data: "):
                                continue
                            data_str = line[6:]
                            if data_str.strip() == "[DONE]":
                                yield sse_done().encode()
                                return

                            try:
                                event = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue

                            event_type = event.get("type", "")

                            if event_type == "response.output_text.delta":
                                delta = event.get("delta", "")
                                if delta:
                                    chunk = sse_chunk(chunk_id, model, content_delta=delta)
                                    yield sse_format(chunk).encode()

                            elif event_type == "response.function_call_arguments.delta":
                                call_id = event.get("call_id", "")
                                if call_id not in tool_call_ids:
                                    # First chunk for this tool call — emit initial with name
                                    tool_call_ids[call_id] = tool_call_index
                                    tc = [{
                                        "index": tool_call_index,
                                        "id": call_id,
                                        "type": "function",
                                        "function": {
                                            "name": event.get("name", ""),
                                            "arguments": event.get("delta", ""),
                                        },
                                    }]
                                    tool_call_index += 1
                                else:
                                    # Subsequent argument delta
                                    tc = [{
                                        "index": tool_call_ids[call_id],
                                        "function": {
                                            "arguments": event.get("delta", ""),
                                        },
                                    }]
                                chunk = sse_chunk(chunk_id, model, tool_calls=tc)
                                yield sse_format(chunk).encode()

                            elif event_type == "response.completed":
                                resp_data = event.get("response", {})
                                actual_model = resp_data.get("model", model)
                                resp_usage = resp_data.get("usage", {})
                                usage = {
                                    "prompt_tokens": resp_usage.get("input_tokens", 0),
                                    "completion_tokens": resp_usage.get("output_tokens", 0),
                                    "total_tokens": resp_usage.get("input_tokens", 0) + resp_usage.get("output_tokens", 0),
                                }
                                finish = "tool_calls" if tool_call_ids else "stop"
                                chunk = sse_chunk(chunk_id, actual_model, finish_reason=finish, usage=usage)
                                yield sse_format(chunk).encode()
                                yield sse_done().encode()
                                return

    except Exception as exc:
        logger.error("Codex stream interrupted: {}", exc)
        err = sse_format({"error": {"message": f"stream_interrupted: {exc}", "type": "stream_error"}})
        yield err.encode()
        yield sse_done().encode()
