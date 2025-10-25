#!/usr/bin/env python3
"""
Chutes warmup probe — best‑effort readiness + cord sampling.

Usage:
  PYTHONPATH=src:. CHUTES_API_BASE=https://<slug>.chutes.ai/v1 CHUTES_API_KEY=... \
  python scripts/chutes_warmup_probe.py --wait-seconds 180

Behavior:
  - POST /warmup/kick (ignored if 404)
  - Poll /warmup/status (if present) alongside /v1/models
  - Succeeds when GET /v1/models returns 200; prints JSON summary
"""
import argparse, json, os, sys, time
from typing import Any, Dict
import httpx

def _hdrs(key: str) -> Dict[str,str]:
    return {"Authorization": f"Bearer {key}", "Content-Type":"application/json"}

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--wait-seconds", type=int, default=180)
    args = p.parse_args()
    base = os.environ["CHUTES_API_BASE"].rstrip("/")
    key = os.environ["CHUTES_API_KEY"]
    t0 = time.time()
    warm = {"kicked": False, "status": None}
    ok_models = False
    with httpx.Client(timeout=10.0) as c:
        # kick (best‑effort) — only if enabled
        try:
            if str(os.getenv("SCILLM_ENABLE_WARMUP_CORDS", "")).strip().lower() in {"1","true","yes","on"}:
                r = c.post(base.replace("/v1","") + "/warmup/kick", headers=_hdrs(key))
                warm["kicked"] = r.status_code in (200, 202, 204)
        except Exception:
            pass
        # poll
        deadline = t0 + max(10, int(args.wait_seconds))
        last = {}
        while time.time() < deadline:
            try:
                m = c.get(base + "/models", headers=_hdrs(key))
                ok_models = (m.status_code == 200)
            except Exception:
                ok_models = False
            try:
                if str(os.getenv("SCILLM_ENABLE_WARMUP_CORDS", "")).strip().lower() in {"1","true","yes","on"}:
                    s = c.get(base.replace("/v1","") + "/warmup/status", headers=_hdrs(key))
                    if s.status_code == 200:
                        warm["status"] = s.json()
            except Exception:
                pass
            if ok_models: break
            time.sleep(2.0)
    elapsed = time.time() - t0
    out = {"ok": ok_models, "elapsed_sec": round(elapsed,2), "warmup": warm}
    print(json.dumps(out))
    return 0 if ok_models else 2

if __name__ == "__main__":
    sys.exit(main())
