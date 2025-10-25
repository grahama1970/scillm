#!/usr/bin/env bash
set -euo pipefail

BASE=${CERTAINLY_BRIDGE_BASE:-${LEAN4_BRIDGE_BASE:-http://127.0.0.1:8791}}
echo "[doctor] CERTAINLY_BRIDGE_BASE=${BASE}"

echo "[doctor] GET ${BASE}/healthz";
code=$(curl -sS -m 8 -w '%{http_code}' -o /tmp/cb_health.json "${BASE}/healthz" || true)
echo "HTTP ${code} $(cat /tmp/cb_health.json 2>/dev/null | tr -d '\n' | cut -c1-160)"
test "${code}" = "200" || { echo "[doctor] FAIL healthz"; exit 2; }

json='{"messages":[{"role":"system","content":"Prove requirements"}],"lean4_requirements":[{"requirement_text":"theorem t1 : True := True.intro"}],"options":{"max_seconds":30}}'
echo "[doctor] POST ${BASE}/bridge/complete"
code=$(curl -sS -m 30 -w '%{http_code}' -o /tmp/cb_run.json -H 'Content-Type: application/json' --data "$json" "${BASE}/bridge/complete" || true)
echo "HTTP ${code} $(cat /tmp/cb_run.json 2>/dev/null | tr -d '\n' | cut -c1-160)"
test "${code}" = "200" || { echo "[doctor] FAIL bridge/complete"; exit 3; }

succ=$(python - <<'PY'
import json,sys
try:
  d=json.load(open('/tmp/cb_run.json'))
  s=(d.get('summary') or {})
  print(1 if s.get('proved',0)>=1 else 0)
except Exception:
  print(0)
PY
)
if [ "$succ" = "1" ]; then
  echo "[doctor] PASS proved>=1"
  exit 0
else
  echo "[doctor] FAIL proved==0"
  exit 4
fi

