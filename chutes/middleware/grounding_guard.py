"""Source grounding verification middleware with course correction.

When a request includes `source` field, this middleware:
1. Extracts source text (inline string or file path)
2. After response, computes fuzzy token match score against source
3. If below threshold, retries with helpful correction message
4. Adds grounding_score to response

The course correction message is specific:
- Current grounding score vs required threshold
- Which parts of the response appear ungrounded
- Clear instruction to quote directly from source

Usage:
    POST /v1/chat/completions
    {
        "model": "chutes-deepseek",
        "messages": [{"role": "user", "content": "Summarize this"}],
        "source": "The actual source text to ground against...",
        "grounding_threshold": 0.7,
        "grounding_retries": 2
    }

Response includes:
    {
        "choices": [...],
        "grounding_score": 0.85,
        "grounding_verified": true,
        "grounding_attempts": 1
    }

Headers returned:
    x-scillm-grounding-score: 0.85
    x-scillm-grounding-verified: true
"""
from __future__ import annotations

import contextvars
import pathlib
from typing import Any

from loguru import logger

from scillm.proxy.middleware import BaseMiddleware


# Try to import rapidfuzz for token matching
try:
    from rapidfuzz.fuzz import token_set_ratio as _token_set_ratio
    from rapidfuzz.fuzz import partial_ratio as _partial_ratio
except ImportError:
    _token_set_ratio = None
    _partial_ratio = None
    logger.warning("rapidfuzz not installed — grounding verification disabled")


class GroundingFailed(Exception):
    """Raised when grounding fails after all retries.

    Contains the grounding details for the error response.
    """

    def __init__(self, score: float, threshold: float, attempts: int, hint: str) -> None:
        self.score = score
        self.threshold = threshold
        self.attempts = attempts
        self.hint = hint
        super().__init__(
            f"Grounding failed: score {score:.2f} < threshold {threshold:.2f} after {attempts} attempts"
        )


class GroundingRetryNeeded(Exception):
    """Signal to retry with correction hint appended."""

    def __init__(self, hint: str) -> None:
        self.hint = hint
        super().__init__(hint)


# Context vars to pass data between pre_call and post_call
_grounding_source: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "grounding_source", default=None
)
_grounding_threshold: contextvars.ContextVar[float] = contextvars.ContextVar(
    "grounding_threshold", default=0.7
)
_grounding_max_retries: contextvars.ContextVar[int] = contextvars.ContextVar(
    "grounding_max_retries", default=2
)
_grounding_attempt: contextvars.ContextVar[int] = contextvars.ContextVar(
    "grounding_attempt", default=0
)
_original_messages: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "grounding_original_messages", default=None
)


