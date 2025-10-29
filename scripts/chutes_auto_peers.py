#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

import httpx


def _auth_headers(base: str, key: str) -> Tuple[Dict[str, str], str]:
    """Detect a working auth style for this base by probing /models.

    Tries Bearer first, then x-api-key. Returns (headers, style).
    """
    base = base.rstrip("/")
    with httpx.Client(timeout=8.0) as cx:
        # Bearer
        try:
            r = cx.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"})
            if r.status_code == 200:
                return {"Authorization": f"Bearer {key}"}, "bearer"
        except Exception:
            pass
        # x-api-key
        try:
            r = cx.get(f"{base}/models", headers={"x-api-key": key})
            if r.status_code == 200:
                return {"x-api-key": key}, "x-api-key"
        except Exception:
            pass
    raise SystemExit(f"/models probe failed on base: {base}")


_SIZE_RE = re.compile(r"([0-9]+)\s*(B|T)\b", re.IGNORECASE)


def _approx_params(model_id: str) -> float:
    """Approximate parameter count from model id, in billions.

    Recognizes patterns like 235B, 78B, 1.8T. Falls back to 0 if unknown.
    """
    m = _SIZE_RE.search(model_id)
    if not m:
        return 0.0
    n = float(m.group(1))
    unit = m.group(2).lower()
    if unit == "t":
        return n * 1000.0
    return n


def _kind_filter(kind: str, mid: str) -> bool:
    midl = mid.lower()
    if kind == "vlm":
        return any(tag in midl for tag in ("-vl", "vl-", "/vl", "vision"))
    return True


def _get_models(base: str, headers: Dict[str, str]) -> List[str]:
    with httpx.Client(timeout=10.0) as cx:
        r = cx.get(f"{base.rstrip('/')}/models", headers=headers)
        r.raise_for_status()
        js = r.json() or {}
        out: List[str] = []
        for m in js.get("data", []) or []:
            if isinstance(m, dict) and isinstance(m.get("id"), str):
                out.append(m["id"])  # type: ignore[index]
        return out


def _quick_probe_chat(
    base: str, headers: Dict[str, str], model_id: str, *, timeout: float = 18.0
) -> bool:
    body = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": "Return only {\"ok\":true} as JSON."},
            {"role": "user", "content": "ping"},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 16,
        "temperature": 0,
    }
    try:
        with httpx.Client(timeout=timeout) as cx:
            r = cx.post(f"{base.rstrip('/')}/chat/completions", headers=headers, json=body)
            if r.status_code != 200:
                return False
            js = r.json() or {}
            choices = js.get("choices") or []
            if not choices:
                return False
            msg = (choices[0] or {}).get("message", {})
            content = msg.get("content")
            return bool(content)
    except Exception:
        return False


def _pick_peers(kind: str, ids: List[str], primary: Optional[str], k: int = 3) -> List[str]:
    # Filter by kind, then rank by closeness to primary size (if present), else by size desc.
    cand = [m for m in ids if _kind_filter(kind, m)]
    if not cand:
        cand = ids[:]
    target = _approx_params(primary or "") if primary else None
    if target:
        cand.sort(key=lambda m: abs(_approx_params(m) - float(target)))
    else:
        cand.sort(key=lambda m: _approx_params(m), reverse=True)
    # Deduplicate while preserving order
    seen = set()
    out: List[str] = []
    for m in cand:
        if m in seen:
            continue
        seen.add(m)
        out.append(m)
        if len(out) >= k:
            break
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Pick peer alternates for Chutes and print a .env snippet.")
    ap.add_argument("--kind", choices=["text", "vlm"], default="text")
    ap.add_argument("--primary", default=os.getenv("CHUTES_TEXT_MODEL") or os.getenv("CHUTES_VLM_MODEL"))
    ap.add_argument("--base", default=os.getenv("CHUTES_API_BASE", ""))
    ap.add_argument("--key", default=os.getenv("CHUTES_API_KEY", ""))
    ap.add_argument("--max", type=int, default=3, help="how many peers to output (default 3)")
    ap.add_argument("--verify", action="store_true", help="POST a minimal json chat to confirm routable")
    args = ap.parse_args()

    if not (args.base and args.key):
        print("Set CHUTES_API_BASE and CHUTES_API_KEY or pass --base/--key.", file=sys.stderr)
        sys.exit(2)

    headers, style = _auth_headers(args.base, args.key)
    ids = _get_models(args.base, headers)
    if not ids:
        print("No models returned by /models.", file=sys.stderr)
        sys.exit(3)

    peers = _pick_peers(args.kind, ids, args.primary, k=args.max)

    if args.verify:
        verified: List[str] = []
        for mid in peers:
            if _quick_probe_chat(args.base, headers, mid):
                verified.append(mid)
        if verified:
            peers = verified

    # Emit .env block
    prefix = "CHUTES_TEXT_MODEL" if args.kind == "text" else "CHUTES_VLM_MODEL"
    print("# --- Suggested peers (", args.kind, ") ---", sep="")
    if args.primary:
        print(f"{prefix}={args.primary}")
    for i, mid in enumerate(peers[: args.max], start=1):
        alt = f"{prefix}_ALT{i}"
        if args.primary and mid == args.primary:
            continue
        print(f"{alt}={mid}")

    # Emit Router example (single-base)
    group = "chutes/text" if args.kind == "text" else "chutes/vlm"
    print("\n# Router example (paste into a Python cell):")
    print("from scillm import Router, os")
    print("# Use env, do not paste secrets:")
    print("AUTH = {\"Authorization\": f\"Bearer {os.environ['CHUTES_API_KEY']}\"} if '%s' == 'bearer' else {\"x-api-key\": os.environ['CHUTES_API_KEY']}" % (style,))
    print("router = Router(model_list=[")
    for mid in ([args.primary] if args.primary else []) + [m for m in peers if m != args.primary]:
        if not mid:
            continue
        print("  {\"model_name\": \"%s\", \"litellm_params\": {\"custom_llm_provider\": \"openai_like\", \"api_base\": \"%s\", \"api_key\": None, \"extra_headers\": AUTH, \"model\": \"%s\"}}," % (group, args.base, mid))
    print("])")
    print("# out = router.completion(model=\"%s\", messages=[{\"role\":\"user\",\"content\":'Return only {\\\"ok\\\":true} as JSON.'}], response_format={\"type\":\"json_object\"})" % group)


if __name__ == "__main__":
    main()
