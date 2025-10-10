#!/usr/bin/env bash
set -euo pipefail

BASE="${1:-http://127.0.0.1:8788}"

echo "[info] GET $BASE/ready"
curl -sSf "$BASE/ready" | jq . || { echo "[fail] /ready"; exit 2; }

echo "[info] POST $BASE/agent/run (tool_backend=local)"
curl -sS -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"hi"}],"model":"dummy","tool_backend":"local"}' \
  "$BASE/agent/run" | jq . || { echo "[fail] /agent/run"; exit 2; }

echo "[ok] mini-agent HTTP checks passed at $BASE"
