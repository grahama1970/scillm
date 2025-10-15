from __future__ import annotations
import argparse, json, os, sys
from litellm.extras.preflight import preflight_models, catalog_for
from litellm.extras.model_alias import resolve_model_id

def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve doc-style model name to canonical Chutes ID (uses cached catalog)")
    ap.add_argument("--name", required=True, help="Requested vendor-first model id (e.g., mistral-ai/Mistral-Small-3.2-24B)")
    ap.add_argument("--base", default=os.getenv("CHUTES_API_BASE", ""), help="OpenAI-compatible base (defaults CHUTES_API_BASE)")
    ap.add_argument("--key", default=os.getenv("CHUTES_API_KEY", ""), help="API key (defaults CHUTES_API_KEY)")
    ap.add_argument("--no-preflight", action="store_true", help="Do not fetch /v1/models; use existing cache only")
    args = ap.parse_args()
    if not args.base:
        print("error: missing --base or CHUTES_API_BASE", file=sys.stderr)
        return 2
    # one-time warm unless disabled
    if not args.no_preflight:
        try:
            preflight_models(api_base=args.base, api_key=args.key)
        except Exception as e:
            print(f"warn: preflight failed: {e}", file=sys.stderr)
    cats = catalog_for(args.base)
    resolved = resolve_model_id(api_base=args.base, requested=args.name) or None
    out = {
        "base": args.base.rstrip('/'),
        "requested": args.name,
        "resolved": resolved,
        "in_catalog": bool(resolved in cats) if resolved else False,
        "catalog_size": len(cats),
    }
    print(json.dumps(out, indent=2))
    return 0 if resolved else 1

if __name__ == "__main__":
    raise SystemExit(main())
