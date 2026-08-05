"""Caller capability policy middleware.

Enforces small, local blast-radius controls keyed by ``X-Caller-Skill``.
This is intentionally not RBAC: no users, teams, virtual keys, or tenant model.
"""

from __future__ import annotations

import fnmatch
from typing import Any

from loguru import logger

from scillm.proxy.config import ProxyConfig
from scillm.proxy.middleware import BaseMiddleware, MiddlewareReject


_TEXT_MODEL_NAMES = {
    "text",
    "local-text",
    "moonshot-text",
    "chutes-deepseek",
    "chutes-kimi",
    "chutes-qwen",
    "chutes-qwen-large",
    "deepseek-direct",
    "gemini-flash",
    "gemini-flash-oauth",
    "gemini-flash-high",
}


def _header_value(headers: dict[str, Any], name: str) -> str:
    needle = name.lower()
    for key, value in headers.items():
        if str(key).lower() == needle:
            return str(value).strip()
    return ""


def _caller_from_request(request: dict[str, Any]) -> str:
    caller = str(request.get("_caller_skill") or "").strip()
    if caller:
        return caller
    headers = request.get("_headers") if isinstance(request.get("_headers"), dict) else {}
    return _header_value(headers, "x-caller-skill")


def _iter_content_parts(request: dict[str, Any]):
    messages = request.get("messages") or []
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    yield part
        elif isinstance(content, dict):
            yield content


def _mime_from_data_uri(url: str) -> str:
    if not url.startswith("data:") or "," not in url:
        return ""
    header = url.split(",", 1)[0]
    return header[5:].split(";", 1)[0].lower()


def request_capabilities(request: dict[str, Any]) -> dict[str, bool]:
    """Return content/tool capability use for a request."""
    has_tools = bool(request.get("tools"))
    has_files = False
    has_images = False
    has_pdfs = False

    for key in ("file", "files", "file_path", "file_paths", "path", "paths", "url", "urls"):
        if request.get(key):
            has_files = True

    for part in _iter_content_parts(request):
        part_type = str(part.get("type") or "").lower()
        if part_type in {"image", "image_url"} or "image_url" in part:
            has_files = True
            has_images = True
            image_url = part.get("image_url") if isinstance(part.get("image_url"), dict) else {}
            mime = _mime_from_data_uri(str(image_url.get("url") or part.get("url") or ""))
            if mime == "application/pdf":
                has_pdfs = True
                has_images = False
        if part_type == "document":
            has_files = True
            has_pdfs = True
        if "inlineData" in part and isinstance(part["inlineData"], dict):
            has_files = True
            mime = str(part["inlineData"].get("mimeType") or "").lower()
            if mime.startswith("image/"):
                has_images = True
            elif mime == "application/pdf":
                has_pdfs = True
            else:
                has_files = True

    return {
        "tools": has_tools,
        "files": has_files,
        "images": has_images,
        "pdfs": has_pdfs,
        "streaming": bool(request.get("stream")),
    }


def _is_text_model(model: str) -> bool:
    model_lower = model.strip().lower()
    return model_lower in _TEXT_MODEL_NAMES or model_lower.startswith("text")


def policy_target_model(request: dict[str, Any]) -> str:
    """Return model identifier to use for allow-list checks."""
    if request.get("_scillm_pool"):
        return str(request["_scillm_pool"])
    model = str(request.get("model") or "")
    caps = request_capabilities(request)
    if (caps["images"] or caps["pdfs"]) and _is_text_model(model):
        return "vlm"
    return model


def _matches_any(value: str, patterns: list[str]) -> bool:
    value_lower = value.lower()
    return any(fnmatch.fnmatchcase(value_lower, pattern.lower()) for pattern in patterns)


