"""OpenCode Go provider helpers.

OpenCode Go exposes two API shapes:
- OpenAI-compatible chat completions for most models.
- Anthropic-compatible messages for DeepSeek and MiniMax models.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from typing import Any, AsyncIterator

import httpx
import openai
from loguru import logger

from scillm.proxy.providers import make_chunk_id, sse_chunk, sse_done, sse_format, streaming_timeout
from scillm.proxy.providers.claude import (
    ANTHROPIC_VERSION,
    _anthropic_to_openai_response,
    _openai_tool_choice_to_anthropic,
    _openai_tools_to_anthropic,
    _openai_to_anthropic_messages,
)

OPENCODE_GO_PROVIDER = "opencode-go"
OPENCODE_GO_PREFIX = f"{OPENCODE_GO_PROVIDER}/"
OPENCODE_GO_DEFAULT_API_BASE = "https://opencode.ai/zen/v1"
OPENCODE_SERVER_DEFAULT_URL = "http://127.0.0.1:4096"
OPENCODE_GO_CHAT_TIMEOUT_SEC = 120
OPENCODE_GO_MESSAGES_TIMEOUT_SEC = 600

ENDPOINT_CHAT_COMPLETIONS = "chat_completions"
ENDPOINT_MESSAGES = "messages"
ENDPOINT_UNKNOWN = "unknown"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# On Zen (opencode.ai/zen/v1) chat/completions serves every model family that
# this account can reach; /messages is only kept for callers that need the
# Anthropic body shape. deepseek/minimax MUST stay on chat/completions: the
# /messages route rejects them with a China-region opt-in RegionError.
_MODEL_ENDPOINT_TYPES: dict[str, str] = {
    model_id: ENDPOINT_CHAT_COMPLETIONS
    for model_id in (
        # OpenAI family (docs list them under /responses; Zen also exposes
        # them on chat/completions — availability is account-gated).
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
        "gpt-5.5", "gpt-5.5-pro",
        "gpt-5.4", "gpt-5.4-pro", "gpt-5.4-mini", "gpt-5.4-nano",
        "gpt-5.3-codex", "gpt-5.3-codex-spark",
        "gpt-5.2", "gpt-5.2-codex",
        "gpt-5.1", "gpt-5.1-codex", "gpt-5.1-codex-max", "gpt-5.1-codex-mini",
        "gpt-5", "gpt-5-codex", "gpt-5-nano",
        # Anthropic family
        "claude-fable-5", "claude-opus-5",
        "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5",
        "claude-sonnet-5", "claude-sonnet-4-6", "claude-sonnet-4-5", "claude-sonnet-4",
        "claude-haiku-4-5",
        # Google family
        "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
        "gemini-3.5-flash-lite", "gemini-3.1-pro", "gemini-3-flash",
        # Qwen
        "qwen3.7-max", "qwen3.7-plus", "qwen3.6-plus", "qwen3.5-plus",
        # DeepSeek
        "deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4-flash-free",
        # MiniMax
        "minimax-m3", "minimax-m2.7", "minimax-m2.5",
        # GLM
        "glm-5.2", "glm-5.1", "glm-5",
        # Kimi
        "kimi-k2.7-code", "kimi-k2.6", "kimi-k2.5",
        # Grok
        "grok-4.5", "grok-build-0.1",
        # Misc / free tier
        "big-pickle", "mimo-v2.5-free", "north-mini-code-free",
        "nemotron-3-ultra-free",
        # Legacy ids kept for existing callers
        "mimo-v2-omni", "mimo-v2-pro", "mimo-v2.5", "mimo-v2.5-pro",
    )
}


class OpenCodeGoHTTPError(Exception):
    """HTTP error returned by the OpenCode Go API."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"OpenCode Go API {status_code}: {message}")


def is_opencode_go_model(model: str) -> bool:
    """Return true when *model* uses the OpenCode Go provider prefix."""
    return model.startswith(OPENCODE_GO_PREFIX) and len(model) > len(OPENCODE_GO_PREFIX)


def opencode_go_model_id(model: str) -> str:
    """Strip ``opencode-go/`` from a model id."""
    return model[len(OPENCODE_GO_PREFIX):] if is_opencode_go_model(model) else model


