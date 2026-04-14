"""Gemini OAuth provider - uses Gemini CLI for headless calls.

Bypasses API rate limits (20 RPD) by using OAuth credentials from ~/.gemini/.
Best for long-context tasks (1M tokens) where API quotas are prohibitive.

Usage in proxy config:
  - model_name: text-gemini-oauth
    scillm_params:
      provider: gemini-oauth
      model: gemini-2.5-flash  # or gemini-3-flash-preview
      timeout: 120
"""

import asyncio
import json
import logging
import shutil
import time
from typing import Any

import openai.types.chat

logger = logging.getLogger(__name__)

# Check if gemini CLI is available
GEMINI_CLI = shutil.which("gemini")


def is_available() -> bool:
    """Check if Gemini CLI is installed and OAuth credentials exist."""
    if not GEMINI_CLI:
        return False
    try:
        from pathlib import Path
        creds = Path.home() / ".gemini" / "oauth_creds.json"
        return creds.exists()
    except Exception:
        return False


async def complete(
    prompt: str,
    model: str | None = None,
    system: str | None = None,
    timeout: float = 120.0,
) -> openai.types.chat.ChatCompletion:
    """Call Gemini via CLI in headless mode.

    Args:
        prompt: The user prompt
        model: Model to use (e.g., gemini-2.5-flash). If None, uses CLI default.
        system: System prompt (prepended to user prompt)
        timeout: Timeout in seconds

    Returns:
        OpenAI ChatCompletion Pydantic object
    """
    if not GEMINI_CLI:
        raise RuntimeError("Gemini CLI not found. Install with: npm install -g @anthropic-ai/gemini-cli")

    # Build full prompt
    full_prompt = prompt
    if system:
        full_prompt = f"{system}\n\n{prompt}"

    # Build command (--sandbox false --yolo for headless Docker execution)
    cmd = [GEMINI_CLI, "-p", full_prompt, "--output-format", "json", "--sandbox", "false", "--yolo"]
    if model:
        cmd.extend(["-m", model])

    logger.debug(f"gemini_oauth: calling {model or 'default'} with {len(full_prompt)} chars")

    try:
        # Run subprocess
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )

        if proc.returncode != 0:
            error_msg = stderr.decode().strip() if stderr else f"Exit code {proc.returncode}"
            # Handle known exit codes
            if proc.returncode == 53:
                error_msg = "Turn limit exceeded"
            elif proc.returncode == 42:
                error_msg = "Input validation error"
            raise RuntimeError(f"Gemini CLI failed: {error_msg}")

        # Parse JSON response
        output = stdout.decode().strip()
        if not output:
            raise RuntimeError("Empty response from Gemini CLI")

        data = json.loads(output)
        response_text = data.get("response", "")
        stats = data.get("stats", {})

        # Extract token usage from stats
        total_tokens = 0
        prompt_tokens = 0
        completion_tokens = 0

        for model_stats in stats.get("models", {}).values():
            tokens = model_stats.get("tokens", {})
            prompt_tokens += tokens.get("prompt", 0)
            completion_tokens += tokens.get("candidates", 0)
            total_tokens += tokens.get("total", 0)

        # Return OpenAI ChatCompletion Pydantic object (not a dict)
        return openai.types.chat.ChatCompletion(
            id=f"chatcmpl-gemini-oauth-{data.get('session_id', 'unknown')[:8]}",
            choices=[
                openai.types.chat.chat_completion.Choice(
                    finish_reason="stop",
                    index=0,
                    message=openai.types.chat.ChatCompletionMessage(
                        content=response_text,
                        role="assistant",
                    ),
                )
            ],
            created=int(time.time()),
            model=model or "gemini-oauth",
            object="chat.completion",
            usage=openai.types.CompletionUsage(
                completion_tokens=completion_tokens,
                prompt_tokens=prompt_tokens,
                total_tokens=total_tokens,
            ),
        )

    except asyncio.TimeoutError:
        raise RuntimeError(f"Gemini CLI timed out after {timeout}s")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse Gemini CLI output: {e}")


async def complete_messages(
    messages: list[dict[str, str]],
    model: str | None = None,
    timeout: float = 120.0,
) -> openai.types.chat.ChatCompletion:
    """Call Gemini with OpenAI-style messages format.

    Args:
        messages: List of {"role": "system"|"user"|"assistant", "content": "..."}
        model: Model to use
        timeout: Timeout in seconds

    Returns:
        OpenAI ChatCompletion Pydantic object
    """
    # Extract system prompt and user messages
    system = None
    prompt_parts = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "system":
            system = content
        elif role == "user":
            prompt_parts.append(content)
        elif role == "assistant":
            # Include assistant messages as context
            prompt_parts.append(f"Assistant: {content}")

    prompt = "\n\n".join(prompt_parts)
    return await complete(prompt, model=model, system=system, timeout=timeout)


# Simple test
if __name__ == "__main__":
    import asyncio

    async def test():
        print(f"Gemini CLI available: {is_available()}")
        if is_available():
            result = await complete("What is 2+2? Reply with just the number.")
            print(f"Response: {result.choices[0].message.content}")
            print(f"Tokens: {result.usage}")
            print(f"Model: {result.model}")
            print(f"ID: {result.id}")

    asyncio.run(test())