def _resolve_source(source: str | list[str] | None) -> str | None:
    """Resolve source text for grounding verification.

    Accepts inline text or file paths (auto-detected). Multiple sources concatenated.
    """
    if source is None:
        return None
    items = [source] if isinstance(source, str) else source
    parts: list[str] = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        p = pathlib.Path(item)
        if p.is_file():
            try:
                parts.append(p.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                parts.append(item)
        else:
            parts.append(item)
    return "\n".join(parts) if parts else None


def _check_grounding(response_text: str, source_text: str) -> tuple[float, list[str]]:
    """Check if response_text is grounded in source_text using fuzzy token matching.

    Returns (similarity_score, list_of_ungrounded_sentences).
    Score is 0.0–1.0. Returns (1.0, []) if rapidfuzz not installed.
    """
    if _token_set_ratio is None:
        return 1.0, []
    if not response_text or not source_text:
        return 0.0, ["Empty response or source"]

    # Overall score using token set ratio
    score = _token_set_ratio(response_text.lower(), source_text.lower()) / 100.0

    # Find ungrounded sentences for helpful feedback
    ungrounded: list[str] = []
    if _partial_ratio is not None and score < 0.9:
        # Split response into sentences and check each
        sentences = _split_sentences(response_text)
        source_lower = source_text.lower()

        for sentence in sentences:
            if len(sentence) < 20:  # Skip very short fragments
                continue
            # Check if this sentence has good overlap with source
            sentence_score = _partial_ratio(sentence.lower(), source_lower) / 100.0
            if sentence_score < 0.6:  # Sentence seems ungrounded
                ungrounded.append(sentence[:100] + "..." if len(sentence) > 100 else sentence)
                if len(ungrounded) >= 3:  # Limit to 3 examples
                    break

    return score, ungrounded


def _split_sentences(text: str) -> list[str]:
    """Simple sentence splitter."""
    import re
    # Split on sentence-ending punctuation
    sentences = re.split(r'(?<=[.!?])\s+', text)
    return [s.strip() for s in sentences if s.strip()]


def _build_correction_prompt(
    score: float,
    threshold: float,
    ungrounded: list[str],
    source_preview: str,
    attempt: int,
) -> str:
    """Build a helpful correction prompt explaining exactly what to fix."""
    lines = [
        f"Your response is not sufficiently grounded in the source text.",
        f"",
        f"Current grounding score: {score:.0%}",
        f"Required threshold: {threshold:.0%}",
        f"",
    ]

    if ungrounded:
        lines.append("The following parts of your response appear to NOT be from the source:")
        for i, sentence in enumerate(ungrounded, 1):
            lines.append(f"  {i}. \"{sentence}\"")
        lines.append("")

    # Progressive hints based on attempt number
    if attempt == 1:
        lines.extend([
            "Please revise your response to:",
            "- Use only information from the provided source text",
            "- Quote or closely paraphrase the source material",
            "- Remove any information not found in the source",
        ])
    else:
        lines.extend([
            "IMPORTANT: Your response MUST be grounded in the source.",
            "- Quote DIRECTLY from the source text using exact phrases",
            "- Do NOT add any information not explicitly stated in the source",
            "- If the source doesn't contain the answer, say so",
            "",
            "Source text preview (first 500 chars):",
            f'"""{source_preview[:500]}"""',
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


class GroundingGuard(BaseMiddleware):
    """Verifies response is grounded in provided source text with course correction."""

    async def pre_call(self, request: dict) -> dict | None:
        source = request.pop("source", None)
        threshold = request.pop("grounding_threshold", 0.7)
        max_retries = request.pop("grounding_retries", 2)

        source_text = _resolve_source(source)
        _grounding_source.set(source_text)
        _grounding_threshold.set(threshold)
        _grounding_max_retries.set(max_retries)

        # Only reset attempt counter on first call (not retries)
        if _original_messages.get() is None:
            _grounding_attempt.set(0)
            _original_messages.set(list(request.get("messages", [])))

        if source_text:
            logger.debug(
                "grounding_guard: source text loaded ({} chars), threshold={}, max_retries={}",
                len(source_text),
                threshold,
                max_retries,
            )

        return request

    async def post_call(self, request: dict, response: Any) -> Any:
        source_text = _grounding_source.get()
        if source_text is None:
            return response

        threshold = _grounding_threshold.get()
        content = _extract_response_text(response)

        if content is None:
            logger.debug("grounding_guard: no content to verify")
            return response

        score, ungrounded = _check_grounding(content, source_text)
        verified = score >= threshold
        attempt = _grounding_attempt.get() + 1
        _grounding_attempt.set(attempt)
        max_retries = _grounding_max_retries.get()

        logger.info(
            "grounding_guard: score={:.2f}, threshold={:.2f}, verified={}, attempt={}/{}",
            score,
            threshold,
            verified,
            attempt,
            max_retries,
        )

        if verified:
            # Success - add grounding info to response
            if isinstance(response, dict):
                response["grounding_score"] = round(score, 3)
                response["grounding_verified"] = True
                response["grounding_attempts"] = attempt
                response["_grounding_headers"] = {
                    "x-scillm-grounding-score": str(round(score, 3)),
                    "x-scillm-grounding-verified": "true",
                    "x-scillm-grounding-attempts": str(attempt),
                }
            # Reset state for next request
            _original_messages.set(None)
            return response

        # Grounding failed
        if attempt >= max_retries:
            logger.warning(
                "grounding_guard: max retries exceeded, score={:.2f} < threshold={:.2f}",
                score,
                threshold,
            )
            # Still return response but mark as unverified
            if isinstance(response, dict):
                response["grounding_score"] = round(score, 3)
                response["grounding_verified"] = False
                response["grounding_attempts"] = attempt
                response["grounding_ungrounded_examples"] = ungrounded
                response["_grounding_headers"] = {
                    "x-scillm-grounding-score": str(round(score, 3)),
                    "x-scillm-grounding-verified": "false",
                    "x-scillm-grounding-attempts": str(attempt),
                }
            _original_messages.set(None)
            return response

        # Build correction prompt and signal retry
        correction = _build_correction_prompt(
            score=score,
            threshold=threshold,
            ungrounded=ungrounded,
            source_preview=source_text,
            attempt=attempt,
        )
        logger.info("grounding_guard: retrying with correction prompt (attempt {})", attempt)

        # Store correction info for the retry mechanism
        if isinstance(response, dict):
            response["_grounding_retry"] = {
                "correction_prompt": correction,
                "score": score,
                "threshold": threshold,
                "ungrounded": ungrounded,
                "attempt": attempt,
            }

        raise GroundingRetryNeeded(correction)

    async def on_error(self, request: dict, error: Exception) -> None:
        """Reset state on error."""
        _original_messages.set(None)
        _grounding_attempt.set(0)
