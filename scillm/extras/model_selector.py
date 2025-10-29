from __future__ import annotations

import json
import os
from dataclasses import dataclass
import re
from typing import Dict, Iterable, List, Optional

from .utilization_ranker import rank_chutes_by_availability_and_utilization, _get_models  # type: ignore


@dataclass
class DesiredSpec:
    kind: str  # 'text' | 'vlm' | 'tools'
    require_tools: bool = False
    require_json: bool = False
    # Room to extend later (context window, price, latency, etc.)


def _load_caps_override() -> Dict[str, Dict]:
    """Optional capability override via env JSON.

    Env: SCILLM_MODEL_CAPS_JSON='{"moonshotai/Kimi-K2-Instruct-0905": {"tools": true, "json": true}}'
    """
    raw = os.getenv("SCILLM_MODEL_CAPS_JSON", "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _infer_caps_from_id(model_id: str) -> Dict[str, bool]:
    mid = (model_id or "").lower()
    return {
        "vlm": any(k in mid for k in ["-vl", "vl-", "/vl", "vision"]),
        "tools": any(k in mid for k in ["-tool", "-function", "-tools", "-func"]),
        "json": any(k in mid for k in ["-json", "json"]),
    }


_SIZE_RE = re.compile(r"([0-9]+)\s*(B|T)\b", re.IGNORECASE)


def _approx_params(model_id: str) -> float:
    """Approximate parameter count from model id, in billions.

    Recognizes patterns like 235B, 78B, 1.8T (T as thousands of B).
    Falls back to 0 when unknown.
    """
    m = _SIZE_RE.search(model_id or "")
    if not m:
        return 0.0
    n = float(m.group(1))
    unit = (m.group(2) or "B").lower()
    return n * (1000.0 if unit == "t" else 1.0)


def _modality_ok(spec: DesiredSpec, caps: Dict[str, bool]) -> bool:
    if spec.kind == "vlm":
        return bool(caps.get("vlm"))
    # text/tools fall back to text path by default
    return True


def _distance(spec: DesiredSpec, caps: Dict[str, bool]) -> int:
    # Lower is better. Penalize missing required features.
    d = 0
    if spec.kind == "vlm" and not caps.get("vlm"):
        d += 10
    if spec.require_tools and not caps.get("tools"):
        d += 5
    if spec.require_json and not caps.get("json"):
        d += 2
    return d


def build_dynamic_model_list(
    *,
    spec: DesiredSpec,
    bases: Iterable[Dict[str, str]],
    max_candidates_per_base: int = 2,
) -> List[Dict]:
    """
    Build a Router model_list dynamically by probing each base's /v1/models,
    selecting models whose capabilities best match the desired spec, and then
    ordering by availability + utilization.

    bases: iterable of {"name", "api_base", "api_key"} (x-api-key) entries.
    Returns a list of Router-compatible entries.
    """
    out: List[Dict] = []
    caps_override = _load_caps_override()
    # Prefer peers near a declared primary when available
    primary_env = {
        "text": "CHUTES_TEXT_MODEL",
        "vlm": "CHUTES_VLM_MODEL",
        "tools": "CHUTES_TEXT_MODEL",
    }.get(spec.kind, "CHUTES_TEXT_MODEL")
    primary_id = os.getenv(primary_env, "").strip()
    primary_size = _approx_params(primary_id) if primary_id else 0.0

    for b in bases:
        base = (b.get("api_base") or "").strip()
        key = (b.get("api_key") or "").strip()
        if not (base and key):
            continue
        headers = {"x-api-key": key}

        try:
            ids = sorted(_get_models(base, headers))
        except Exception:
            ids = []
        if not ids:
            continue

        scored: List[tuple[int, float, str]] = []
        for mid in ids:
            caps = {**_infer_caps_from_id(mid), **caps_override.get(mid, {})}
            if not _modality_ok(spec, caps):
                continue
            caps_d = _distance(spec, caps)
            # Secondary key: absolute size distance to primary (peers first)
            size_d = abs(_approx_params(mid) - primary_size) if primary_size > 0 else -_approx_params(mid)
            scored.append((caps_d, size_d, mid))
        # Sort by: capability distance, then peer closeness (or largest first when no primary), then id
        scored.sort(key=lambda x: (x[0], x[1], x[2]))
        group = "chutes/text" if spec.kind != "vlm" else "chutes/vlm"
        for _, __, mid in scored[:max_candidates_per_base]:
            out.append(
                {
                    "model_name": group,
                    "litellm_params": {
                        "custom_llm_provider": "openai_like",
                        "api_base": base,
                        "api_key": None,
                        "extra_headers": {"x-api-key": key, "Authorization": f"Bearer {key}"},
                        "model": mid,
                        "order": 1,
                    },
                }
            )

    # Finally, apply availability + utilization ranking (idempotent if same input)
    return rank_chutes_by_availability_and_utilization(out)


__all__ = [
    "DesiredSpec",
    "build_dynamic_model_list",
]


# --------- Env-driven convenience (reduce friction) ---------

def _iter_bases_from_env(max_bases: int = 5):
    # numbered first
    for i in range(1, max_bases + 1):
        base = os.getenv(f"CHUTES_API_BASE_{i}", "").strip()
        key = os.getenv(f"CHUTES_API_KEY_{i}", "").strip()
        name = f"chutes-{i}"
        if base and key:
            yield {"name": name, "api_base": base, "api_key": key}
    # fallback to unnumbered
    base = os.getenv("CHUTES_API_BASE", "").strip()
    key = os.getenv("CHUTES_API_KEY", "").strip()
    if base and key:
        yield {"name": "chutes-1", "api_base": base, "api_key": key}


def auto_model_list_from_env(
    *,
    kind: str = "text",
    require_tools: bool = False,
    require_json: bool = False,
    max_bases: int = 5,
    max_candidates_per_base: int = 2,
) -> List[Dict]:
    """
    Build a dynamic model_list from numbered envs:
      CHUTES_API_BASE_{n}, CHUTES_API_KEY_{n}
    Selects closest models per base and applies availability+utilization ranking.

    If dynamic discovery yields nothing, falls back to static single-candidate per base
    using CHUTES_{KIND}_MODEL_{n} when present.
    """
    spec = DesiredSpec(kind=kind, require_tools=require_tools, require_json=require_json)
    bases = list(_iter_bases_from_env(max_bases=max_bases))

    # Try dynamic selection first
    dynamic = build_dynamic_model_list(spec=spec, bases=bases, max_candidates_per_base=max_candidates_per_base)
    if dynamic:
        return dynamic

    # Fallback to static per-base model if provided
    kind_env = {
        "text": "CHUTES_TEXT_MODEL",
        "vlm": "CHUTES_VLM_MODEL",
        "tools": "CHUTES_TOOLS_MODEL",
    }.get(kind, "CHUTES_TEXT_MODEL")

    static_list: List[Dict] = []
    for idx, b in enumerate(bases, start=1):
        model = os.getenv(f"{kind_env}_{idx}", os.getenv(kind_env, "")).strip()
        if not model:
            continue
        static_list.append(
            {
                "model_name": f"{b['name']}",
                "litellm_params": {
                    "custom_llm_provider": "openai_like",
                    "api_base": b["api_base"],
                    "api_key": None,
                    "extra_headers": {"x-api-key": b["api_key"]},
                    "model": model,
                },
            }
        )
    return rank_chutes_by_availability_and_utilization(static_list)


__all__ += [
    "auto_model_list_from_env",
]


def find_best_chutes_model(
    *,
    kind: str = "text",
    require_tools: bool = False,
    require_json: bool = False,
    bases: Optional[Iterable[Dict[str, str]]] = None,
    util_threshold: float = float(os.getenv("SCILLM_UTIL_HI", "0.85")),
) -> Optional[Dict]:
    """
    Return a single best candidate entry (Router-compatible dict) whose utilization
    is below `util_threshold` when possible; otherwise return the lowest-utilization
    eligible candidate. Uses dynamic discovery across bases (env by default).
    """
    if bases is None:
        bases = list(_iter_bases_from_env(max_bases=5))
    spec = DesiredSpec(kind=kind, require_tools=require_tools, require_json=require_json)
    lst = build_dynamic_model_list(spec=spec, bases=bases, max_candidates_per_base=2)
    if not lst:
        return None

    # Decorate with utilization; prefer under threshold
    best_entry = None
    best_util = 2.0  # >1 sentinel

    for entry in lst:
        p = entry.get("litellm_params", {}) or {}
        base = (p.get("api_base") or "").strip()
        headers = (p.get("extra_headers") or {})
        try:
            util = _get_utilization(base, headers)  # type: ignore[name-defined]
        except Exception:
            util = None

        # Prefer under threshold
        if util is not None and util < util_threshold:
            return entry

        # Track lowest utilization seen (including None as worst)
        comp = util if util is not None else 1.1
        if comp < best_util:
            best_util = comp
            best_entry = entry

    return best_entry

__all__ += [
    "find_best_chutes_model",
]