def opencode_go_endpoint_type(model_id: str) -> str:
    """Return the OpenCode Go endpoint type for a model id.

    Unknown ids default to chat/completions so any model in the live Zen
    catalog is callable without a code change.
    """
    return _MODEL_ENDPOINT_TYPES.get(model_id, ENDPOINT_CHAT_COMPLETIONS)


def list_opencode_go_models_from_zen_sync(*, api_base: str | None = None) -> list[str]:
    """List models from the Zen gateway's public ``/models`` endpoint.

    ``https://opencode.ai/zen/v1/models`` answers without authentication, so
    this is the authoritative catalog when the local CLI/server are absent.
    """
    base = (api_base or os.environ.get("OPENCODE_GO_API_BASE") or OPENCODE_GO_DEFAULT_API_BASE).rstrip("/")
    with httpx.Client(timeout=5.0) as client:
        resp = client.get(f"{base}/models")
    resp.raise_for_status()
    data = resp.json()
    rows = data.get("data", []) if isinstance(data, dict) else []
    return [
        f"{OPENCODE_GO_PREFIX}{row['id']}"
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    ]


def static_opencode_go_models() -> list[str]:
    """Return known OpenCode Go model ids in prefixed provider/model form."""
    return [f"{OPENCODE_GO_PREFIX}{model_id}" for model_id in sorted(_MODEL_ENDPOINT_TYPES)]


def describe_opencode_go_model(model: str, *, key_configured: bool) -> dict[str, Any]:
    """Build metadata for one OpenCode Go model."""
    model_id = opencode_go_model_id(model)
    endpoint_type = opencode_go_endpoint_type(model_id)
    supported = endpoint_type in {ENDPOINT_CHAT_COMPLETIONS, ENDPOINT_MESSAGES}
    input_capabilities = opencode_go_input_capabilities(model)
    return {
        "id": f"{OPENCODE_GO_PREFIX}{model_id}",
        "model": model_id,
        "provider": OPENCODE_GO_PROVIDER,
        "endpoint_type": endpoint_type,
        "supported": supported,
        "requires_key": True,
        "key_configured": key_configured,
        "route": "/v1/chat/completions",
        "input": input_capabilities,
        "capabilities": {
            "text_input": input_capabilities["text"],
            "image_input": input_capabilities["image"],
            "pdf_input": input_capabilities["pdf"],
            "image_output": False,
            "streaming": True,
            "tools": True,
        },
    }


def opencode_go_input_capabilities(model: str) -> dict[str, bool]:
    """Return current per-model OpenCode Go input capabilities.

    The provider is mixed-capability: DeepSeek/MiniMax use the Anthropic-style
    /messages route and are text-only here, while Kimi accepts normal
    OpenAI-style image_url parts on the chat/completions route.
    """
    model_id = opencode_go_model_id(model)
    endpoint_type = opencode_go_endpoint_type(model_id)
    image = model_id.startswith("kimi-k2.")
    return {
        "text": endpoint_type in {ENDPOINT_CHAT_COMPLETIONS, ENDPOINT_MESSAGES},
        "image": image,
        "pdf": False,
    }


def parse_opencode_models_output(output: str) -> list[str]:
    """Parse ``opencode models`` output into prefixed model ids."""
    models: list[str] = []
    seen: set[str] = set()
    for raw_line in output.splitlines():
        line = _ANSI_RE.sub("", raw_line).strip()
        if not line or line.lower().startswith("models cache refreshed"):
            continue
        for token in line.split():
            token = token.strip(",")
            if is_opencode_go_model(token) and token not in seen:
                seen.add(token)
                models.append(token)
                break
    return models


async def list_opencode_go_models_from_cli(*, refresh: bool = False, verbose: bool = False) -> list[str]:
    """List OpenCode Go models via ``opencode models opencode-go``."""
    args = ["opencode", "models"]
    if refresh:
        args.append("--refresh")
    if verbose:
        args.append("--verbose")
    args.append(OPENCODE_GO_PROVIDER)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
    text = stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"{' '.join(args)} failed ({proc.returncode}): {err[:500]}")
    return parse_opencode_models_output(text)


