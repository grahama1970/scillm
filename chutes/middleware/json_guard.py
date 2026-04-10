"""JSON response validation guard for unreliable providers.

When a caller requests response_format: {"type": "json_object"} but the
provider returns prose instead of valid JSON, this middleware:
1. Attempts multi-stage repair (brace trimming, json_repair library).
2. Replaces content with repaired JSON if fixable.
3. Raises JsonValidationFailed if repair fails — caught by the app layer
   to trigger fallback retry through the next provider in the cascade.

Migrated from CustomLogger to BaseMiddleware interface.
"""
from __future__ import annotations

import contextvars
import json
from typing import Any

from loguru import logger

from scillm.proxy.middleware import BaseMiddleware


class JsonValidationFailed(Exception):
    """Raised when JSON repair fails after all stages.

    Caught by app.py to trigger fallback retry through the cascade.
    """

    def __init__(self, raw_text: str) -> None:
        self.raw_text = raw_text
        super().__init__(f"JSON validation failed after repair: {raw_text[:120]}")

_json_mode_requested: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "json_mode_requested", default=False
)


def _try_repair(text: str) -> str | None:
    """Attempt JSON repair before rejecting.

    Returns repaired JSON string if repairable, None otherwise.
    """
    # Stage 1: trim to outer braces
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]
            json.loads(candidate)
            return candidate
    except Exception:
        pass
    # Stage 2: try json_repair library
    try:
        from json_repair import repair_json

        repaired = repair_json(text, return_objects=False)
        if isinstance(repaired, str):
            json.loads(repaired)
            return repaired
    except Exception:
        pass
    return None


def validate_json_response(response_text: str) -> bool:
    """Standalone validator: reject non-JSON when JSON was requested.

    Returns True (pass) or False (fail).
    Kept for backward compatibility.
    """
    if not _json_mode_requested.get(False):
        return True
    try:
        json.loads(response_text)
        return True
    except (json.JSONDecodeError, TypeError, ValueError):
        if _try_repair(response_text) is not None:
            return True
        return False


def _extract_response_text(response: Any) -> str | None:
    """Pull text content from various response shapes."""
    if isinstance(response, dict):
        # OpenAI-style response dict
        choices = response.get("choices", [])
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message", {})
            if isinstance(msg, dict):
                return msg.get("content")
    return None


class JsonGuard(BaseMiddleware):
    """Validates JSON responses when json_object/json_schema format requested."""

    async def pre_call(self, request: dict) -> dict | None:
        rf = request.get("response_format")
        expect_json = request.get("_expect_json", False)

        if isinstance(rf, dict) and rf.get("type") in ("json_object", "json_schema"):
            _json_mode_requested.set(True)
            logger.debug("json_guard: JSON mode requested via response_format")
        elif expect_json:
            # x-expect-json header: activate JSON repair for providers that don't support response_format
            _json_mode_requested.set(True)
            logger.debug("json_guard: JSON mode requested via x-expect-json header")
        else:
            _json_mode_requested.set(False)
        return request

    async def post_call(self, request: dict, response: Any) -> Any:
        if not _json_mode_requested.get(False):
            return response

        text = _extract_response_text(response)
        if text is None:
            return response

        # Fast path: valid JSON
        try:
            json.loads(text)
            return response
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        # Try repair
        repaired = _try_repair(text)
        if repaired is not None:
            logger.info("json_guard: repaired malformed JSON response")
            if isinstance(response, dict):
                choices = response.get("choices", [])
                if choices and isinstance(choices[0], dict):
                    msg = choices[0].get("message", {})
                    if isinstance(msg, dict):
                        msg["content"] = repaired
            return response

        logger.warning("json_guard: response is not valid JSON — raising for fallback retry")
        raise JsonValidationFailed(text)
