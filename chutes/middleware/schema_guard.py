"""JSON Schema validation middleware with course correction.

When a request includes `json_schema` field, this middleware:
1. Validates the parsed JSON response against the schema
2. On failure, retries with a helpful error message explaining exactly what failed
3. After max retries, raises SchemaValidationFailed for fallback cascade

The course correction message is specific:
- Which fields failed validation
- What type was expected vs received
- Which required fields are missing
- What enum values are allowed

Usage:
    POST /v1/chat/completions
    {
        "model": "text",
        "messages": [{"role": "user", "content": "Return user info"}],
        "response_format": {"type": "json_object"},
        "json_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
                "email": {"type": "string", "format": "email"}
            },
            "required": ["name", "age", "email"]
        },
        "schema_retries": 2
    }

Response on validation failure includes detailed error:
    {
        "error": {
            "message": "Schema validation failed after 2 retries...",
            "type": "schema_validation_failed",
            "validation_errors": [...]
        }
    }
"""
from __future__ import annotations

import contextvars
import json
from typing import Any

from loguru import logger

from scillm.proxy.middleware import BaseMiddleware


class SchemaValidationFailed(Exception):
    """Raised when schema validation fails after all retries.

    Contains detailed validation errors for the response.
    """

    def __init__(self, errors: list[dict], raw_content: str) -> None:
        self.errors = errors
        self.raw_content = raw_content
        error_summary = "; ".join(e.get("message", str(e)) for e in errors[:3])
        super().__init__(f"Schema validation failed: {error_summary}")


class SchemaRetryNeeded(Exception):
    """Signal to retry with correction hint appended."""

    def __init__(self, hint: str) -> None:
        self.hint = hint
        super().__init__(hint)


# Context vars to track state between pre_call and post_call
_schema: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "schema_guard_schema", default=None
)
_max_retries: contextvars.ContextVar[int] = contextvars.ContextVar(
    "schema_guard_max_retries", default=2
)
_attempt: contextvars.ContextVar[int] = contextvars.ContextVar(
    "schema_guard_attempt", default=0
)
_original_messages: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "schema_guard_original_messages", default=None
)


def _validate_schema(data: Any, schema: dict) -> list[dict]:
    """Validate data against JSON Schema, return list of detailed errors.

    Each error dict contains:
    - path: JSON path to the failing field (e.g., "$.user.age")
    - message: Human-readable error message
    - expected: What was expected
    - actual: What was received
    """
    errors: list[dict] = []

    try:
        import jsonschema
        from jsonschema import Draft7Validator, ValidationError

        validator = Draft7Validator(schema)
        for error in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
            path = "$.{}".format(".".join(str(p) for p in error.path)) if error.path else "$"

            err_dict: dict[str, Any] = {
                "path": path,
                "message": _format_error_message(error),
            }

            # Add expected/actual for type errors
            if error.validator == "type":
                err_dict["expected"] = error.validator_value
                err_dict["actual"] = type(error.instance).__name__
            elif error.validator == "enum":
                err_dict["expected"] = f"one of {error.validator_value}"
                err_dict["actual"] = repr(error.instance)
            elif error.validator == "required":
                err_dict["expected"] = f"required field"
                err_dict["actual"] = "missing"
            elif error.validator == "minLength":
                err_dict["expected"] = f"minimum length {error.validator_value}"
                err_dict["actual"] = f"length {len(error.instance)}"
            elif error.validator == "maxLength":
                err_dict["expected"] = f"maximum length {error.validator_value}"
                err_dict["actual"] = f"length {len(error.instance)}"
            elif error.validator == "minimum":
                err_dict["expected"] = f">= {error.validator_value}"
                err_dict["actual"] = error.instance
            elif error.validator == "maximum":
                err_dict["expected"] = f"<= {error.validator_value}"
                err_dict["actual"] = error.instance
            elif error.validator == "pattern":
                err_dict["expected"] = f"match pattern {error.validator_value}"
                err_dict["actual"] = repr(error.instance)

            errors.append(err_dict)

    except ImportError:
        logger.warning("jsonschema not installed — schema validation skipped")
        return []
    except Exception as e:
        errors.append({"path": "$", "message": f"Validation error: {e}"})

    return errors


