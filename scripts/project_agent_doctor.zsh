#!/usr/bin/env zsh
set -euo pipefail

echo "[doctor] SciLLM Project Agent Doctor"

BASE=${CODEX_AGENT_API_BASE:-}
KEY=${CODEX_AGENT_API_KEY:-}
if [[ -z "${BASE}" ]]; then
  echo "[doctor] ERROR: Set CODEX_AGENT_API_BASE (e.g., http://127.0.0.1:8788 or :8077)" >&2
  exit 2
fi

echo "[doctor] Base: ${BASE}"

echo "[doctor] Checking /healthz ..."
curl -sf "${BASE}/healthz" | jq . || { echo "[doctor] /healthz failed" >&2; exit 3; }

echo "[doctor] Listing /v1/models ..."
curl -sf "${BASE}/v1/models" | jq . || { echo "[doctor] /v1/models failed" >&2; exit 4; }

echo "[doctor] Making high‑reasoning /v1/chat/completions ..."
AUTH=()
[[ -n "${KEY}" ]] && AUTH=(-H "Authorization: Bearer ${KEY}")

payload='{"model":"gpt-5","reasoning":{"effort":"high"},"messages":[{"role":"user","content":"Say hello and stop."}]}'
curl -sS "${BASE}/v1/chat/completions" \
  -H 'Content-Type: application/json' ${AUTH[@]:-} \
  -d "${payload}" | jq -r '.choices[0].message.content' || { echo "[doctor] chat failed" >&2; exit 5; }

echo "[doctor] OK"

