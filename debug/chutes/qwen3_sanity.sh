#!/usr/bin/env bash
set -euo pipefail

# Sanity for Qwen3 via Chutes Chat Completions
# - Uses Authorization: Bearer $CHUTES_API_KEY
# - Runs a JSON echo (non-stream) and a short streaming story

BASE="${CHUTES_API_BASE:-https://llm.chutes.ai/v1}"
TOKEN_RAW="${CHUTES_API_KEY:-}"
MODEL="${CHUTES_TEXT_MODEL:-Qwen/Qwen3-235B-A22B-Instruct-2507}"

if [[ -z "$TOKEN_RAW" ]]; then
  echo "ENV_MISSING CHUTES_API_KEY" >&2
  exit 12
fi

# Strip accidental surrounding quotes
TOKEN="$TOKEN_RAW"
if [[ "${#TOKEN}" -gt 1 && ( ( "$TOKEN" == '"'*'"' ) || ( "$TOKEN" == "'"*"'" ) ) ]]; then
  TOKEN="${TOKEN:1:${#TOKEN}-2}"
fi

tmpdir="$(mktemp -d)"
hdr_json="$tmpdir/hdr_json.txt"
body_json="$tmpdir/body_json.txt"
hdr_stream="$tmpdir/hdr_stream.txt"
body_stream="$tmpdir/body_stream.txt"

echo "== JSON echo test =="
code_json=0
curl -sS -D "$hdr_json" -o "$body_json" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -X POST "$BASE/chat/completions" \
  --data @<(cat <<JSON
{
  "model": "$MODEL",
  "messages": [{"role":"user","content":"Return only {\\"ok\\":true} as JSON."}],
  "response_format": {"type":"json_object"},
  "max_tokens": 64,
  "temperature": 0
}
JSON
) || code_json=$?

status_json=$(head -n1 "$hdr_json" 2>/dev/null || true)
slice_json=$(head -n1 "$body_json" 2>/dev/null || true)
echo "STATUS(JSON): ${status_json:-unknown}"
echo "MODEL: $MODEL"
echo "CONTENT(JSON): ${slice_json}"

pass_json=1
if ! grep -q ' 200 ' <<<"$status_json"; then pass_json=0; fi
if ! grep -q '"ok"\s*:\s*true' "$body_json" >/dev/null 2>&1; then pass_json=0; fi

echo "== Streaming story test =="
code_stream=0
curl -sS -N -D "$hdr_stream" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -X POST "$BASE/chat/completions" \
  --data @<(cat <<JSON
{
  "model": "$MODEL",
  "messages": [{"role":"user","content":"Tell me a 120 word story."}],
  "stream": true,
  "max_tokens": 256,
  "temperature": 0.7
}
JSON
) | tee "$body_stream" >/dev/null || code_stream=$?

status_stream=$(head -n1 "$hdr_stream" 2>/dev/null || true)
echo "STATUS(STREAM): ${status_stream:-unknown}"
echo "SSE SLICE:"; head -n 5 "$body_stream" || true
echo "ARTIFACTS: $hdr_json $body_json | $hdr_stream $body_stream"

# Exit codes: 0=ok, 31=auth/model error, 32=capacity/other
if [[ $pass_json -eq 1 ]]; then
  exit 0
fi

# If capacity (429) on either path, surface as special exit
if grep -q ' 429 ' <<<"$status_json$status_stream"; then
  exit 32
fi

exit 31