def _format_error_message(error) -> str:
    """Format a jsonschema ValidationError into a clear, actionable message."""
    path = ".".join(str(p) for p in error.path) if error.path else "root"

    if error.validator == "type":
        expected = error.validator_value
        if isinstance(expected, list):
            expected = " or ".join(expected)
        actual = type(error.instance).__name__
        return f"Field '{path}': expected {expected}, got {actual} ({repr(error.instance)[:50]})"

    elif error.validator == "required":
        missing = error.validator_value
        if isinstance(missing, list):
            missing = ", ".join(f"'{m}'" for m in missing)
        # Extract actual missing field from error message
        if "'[" in error.message or "'" in error.message:
            return f"Missing required field(s) at '{path}': {error.message}"
        return f"Missing required field(s) at '{path}': {missing}"

    elif error.validator == "enum":
        allowed = ", ".join(repr(v) for v in error.validator_value)
        return f"Field '{path}': value {repr(error.instance)} not in allowed values [{allowed}]"

    elif error.validator == "minLength":
        return f"Field '{path}': string too short (min {error.validator_value}, got {len(error.instance)})"

    elif error.validator == "maxLength":
        return f"Field '{path}': string too long (max {error.validator_value}, got {len(error.instance)})"

    elif error.validator == "minimum":
        return f"Field '{path}': value {error.instance} is below minimum {error.validator_value}"

    elif error.validator == "maximum":
        return f"Field '{path}': value {error.instance} is above maximum {error.validator_value}"

    elif error.validator == "pattern":
        return f"Field '{path}': value {repr(error.instance)[:30]} does not match pattern {error.validator_value}"

    elif error.validator == "format":
        return f"Field '{path}': value {repr(error.instance)[:30]} is not a valid {error.validator_value}"

    elif error.validator == "additionalProperties":
        return f"Field '{path}': unexpected additional property not allowed"

    elif error.validator == "minItems":
        return f"Field '{path}': array too short (min {error.validator_value} items, got {len(error.instance)})"

    elif error.validator == "maxItems":
        return f"Field '{path}': array too long (max {error.validator_value} items, got {len(error.instance)})"

    elif error.validator == "uniqueItems":
        return f"Field '{path}': array contains duplicate items"

    else:
        return f"Field '{path}': {error.message}"


def _build_correction_prompt(errors: list[dict], schema: dict) -> str:
    """Build a helpful correction prompt that tells the model exactly what to fix."""
    lines = [
        "Your JSON response failed schema validation. Please fix the following errors:",
        "",
    ]

    for i, err in enumerate(errors, 1):
        path = err.get("path", "$")
        msg = err.get("message", str(err))
        lines.append(f"{i}. {msg}")

        if "expected" in err and "actual" in err:
            lines.append(f"   Expected: {err['expected']}")
            lines.append(f"   Got: {err['actual']}")

    lines.extend([
        "",
        "Return a corrected JSON response that matches this schema:",
        "```json",
        json.dumps(schema, indent=2),
        "```",
        "",
        "Return ONLY the corrected JSON, no explanation.",
    ])

    return "\n".join(lines)


def _extract_response_text(response: Any) -> str | None:
    """Pull text content from OpenAI-style response."""
    if isinstance(response, dict):
        choices = response.get("choices", [])
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message", {})
            if isinstance(msg, dict):
                return msg.get("content")
    return None


class SchemaGuard(BaseMiddleware):
    """Validates JSON responses against provided schema with course correction."""

    async def pre_call(self, request: dict) -> dict | None:
        schema = request.pop("json_schema", None)
        max_retries = request.pop("schema_retries", 2)

        _schema.set(schema)
        _max_retries.set(max_retries)

        # Only reset attempt counter on first call (not retries)
        if _original_messages.get() is None:
            _attempt.set(0)
            _original_messages.set(list(request.get("messages", [])))

        if schema:
            logger.debug(
                "schema_guard: schema loaded, max_retries={}",
                max_retries,
            )

        return request

    async def post_call(self, request: dict, response: Any) -> Any:
        schema = _schema.get()
        if schema is None:
            return response

        content = _extract_response_text(response)
        if content is None:
            logger.debug("schema_guard: no content to validate")
            return response

        # Parse JSON (should already be valid from JsonGuard)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as e:
            logger.warning("schema_guard: content is not valid JSON: {}", e)
            return response

        # Validate against schema
        errors = _validate_schema(parsed, schema)

        if not errors:
            logger.debug("schema_guard: validation passed")
            # Add validation success to response
            if isinstance(response, dict):
                response["schema_validated"] = True
            # Reset state for next request
            _original_messages.set(None)
            return response

        # Validation failed
        attempt = _attempt.get() + 1
        _attempt.set(attempt)
        max_retries = _max_retries.get()

        logger.warning(
            "schema_guard: validation failed (attempt {}/{}): {}",
            attempt,
            max_retries,
            "; ".join(e.get("message", str(e)) for e in errors[:3]),
        )

        if attempt >= max_retries:
            logger.error("schema_guard: max retries exceeded, failing")
            _original_messages.set(None)  # Reset state
            raise SchemaValidationFailed(errors, content)

        # Build correction prompt and signal retry
        correction = _build_correction_prompt(errors, schema)
        logger.info("schema_guard: retrying with correction prompt")

        # Store correction info for the retry mechanism
        if isinstance(response, dict):
            response["_schema_retry"] = {
                "correction_prompt": correction,
                "errors": errors,
                "attempt": attempt,
            }

        raise SchemaRetryNeeded(correction)

    async def on_error(self, request: dict, error: Exception) -> None:
        """Reset state on error."""
        _original_messages.set(None)
        _attempt.set(0)
