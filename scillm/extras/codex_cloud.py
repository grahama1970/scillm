"""
DEPRECATED/EXPERIMENTAL — codex-cloud

This module previously provided a "codex-cloud" lane. There is no public,
stable Codex Cloud tasks API; to avoid confusion with normal OpenAI-compatible
gateways, this feature is disabled by default.

To acknowledge the risk and re-enable temporarily, set:
  SCILLM_ENABLE_CODEX_CLOUD=1 and SCILLM_EXPERIMENTAL_CODEX_CLOUD=1

Prefer: codex-agent or your OpenAI-compatible gateway for live best-of-N.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_DIR = ROOT / "extras" / "js"
BRIDGE_PATH = BRIDGE_DIR / "codex_cloud_bridge.mjs"

USE_NODE = os.getenv("SCILLM_CODEX_CLOUD_USE_NODE", "").lower() in {"1", "true", "yes"}

# Hard gate: disabled unless explicitly enabled
if os.getenv("SCILLM_ENABLE_CODEX_CLOUD", "").lower() not in {"1", "true", "yes"}:
    raise RuntimeError(
        "codex-cloud is disabled: no public Codex Cloud tasks API. "
        "Use codex-agent or your OpenAI-compatible gateway. Set SCILLM_ENABLE_CODEX_CLOUD=1 "
        "only if you understand the risks."
    )


def _ensure_experimental_enabled() -> None:
    if os.getenv("SCILLM_EXPERIMENTAL_CODEX_CLOUD", "").lower() not in {"1", "true", "yes"}:
        raise RuntimeError(
            "codex-cloud is experimental; set SCILLM_EXPERIMENTAL_CODEX_CLOUD=1 to enable."
        )


def _node_bin() -> str:
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        raise RuntimeError("node is not installed; please install Node >=18 and rerun")
    return node


def _bridge_ok() -> None:
    if not BRIDGE_PATH.exists():
        raise RuntimeError(f"bridge not found: {BRIDGE_PATH}")
    # codex-ts-sdk must be installed under scillm/extras/js
    pkg = BRIDGE_DIR / "package.json"
    node_modules = BRIDGE_DIR / "node_modules"
    if not pkg.exists() or not node_modules.exists():
        raise RuntimeError(
            "codex-ts-sdk not installed. Run: cd scillm/extras/js && npm install"
        )


def generate_variants_cloud(
    *,
    task: str,
    best_of_n: int = 6,
    environment_id: str = "prod",
    timeout_ms: int = 90_000,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a best-of-N remote code generation task.
    Python SDK path (default) or Node bridge fallback.
    Returns a dict with at least { "taskId"|"id", "status"?, "diff"? }.
    """
    _ensure_experimental_enabled()
    if not USE_NODE:
        # Python SDK simple path (honors CODEX_CLOUD_ECHO)
        from scillm_codex_sdk import CodexCloudClient
        client = CodexCloudClient()
        # Reuse 'prompt' as 'task' text; map n to best-of-N if server supports it via extra
        data = client.generate_variants(prompt=task, n=best_of_n or 3, extra={"environment_id": environment_id})
        # Normalize to legacy shape for variants_to_scillm
        first = data.get("variants", [{}])[0] if isinstance(data, dict) else {}
        return {"taskId": first.get("id") or "var-1", "status": "ok", "diff": first.get("content")}
    # Node fallback
    _bridge_ok()
    node = _node_bin()
    payload = {
        "environmentId": environment_id,
        "prompt": task,
        "bestOfN": best_of_n,
        "timeoutMs": timeout_ms,
    }
    if base_url:
        payload["baseUrl"] = base_url
    proc = subprocess.Popen(
        [node, str(BRIDGE_PATH)],
        cwd=str(BRIDGE_DIR),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = proc.communicate(json.dumps(payload), timeout=(timeout_ms / 1000 + 15))
    if proc.returncode != 0:
        raise RuntimeError(f"codex-cloud bridge failed ({proc.returncode}): {err or out}")
    return json.loads(out)


def variants_to_scillm(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert a cloud result to a SciLLM-ish variants structure (single diff as one variant).
    In a richer integration we would map each attempt to a variant; for PoC we attach the diff.
    """
    diff = result.get("diff") or {}
    return {
        "variants": [
            {
                "id": result.get("taskId") or result.get("id"),
                "meta": {
                    "status": result.get("status"),
                    "attemptsCount": result.get("attemptsCount"),
                },
                "diff": diff,
            }
        ]
    }


def chat_cloud(
    *,
    messages: List[Dict[str, Any]],
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    reasoning: Optional[Dict[str, Any]] = None,
    env_label: Optional[str] = None,
    env_id: Optional[str] = None,
    best_of_n: Optional[int] = None,
    timeout_s: int = 300,
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Familiar, OpenAI-shaped helper. Returns {choices:[{message:{content}}]}.
    Uses Python SDK by default. Honors SCILLM_CODEX_CLOUD_USE_NODE for fallback (adapter not used here).
    """
    _ensure_experimental_enabled()
    if not USE_NODE:
        from scillm_codex_sdk import CodexCloudClient
        client = CodexCloudClient(timeout_s=timeout_s)
        # Return OpenAI-shaped dict (non-streaming)
        return client.chat(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning=reasoning,
            extra=kwargs if kwargs else None,
        )
    # Node fallback: instruct to use the adapter server instead of direct helper
    raise RuntimeError("Set up the Python SDK (preferred) or run the FastAPI adapter for chat via HTTP.")
