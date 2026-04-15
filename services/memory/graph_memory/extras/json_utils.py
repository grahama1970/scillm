"""Minimal JSON helpers used across projects (lifted from LiteLLM extras).

If LiteLLM is installed, prefer its implementation; otherwise provide a
compatible local copy to avoid cross-repo import fragility.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Union

try:  # pragma: no cover - optional dependency
    # Prefer the upstream module if available
    from litellm.extras.json_utils import (  # type: ignore
        PathEncoder,  # noqa: F401
        json_serialize,  # noqa: F401
        load_json_file,  # noqa: F401
        save_json_to_file,  # noqa: F401
        parse_json,  # noqa: F401
        clean_json_string,  # noqa: F401
    )
except Exception as exc:  # Fallback: embed a minimal implementation
    try:  # pragma: no cover - optional dependency
        from json_repair import repair_json
    except ImportError:  # pragma: no cover - fallback when package missing
        repair_json = None  # type: ignore

    __all__ = [
        "PathEncoder",
        "json_serialize",
        "load_json_file",
        "save_json_to_file",
        "parse_json",
        "clean_json_string",
    ]

    from loguru import logger
    logger.error("Suppressed error in json_utils: {}", exc)

    class PathEncoder(json.JSONEncoder):
        def default(self, obj: Any) -> Any:  # noqa: D401 - short override
            if isinstance(obj, Path):
                return str(obj)
            return super().default(obj)

    def json_serialize(data: Any, *, handle_paths: bool = False, **kwargs: Any) -> str:
        if handle_paths:
            return json.dumps(data, cls=PathEncoder, **kwargs)
        return json.dumps(data, **kwargs)

    def load_json_file(file_path: str) -> Any:
        if not os.path.exists(file_path):
            logger.warning("File does not exist: {}", file_path)
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as file:
                return json.load(file)
        except json.JSONDecodeError:
            logger.warning("JSON decoding error; retrying with utf-8-sig: {}", file_path)
            with open(file_path, "r", encoding="utf-8-sig") as file:
                return json.load(file)

    def save_json_to_file(data: Any, file_path: str) -> None:
        directory = os.path.dirname(file_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=4)

    _JSON_PATTERN = re.compile(r"(\[.*\]|\{.*\})", re.DOTALL)

    def parse_json(content: str) -> Union[dict, list, str]:
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        match = _JSON_PATTERN.search(content)
        if match:
            content = match.group(1)
        if repair_json is None:
            logger.error("json_repair not installed; returning original content")
            return content

        try:
            repaired = repair_json(content, return_objects=True)
            if isinstance(repaired, (dict, list)):
                return repaired
            return json.loads(repaired)
        except Exception as exc:
            logger.error("Returning original content after repair failure")
            return content

    def clean_json_string(content: Union[str, dict, list], *, return_dict: bool = False) -> Union[str, dict, list]:
        if isinstance(content, (dict, list)):
            return content if return_dict else json.dumps(content)

        cleaned = parse_json(content)
        if return_dict and isinstance(cleaned, (dict, list)):
            return cleaned
        if return_dict:
            return {}
        return json.dumps(cleaned) if isinstance(cleaned, (dict, list)) else str(cleaned)

