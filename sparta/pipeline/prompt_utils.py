"""
Shared helper for dumping the *first* prompt of a pipeline step to disk so
humans (and future runs) can debug quickly.  It writes both JSON and a
human-friendly Markdown view (system + user) beside it.

Usage:
    from sparta.pipeline.prompt_utils import dump_first_prompt_once
    dump_first_prompt_once(payload, default_path="sparta/data/run_artifacts/07b_first_prompt.json",
                           env_var="STEP07B_DEBUG_PROMPT_PATH",
                           messages=[{"role":"system","content":...},{"role":"user","content":...}])

Notes:
  - The dump happens only once per process (kept in _DUMPED registry).
  - If the resolved path is empty, dumping is skipped.
  - If only payload is provided, Markdown will contain the JSON payload.
  - The default_path is normalized to end with ".json"; a sibling ".md" is also written.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, List, Dict

_DUMPED: set[str] = set()


def _resolve_path(default_path: str | Path, env_var: str | None) -> Path | None:
    p = os.getenv(env_var) if env_var else None
    target = p if p is not None else str(default_path)
    if target is None or str(target).strip() == "":
        return None
    path = Path(target)
    if path.suffix != ".json":
        path = path.with_suffix(".json")
    return path


def _write_md(md_path: Path, messages: List[Dict[str, Any]] | None, payload: Any) -> None:
    md_path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    if messages:
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            lines.append(f"**{role}**")
            fence = "json" if isinstance(content, (dict, list)) else ""
            lines.append(f"```{fence}")
            lines.append(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2))
            lines.append("```")
            lines.append("")
    lines.append("**Payload**")
    lines.append("```json")
    try:
        lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception:
        lines.append(str(payload))
    lines.append("```")
    md_path.write_text("\n".join(lines), encoding="utf-8")


def dump_first_prompt_once(
    payload: Any,
    *,
    default_path: str | Path,
    env_var: str | None = None,
    messages: Iterable[Dict[str, Any]] | None = None,
    also_markdown: bool = True,
) -> Path | None:
    """
    Dump the first prompt encountered by a step. Returns the JSON path used, or None.

    Parameters:
      payload: Arbitrary JSON-serializable payload (or best-effort).
      default_path: default location for the JSON dump.
      env_var: optional env var to override/disable (empty string disables).
      messages: optional list of {role, content} to include in the markdown view.
      also_markdown: write a sibling .md view if True.
    """
    path = _resolve_path(default_path, env_var)
    if path is None:
        return None
    key = str(path.resolve())
    if key in _DUMPED:
        return path
    _DUMPED.add(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if also_markdown:
            _write_md(path.with_suffix(".md"), list(messages) if messages else None, payload)
        print(f"[prompt_utils] first prompt written to {path}", flush=True)
    except Exception as exc:  # pragma: no cover
        print(f"[prompt_utils] failed to write prompt dump: {exc}", flush=True)
    return path