class CallerPolicyMiddleware(BaseMiddleware):
    """Enforce configured caller profiles."""

    def __init__(self, config: ProxyConfig) -> None:
        self._config = config

    def _fallback_candidates(self, model: str) -> list[str]:
        candidates = [model]
        seen = {model}
        pending = list(self._config.fallbacks.get(model, []))
        while pending:
            candidate = pending.pop(0)
            if candidate in seen:
                continue
            seen.add(candidate)
            candidates.append(candidate)
            pending.extend(self._config.fallbacks.get(candidate, []))
        return candidates

    @staticmethod
    def _reject(caller: str, reason: str) -> None:
        raise MiddlewareReject(f"Caller policy denied '{caller}': {reason}", status_code=403)

    async def pre_call(self, request: dict[str, Any]) -> dict | None:
        caller = _caller_from_request(request)
        profile = self._config.caller_profiles.get(caller)
        if profile is None:
            return request

        target_model = policy_target_model(request)
        if profile.allowed_models and not _matches_any(target_model, profile.allowed_models):
            self._reject(
                caller,
                f"model '{target_model}' is not in allowed_models={profile.allowed_models}",
            )

        fallback_seed = target_model
        if request.get("_scillm_pool"):
            fallback_seed = str(request.get("model") or target_model)
        denied_candidates = [
            candidate
            for candidate in self._fallback_candidates(fallback_seed)
            if _matches_any(candidate, profile.deny_model_patterns)
        ]
        if request.get("_scillm_pool"):
            selected_model = str(request.get("model") or "")
            if _matches_any(selected_model, profile.deny_model_patterns):
                denied_candidates.append(selected_model)
        if denied_candidates:
            self._reject(
                caller,
                f"model/fallback denied by pattern: {', '.join(dict.fromkeys(denied_candidates))}",
            )

        metadata = request.get("scillm_metadata") or request.get("_scillm_metadata") or {}
        if not isinstance(metadata, dict):
            self._reject(caller, "scillm_metadata must be an object")
        missing = [key for key in profile.require_scillm_metadata if not metadata.get(key)]
        if missing:
            self._reject(caller, f"missing required scillm_metadata keys: {', '.join(missing)}")

        caps = request_capabilities(request)
        if caps["tools"] and not profile.allow_tools:
            self._reject(caller, "tools are not allowed")
        if caps["files"] and not profile.allow_files:
            self._reject(caller, "files are not allowed")
        if caps["images"] and not profile.allow_images:
            self._reject(caller, "images are not allowed")
        if caps["pdfs"] and not profile.allow_pdfs:
            self._reject(caller, "PDFs are not allowed")
        if caps["streaming"] and not profile.allow_streaming:
            self._reject(caller, "streaming is not allowed")

        if profile.max_timeout_s is not None:
            max_timeout_ms = int(profile.max_timeout_s * 1000)
            current = request.get("_dynamic_timeout_ms")
            if current is None:
                request["_dynamic_timeout_ms"] = max_timeout_ms
            else:
                request["_dynamic_timeout_ms"] = min(int(current), max_timeout_ms)
            request["_policy_max_timeout_s"] = float(profile.max_timeout_s)
            request["_timeout_source"] = "caller_policy_cap"

        logger.debug("caller_policy: allowed caller={} model={}", caller, target_model)
        return request


def caller_profiles_for_capabilities(config: ProxyConfig) -> dict[str, dict[str, Any]]:
    """Serialize caller profiles for the authenticated capabilities endpoint."""
    out: dict[str, dict[str, Any]] = {}
    for caller, profile in config.caller_profiles.items():
        out[caller] = {
            "allowed_models": profile.allowed_models,
            "deny_model_patterns": profile.deny_model_patterns,
            "require_scillm_metadata": profile.require_scillm_metadata,
            "allow_tools": profile.allow_tools,
            "allow_files": profile.allow_files,
            "allow_images": profile.allow_images,
            "allow_pdfs": profile.allow_pdfs,
            "allow_streaming": profile.allow_streaming,
            "max_timeout_s": profile.max_timeout_s,
        }
    return out
