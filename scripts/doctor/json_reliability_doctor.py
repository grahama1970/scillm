from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

from litellm.extras.model_alias import resolve_models
from scillm import completion


def load_models_arg(models: Optional[str], models_file: Optional[str]) -> List[str]:
    if models_file:
        p = Path(models_file)
        if not p.exists():
            print(f"error: models file not found: {models_file}", file=sys.stderr)
            sys.exit(2)
        out: List[str] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
        if not out:
            print("error: no models in file", file=sys.stderr)
            sys.exit(2)
        return out
    if models:
        return [m.strip() for m in models.split(",") if m.strip()]
    print("error: provide --models or --models-file", file=sys.stderr)
    sys.exit(2)


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe models for strict JSON compliance (no sanitizer).")
    ap.add_argument("--base", default=os.getenv("CHUTES_API_BASE") or os.getenv("OPENAI_BASE_URL"), help="OpenAI-compatible base (defaults CHUTES_API_BASE or OPENAI_BASE_URL)")
    ap.add_argument("--key", default=os.getenv("CHUTES_API_KEY") or os.getenv("OPENAI_API_KEY"), help="API key (defaults CHUTES_API_KEY or OPENAI_API_KEY)")
    ap.add_argument("--models", help="Comma-separated doc-style model ids")
    ap.add_argument("--models-file", help="Path to newline-delimited model ids")
    ap.add_argument("--cutoff", type=float, default=float(os.getenv("SCILLM_ALIAS_CUTOFF", "0.6") or 0.6))
    ap.add_argument("--timeout", type=float, default=float(os.getenv("SCILLM_JSON_PROBE_TIMEOUT_S", "12") or 12))
    ap.add_argument("--out", default=str(Path.home() / ".cache" / "scillm" / "json_capability.json"))
    ap.add_argument("--print-table", action="store_true", help="Print a compact table summary")
    args = ap.parse_args()

    if not args.base or not args.key:
        print("error: missing --base/--key or CHUTES/OPENAI env", file=sys.stderr)
        return 2
    models = load_models_arg(args.models, args.models_file)

    # Resolve to canonical IDs using once-per-session catalog
    res = resolve_models(models, api_base=args.base, api_key=args.key, cutoff=args.cutoff, prefer_live=True, allow_stale=True, timeout=args.timeout)

    strict_ok: List[str] = []
    strict_fail: List[dict] = []

    for item in res.resolved:
        if not item.exists:
            strict_fail.append({"requested": item.input, "canonical": item.canonical, "error": "not_in_catalog"})
            continue
        model_id = item.canonical
        try:
            r = completion(
                model=model_id,
                messages=[
                    {"role":"system","content":"Return a single valid JSON object with only {\"ok\": true}."},
                    {"role":"user","content":"{}"}
                ],
                response_format={"type":"json_object"},
                temperature=0, top_p=0,
                api_base=args.base, api_key=args.key,
                custom_llm_provider="openai",
                auto_json_sanitize=False,
                timeout=args.timeout,
            )
            raw = r.choices[0].message.content
            json.loads(raw)  # must parse without any sanitizer
            strict_ok.append(model_id)
        except Exception as e:
            strict_fail.append({"requested": item.input, "canonical": model_id, "error": str(e)[:240]})

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"base": args.base.rstrip("/"), "ts": time.time(), "strict_ok": sorted(set(strict_ok)), "strict_fail": strict_fail}
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if args.print-table:
        print("\nStrict JSON capability (no sanitizer):")
        for ok in sorted(set(strict_ok)):
            print(f"  [OK]   {ok}")
        for f in strict_fail:
            print(f"  [FAIL] {f.get('canonical') or f.get('requested')} :: {f.get('error')}")
    else:
        print(f"Wrote capability snapshot to {out_path} (strict_ok={len(strict_ok)}, strict_fail={len(strict_fail)})")

    return 0 if strict_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
