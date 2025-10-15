from __future__ import annotations

import json
import os
import re
import time
from typing import Iterable, Optional, Sequence, Tuple
from urllib import request as _urlreq

from .preflight import catalog_for, _normalize_base
from rapidfuzz import process, fuzz

_VENDOR_MAP = {
    # common vendor synonyms to canonical vendor prefixes in most OpenAI-compatible lists
    "mistral": "mistral-ai",
    "mistralai": "mistral-ai",
    "qwen": "qwen",
    "qwen3": "qwen",
    "moonshotai": "moonshotai",
    "deepseek": "deepseek-ai",
    "deepseek-ai": "deepseek-ai",
    "inclusionai": "inclusionai",
}

_DEF_CUTOFF = 0.55  # lenient but avoids wildly wrong matches


def _vendor_part(model_id: str) -> str:
    if "/" in model_id:
        return model_id.split("/", 1)[0]
    return model_id


def _model_part(model_id: str) -> str:
    if "/" in model_id:
        return model_id.split("/", 1)[1]
    return model_id


def _canonical_vendor(v: str) -> str:
    v = re.sub(r"[^a-z0-9]+", "", v.lower())
    return _VENDOR_MAP.get(v, v)


def _norm_text(t: str) -> str:
    t = t.lower()
    # unify common descriptors
    t = t.replace("instruct", "")
    t = t.replace("chat", "")
    # strip separators
    t = re.sub(r"[^a-z0-9]+", "", t)
    return t


def _score_simple(a: str, b: str) -> float:
    # simple token overlap / Jaccard-like on bigrams as a fast, dependency-free heuristic
    def grams(s: str) -> set[str]:
        return {s[i : i + 2] for i in range(max(0, len(s) - 1))} or {s}

    A, B = grams(a), grams(b)
    inter = len(A & B)
    union = len(A | B)
    return inter / union if union else 0.0


def _regex_prefilter(requested: str, candidates: Sequence[str]) -> Sequence[str]:
    # build token regex from normalized requested model part
    tokens = re.findall(r"[a-zA-Z0-9]+", _model_part(requested))
    tokens = [t for t in tokens if t]
    if not tokens:
        return candidates
    # require at least one token to appear in candidate model part
    pat = re.compile("|".join(re.escape(t.lower()) for t in tokens))
    out = []
    for c in candidates:
        if pat.search(_model_part(c).lower()):
            out.append(c)
    return out or candidates


def _score_rapidfuzz(query: str, choices: Sequence[str]) -> Sequence[Tuple[str, float]]:
    """Score choices using rapidfuzz (required dependency)."""
    return process.extract(query, choices, scorer=fuzz.WRatio, score_cutoff=0)


def resolve_model_id(
    *,
    api_base: str,
    requested: str,
    prefer_same_vendor: bool = True,
    cutoff: float = _DEF_CUTOFF,
) -> Optional[str]:
    """Resolve a requested vendor-first ID to a canonical ID from the cached catalog.

    Returns the canonical ID if a good match is found; otherwise None.
    Requires that preflight_models() has already warmed the catalog for api_base.
    """
    base = _normalize_base(api_base)
    requested = requested.strip()

    cats = list(catalog_for(base))
    if not cats:
        return None
    if requested in cats:
        return requested

    r_vendor = _canonical_vendor(_vendor_part(requested))
    # regex prefilter to reduce choice set size
    cand = list(_regex_prefilter(requested, cats))

    # build normalized comparison strings
    norm_choices = []
    for c in cand:
        c_vendor = _canonical_vendor(_vendor_part(c))
        c_model_norm = _norm_text(_model_part(c))
        # vendor boost encoded by duplicating vendor in the normalized key
        key = ("v:" + c_vendor + ":" + c_model_norm)
        norm_choices.append((c, key, c_vendor))

    query = "v:" + r_vendor + ":" + _norm_text(_model_part(requested))
    scored = _score_rapidfuzz(query, [k for _, k, _ in norm_choices])
    # map back to candidate ids and apply small vendor tie-break
    best_id = None
    best_score = -1.0
    for item in scored:  # type: ignore
        # locate original
        for cid, k, c_vendor in norm_choices:
            if k == item[0]:
                s = float(item[1]) / 100.0 if isinstance(item[1], (int,float)) and item[1] > 1 else float(item[1])
                if prefer_same_vendor and c_vendor == r_vendor:
                    s += 0.05
                if s > best_score:
                    best_id, best_score = cid, s
                break
    if best_id and best_score >= cutoff:
        return best_id
    # Optional LLM assist
    if os.getenv("SCILLM_MODEL_ALIAS_LLM", "0").lower() in {"1", "true", "yes"}:
        try:
            rid = _resolve_with_llm(base, requested, [c for c, _, _ in norm_choices])
            if rid in cats:
                return rid
        except Exception:
            pass
    return None


def _resolve_with_llm(api_base: str, requested: str, candidates: Sequence[str]) -> Optional[str]:
    base = _normalize_base(api_base)
    llm_base = os.getenv("CODEX_AGENT_API_BASE") or os.getenv("OPENAI_BASE_URL")
    if not llm_base:
        return None
    key = os.getenv("CODEX_AGENT_API_KEY") or os.getenv("OPENAI_API_KEY", "none")
    url = llm_base.rstrip("/") + "/v1/chat/completions"
    prompt = (
        "Given a requested model id and a list of available canonical model ids, "
        "choose the single best match by id (exactly one of the provided ids). "
        "Respond with just the id.\nRequested: "
        + requested
        + "\nAvailable:\n- "
        + "\n- ".join(candidates)
        + "\nAnswer:"
    )
    body = {
        "model": os.getenv("SCILLM_ALIAS_LLM_MODEL", "gpt-5"),
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(body).encode("utf-8")
    headers = {"content-type": "application/json", "authorization": f"Bearer {key}"}
    try:
        with _urlreq.urlopen(_urlreq.Request(url=url, data=data, headers=headers, method="POST"), timeout=float(os.getenv("SCILLM_ALIAS_LLM_TIMEOUT_S", "6") or "6")) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        content = res.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        # Try to extract an id by simple heuristics
        # If JSON, try to parse {"id":"..."}
        try:
            j = json.loads(content)
            if isinstance(j, dict) and j.get("id"):
                return str(j["id"]).strip()
        except Exception:
            pass
        # Otherwise, use the raw content line if it exactly matches a candidate
        for c in candidates:
            if content == c:
                return c
        return None
    except Exception:
        return None
