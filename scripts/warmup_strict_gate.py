#!/usr/bin/env python3
"""
Strict warm‑up gate wrapper.

Behavior:
- If STRICT_WARMUPS!=1 → exit 0 (no-op; keeps readiness optional by default).
- If STRICT_WARMUPS=1 → require provider creds exist; run provider warmup script.
- Exits 1 only when STRICT_WARMUPS=1 AND required provider creds are missing.

Note: This does not parse per-model warmup results; it gates only on presence of
provider credentials and a successful script invocation.
"""
from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    provider = None
    argv = list(sys.argv[1:])
    # simple arg parse: --provider NAME
    if "--provider" in argv:
        try:
            i = argv.index("--provider")
            provider = argv[i + 1]
            del argv[i : i + 2]
        except Exception:
            pass
    provider = (provider or os.getenv("WARMUP_PROVIDER") or "").strip().lower()
    strict = os.getenv("STRICT_WARMUPS", "0") == "1"
    if not strict:
        return 0

    if provider == "chutes":
        if not os.getenv("CHUTES_API_KEY"):
            print("warmup-strict: CHUTES_API_KEY missing and STRICT_WARMUPS=1")
            return 1
        cmd = [sys.executable, "scripts/chutes_warmup.py"] + argv
        return subprocess.call(cmd)
    if provider == "runpod":
        if not os.getenv("RUNPOD_API_KEY") or not os.getenv("RUNPOD_API_BASE"):
            print("warmup-strict: RUNPOD_API_KEY or RUNPOD_API_BASE missing and STRICT_WARMUPS=1")
            return 1
        cmd = [sys.executable, "scripts/provider_warmup.py", "--provider", "runpod"] + argv
        return subprocess.call(cmd)

    # Unknown provider → treat as pass in strict mode (or add more as needed)
    print(f"warmup-strict: unknown provider '{provider}', passing")
    return 0


if __name__ == "__main__":
    sys.exit(main())

