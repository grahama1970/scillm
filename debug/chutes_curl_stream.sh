#!/usr/bin/env bash
set -euo pipefail

BASE="${CHUTES_API_BASE:-https://llm.chutes.ai/v1}"
TOKEN="${CHUTES_API_KEY:-}" 
MODEL="${CHUTES_TEXT_MODEL:-Qwen/Qwen3-235B-A22B-Instruct-2507}"

if [[ -z "$TOKEN" ]]; then
  echo "ENV_MISSING CHUTES_API_KEY" >&2
  exit 12
fi

TMP_DIR="$(mktemp -d)"
HDR_OUT="$TMP_DIR/hdr.txt"
BODY_OUT="$TMP_DIR/body.txt"

curl -sS -N \
  -D "$HDR_OUT" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -X POST "$BASE/chat/completions" \
  --data @<(cat <<JSON
{ 
  "model": "$MODEL",
  "messages": [
    {"role":"user","content":"Tell me a 250 word story."}
  ],
  "stream": true,
  "max_tokens": 1024,
  "temperature": 0.7
}
JSON
) | tee "$BODY_OUT" >/dev/null || true

STATUS_LINE=$(head -n1 "$HDR_OUT" 2>/dev/null || true)
RETRY_AFTER=$(grep -i '^retry-after:' "$HDR_OUT" | awk '{print $2}' | tr -d '\r' | head -n1 || true)
echo "STATUS: ${STATUS_LINE:-unknown}"
if [[ -n "$RETRY_AFTER" ]]; then echo "RETRY_AFTER: ${RETRY_AFTER}s"; fi
echo "MODEL: $MODEL" 
echo "SLICE:" 
head -n 5 "$BODY_OUT" || true
echo "ARTIFACTS: $HDR_OUT $BODY_OUT" 
