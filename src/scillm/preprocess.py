from __future__ import annotations

"""
Lightweight request pre-processing helpers for scillm.batch.

This module intentionally avoids any network or filesystem I/O. It only
normalizes explicit IO fields into a structured `artifacts` dict so downstream
logic can detect multimodal requests consistently.
"""

from typing import Any, Dict, List


def _ensure_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    if isinstance(value, tuple):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def expand_requests_io(requests: List[Dict]) -> List[Dict]:
    """
    Normalize explicit IO fields onto `artifacts`:
      - url -> artifacts.urls
      - urls -> artifacts.urls
      - file_path -> artifacts.file_paths
      - paths -> artifacts.file_paths

    This is intentionally conservative: it does NOT fetch URLs or read files.
    """
    out: List[Dict] = []
    for req in requests or []:
        if not isinstance(req, dict):
            out.append(req)
            continue
        new_req = dict(req)
        artifacts = dict(new_req.get("artifacts") or {})
        urls = _ensure_list(new_req.get("url")) + _ensure_list(new_req.get("urls"))
        paths = _ensure_list(new_req.get("file_path")) + _ensure_list(new_req.get("paths"))

        if urls:
            existing = _ensure_list(artifacts.get("urls"))
            artifacts["urls"] = existing + urls
        if paths:
            existing = _ensure_list(artifacts.get("file_paths"))
            artifacts["file_paths"] = existing + paths

        if artifacts:
            new_req["artifacts"] = artifacts
        out.append(new_req)
    return out