def list_opencode_go_models_from_cli_sync(*, refresh: bool = False, verbose: bool = False) -> list[str]:
    """List OpenCode Go models via the CLI from sync validation code."""
    args = ["opencode", "models"]
    if refresh:
        args.append("--refresh")
    if verbose:
        args.append("--verbose")
    args.append(OPENCODE_GO_PROVIDER)

    proc = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed ({proc.returncode}): {proc.stderr[:500]}")
    return parse_opencode_models_output(proc.stdout)


def list_opencode_go_models_from_server_sync(
    *,
    server_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> list[str]:
    """List OpenCode Go models from a running ``opencode serve`` instance."""
    base = (server_url or os.environ.get("OPENCODE_SERVER_URL") or OPENCODE_SERVER_DEFAULT_URL).rstrip("/")
    auth = None
    password = password if password is not None else os.environ.get("OPENCODE_SERVER_PASSWORD")
    username = username if username is not None else os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
    if password:
        auth = (username or "opencode", password)

    with httpx.Client(timeout=1.5, auth=auth) as client:
        resp = client.get(f"{base}/provider")
    resp.raise_for_status()
    data = resp.json()

    providers = data.get("all", []) if isinstance(data, dict) else []
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        provider_id = str(provider.get("id") or provider.get("providerID") or provider.get("name") or "")
        if provider_id == OPENCODE_GO_PROVIDER:
            return _extract_models_from_provider_obj(provider)
    return []


def _extract_models_from_provider_obj(provider: Any) -> list[str]:
    """Extract model ids from a flexible OpenCode Provider object."""
    found: list[str] = []
    seen: set[str] = set()

    def add(model_id: str) -> None:
        if not model_id:
            return
        prefixed = model_id if is_opencode_go_model(model_id) else f"{OPENCODE_GO_PREFIX}{model_id}"
        if prefixed not in seen:
            seen.add(prefixed)
            found.append(prefixed)

    def walk(value: Any) -> None:
        if isinstance(value, str):
            if is_opencode_go_model(value):
                add(opencode_go_model_id(value))
            elif value in _MODEL_ENDPOINT_TYPES:
                add(value)
        elif isinstance(value, dict):
            if isinstance(value.get("id"), str) and value["id"] in _MODEL_ENDPOINT_TYPES:
                add(value["id"])
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(provider)
    return found


async def list_opencode_go_models_from_server(
    *,
    server_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> list[str]:
    """List OpenCode Go models from a running ``opencode serve`` instance."""
    base = (server_url or os.environ.get("OPENCODE_SERVER_URL") or OPENCODE_SERVER_DEFAULT_URL).rstrip("/")
    auth = None
    password = password if password is not None else os.environ.get("OPENCODE_SERVER_PASSWORD")
    username = username if username is not None else os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
    if password:
        auth = (username or "opencode", password)

    async with httpx.AsyncClient(timeout=1.5, auth=auth) as client:
        resp = await client.get(f"{base}/provider")
    resp.raise_for_status()
    data = resp.json()

    providers = data.get("all", []) if isinstance(data, dict) else []
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        provider_id = str(provider.get("id") or provider.get("providerID") or provider.get("name") or "")
        if provider_id == OPENCODE_GO_PROVIDER:
            return _extract_models_from_provider_obj(provider)
    return []


def _system_content_to_text(content: Any) -> str:
    """Return text from OpenAI system content, including content-part lists."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
        ]
        return "\n".join(parts)
    return ""


def _collect_system_prompt(messages: list[dict[str, Any]]) -> str | None:
    """Collect all OpenAI system messages instead of keeping only the last one."""
    prompts = [
        text
        for message in messages
        if message.get("role") == "system"
        for text in [_system_content_to_text(message.get("content", ""))]
        if text
    ]
    return "\n\n".join(prompts) if prompts else None


def _json_response_instruction(response_format: Any) -> str | None:
    """Translate OpenAI response_format into text for OpenCode Go /messages."""
    if not isinstance(response_format, dict):
        return None

    response_type = response_format.get("type")
    if response_type == "json_object":
        return (
            "You must respond with exactly one valid JSON object. "
            "Do not include markdown fences, prose, commentary, or text outside the JSON object."
        )

    if response_type == "json_schema":
        json_schema = response_format.get("json_schema")
        if not isinstance(json_schema, dict):
            return (
                "You must respond with exactly one valid JSON value matching the requested schema. "
                "Do not include markdown fences, prose, commentary, or text outside JSON."
            )
        name = json_schema.get("name") or "response"
        schema = json_schema.get("schema") or json_schema
        schema_text = json.dumps(schema, ensure_ascii=False, sort_keys=True)
        return (
            f"You must respond with exactly one valid JSON value for schema {name!r}. "
            "Do not include markdown fences, prose, commentary, or text outside JSON. "
            f"JSON schema: {schema_text}"
        )

    return None


def _merge_system_prompt(*parts: str | None) -> str | None:
    merged = [part for part in parts if part]
    return "\n\n".join(merged) if merged else None


def _append_user_contract(messages: list[dict[str, Any]], instruction: str) -> None:
    """Append a provider-boundary contract to the last user message."""
    contract_block = {
        "type": "text",
        "text": f"Output contract reminder: {instruction}",
    }
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content.append(contract_block)
        elif isinstance(content, str):
            message["content"] = [
                {"type": "text", "text": content},
                contract_block,
            ]
        else:
            message["content"] = [contract_block]
        return
    messages.append({"role": "user", "content": [contract_block]})


def _build_messages_body(
    model: str,
    messages: list[dict[str, Any]],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    system_prompt, anthropic_msgs = _openai_to_anthropic_messages(messages)
    json_instruction = _json_response_instruction(kwargs.get("response_format"))
    system_prompt = _merge_system_prompt(
        _collect_system_prompt(messages) or system_prompt,
        json_instruction,
    )
    for message in anthropic_msgs:
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = [{"type": "text", "text": content}]
    if json_instruction:
        _append_user_contract(anthropic_msgs, json_instruction)
    body: dict[str, Any] = {
        "model": model,
        "messages": anthropic_msgs,
    }
    max_tokens = kwargs.get("max_tokens")
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if system_prompt:
        body["system"] = system_prompt
    if "temperature" in kwargs:
        body["temperature"] = kwargs["temperature"]
    if "top_p" in kwargs:
        body["top_p"] = kwargs["top_p"]
    if "stop" in kwargs:
        stop = kwargs["stop"]
        body["stop_sequences"] = stop if isinstance(stop, list) else [stop]
    if "tools" in kwargs and kwargs["tools"]:
        tools = _openai_tools_to_anthropic(kwargs["tools"])
        if tools:
            body["tools"] = tools
            tool_choice = _openai_tool_choice_to_anthropic(kwargs.get("tool_choice"))
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
    return body


def _record_provider_bound_diagnostics(
    diagnostics: dict[str, Any] | None,
    *,
    model: str,
    api_base: str,
    body: dict[str, Any],
) -> None:
    """Record prompt-free provider-bound body shape for diagnostics."""
    if diagnostics is None:
        return
    diagnostics.update({
        "provider": OPENCODE_GO_PROVIDER,
        "model": model,
        "api_base_host": httpx.URL(api_base).host,
        "body_keys": sorted(body.keys()),
        "token_cap_fields_present": [
            field for field in ("max_tokens", "max_completion_tokens") if field in body
        ],
        "stream": bool(body.get("stream")),
    })


def _messages_headers(api_key: str) -> dict[str, str]:
    if not api_key:
        raise OpenCodeGoHTTPError(401, "OPENCODE_GO_API_KEY is required")
    return {
        "Authorization": f"Bearer {api_key}",
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


async def opencode_go_messages_completion(
    model: str,
    api_base: str,
    api_key: str,
    messages: list[dict[str, Any]],
    **kwargs: Any,
) -> openai.types.chat.ChatCompletion:
    """Call an OpenCode Go Anthropic-compatible messages model."""
    diagnostics = kwargs.pop("_provider_bound_diagnostics", None)
    body = _build_messages_body(model, messages, dict(kwargs))
    _record_provider_bound_diagnostics(
        diagnostics,
        model=model,
        api_base=api_base,
        body=body,
    )
    url = f"{api_base.rstrip('/')}/messages"
    logger.info("OpenCode Go messages call: model={}, {} messages", model, len(body["messages"]))

    async with httpx.AsyncClient(timeout=float(kwargs.get("timeout", 120))) as client:
        resp = await client.post(url, json=body, headers=_messages_headers(api_key))

    if resp.status_code != 200:
        raise OpenCodeGoHTTPError(resp.status_code, resp.text[:500])
    return _anthropic_to_openai_response(resp.json(), model)


async def opencode_go_messages_stream(
    model: str,
    api_base: str,
    api_key: str,
    messages: list[dict[str, Any]],
    **kwargs: Any,
) -> AsyncIterator[bytes]:
    """Stream an OpenCode Go messages response as OpenAI-compatible SSE."""
    diagnostics = kwargs.pop("_provider_bound_diagnostics", None)
    stream_kwargs = dict(kwargs)
    stream_kwargs["stream"] = True
    body = _build_messages_body(model, messages, stream_kwargs)
    body["stream"] = True
    _record_provider_bound_diagnostics(
        diagnostics,
        model=model,
        api_base=api_base,
        body=body,
    )
    url = f"{api_base.rstrip('/')}/messages"
    headers = _messages_headers(api_key)
    chunk_id = make_chunk_id()
    tool_call_index = 0

    try:
        async with httpx.AsyncClient(timeout=streaming_timeout(kwargs.get("timeout", 120))) as client:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    error_text = error_body.decode("utf-8", errors="replace")
                    yield sse_format({
                        "error": {
                            "message": f"OpenCode Go API {resp.status_code}: {error_text[:300]}",
                            "type": "provider_error",
                        }
                    }).encode()
                    yield sse_done().encode()
                    return

                buffer = ""
                async for text_chunk in resp.aiter_text():
                    buffer += text_chunk
                    while "\n\n" in buffer:
                        event_str, buffer = buffer.split("\n\n", 1)
                        event_type = ""
                        event_data = ""
                        for line in event_str.splitlines():
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
                            block = data.get("content_block", {})
                            if block.get("type") == "tool_use":
                                yield sse_format(sse_chunk(chunk_id, model, tool_calls=[{
                                    "index": tool_call_index,
                                    "id": block.get("id", ""),
                                    "type": "function",
                                    "function": {"name": block.get("name", ""), "arguments": ""},
                                }])).encode()
                                tool_call_index += 1
                        elif event_type == "content_block_delta":
                            delta = data.get("delta", {})
                            if delta.get("type") == "text_delta":
                                yield sse_format(sse_chunk(chunk_id, model, content_delta=delta.get("text", ""))).encode()
                            elif delta.get("type") == "input_json_delta":
                                yield sse_format(sse_chunk(chunk_id, model, tool_calls=[{
                                    "index": tool_call_index - 1,
                                    "function": {"arguments": delta.get("partial_json", "")},
                                }])).encode()
                        elif event_type == "message_delta":
                            delta = data.get("delta", {})
                            stop_reason = delta.get("stop_reason", "end_turn")
                            finish = "length" if stop_reason == "max_tokens" else "tool_calls" if stop_reason == "tool_use" else "stop"
                            usage_data = data.get("usage", {})
                            usage = None
                            if usage_data:
                                usage = {
                                    "prompt_tokens": usage_data.get("input_tokens", 0),
                                    "completion_tokens": usage_data.get("output_tokens", 0),
                                    "total_tokens": usage_data.get("input_tokens", 0) + usage_data.get("output_tokens", 0),
                                }
                            yield sse_format(sse_chunk(chunk_id, model, finish_reason=finish, usage=usage)).encode()
                        elif event_type == "message_stop":
                            yield sse_done().encode()
                            return
    except Exception as exc:
        logger.error("OpenCode Go stream interrupted: {}", exc)
        yield sse_format({"error": {"message": f"stream_interrupted: {exc}", "type": "stream_error"}}).encode()
        yield sse_done().encode()
