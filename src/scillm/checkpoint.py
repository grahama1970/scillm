"""Checkpoint helpers for batch engine — write/resume JSONL progress files."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger


def serialize_result(result: dict) -> dict:
    """Make a result dict JSON-serializable by dumping pydantic models."""
    out = dict(result)
    resp = out.get("response")
    if resp is not None and hasattr(resp, "model_dump"):
        out["response"] = resp.model_dump()
    req = out.get("request")
    if req is not None:
        out["request"] = {k: v for k, v in req.items() if k != "api_key"}
    return out


def load_checkpoint(path: Path) -> dict[int, dict]:
    """Load existing checkpoint JSONL, returning {index: result_dict}."""
    completed: dict[int, dict] = {}
    if not path.exists():
        return completed
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                idx = item.get("index")
                if idx is not None:
                    completed[idx] = item
            except json.JSONDecodeError:
                logger.warning(f"Checkpoint line {line_no}: invalid JSON, skipping")
    return completed


def append_checkpoint(path: Path, result: dict) -> None:
    """Append a single result as a JSONL line to the checkpoint file."""
    serialized = serialize_result(result)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(serialized, default=str, ensure_ascii=False) + "\n")
        f.flush()
