#!/usr/bin/env bash
set -euo pipefail

echo "=== scillm entrypoint: starting proxy on :4001 ==="
exec uvicorn scillm.proxy.app:app \
    --host 0.0.0.0 \
    --port 4001 \
    --log-level info
