#!/usr/bin/env bash
set -euo pipefail

BASE="${SCILLM_BASE_URL:-http://127.0.0.1:4001}"
KEY="${SCILLM_PROXY_KEY:-sk-dev-proxy-123}"
PYTHON_BIN="${PYTHON_BIN:-python}"
AUTH=(-H "Authorization: Bearer ${KEY}" -H "Content-Type: application/json" -H "X-Caller-Skill: scillm-exec-sanity")

run_id() {
  printf "%s-%s" "$1" "$(date +%s)-$RANDOM"
}

printf '== scillm exec sanity: health ==\n'
curl -sf "${BASE}/health/liveliness" >/dev/null

EXEC_RUN_ID="$(run_id sanity-exec-local)"
printf '== /v1/scillm/exec local_command (%s) ==\n' "$EXEC_RUN_ID"
curl -sf -X POST "${BASE}/v1/scillm/exec" "${AUTH[@]}" -d @- | jq . <<JSON
{
  "run_id": "${EXEC_RUN_ID}",
  "id": "local_ok",
  "type": "local_command",
  "node_goal": "Return deterministic JSON for the exec endpoint sanity check.",
  "command": ["${PYTHON_BIN}", "-c", "import json; print(json.dumps({'status':'ok','endpoint':'exec'}))"]
}
JSON

printf '== status endpoint ==\n'
curl -sf "${BASE}/v1/scillm/exec/${EXEC_RUN_ID}/status" "${AUTH[@]}" | jq .

printf '== events endpoint ==\n'
curl -sf "${BASE}/v1/scillm/exec/${EXEC_RUN_ID}/events?tail=20" "${AUTH[@]}" | jq .

BATCH_ID="$(run_id sanity-exec-batch)"
printf '== /v1/scillm/exec/batch (%s) ==\n' "$BATCH_ID"
curl -sf -X POST "${BASE}/v1/scillm/exec/batch" "${AUTH[@]}" -d @- | jq . <<JSON
{
  "batch_id": "${BATCH_ID}",
  "graph_goal": "Run two independent deterministic exec workers.",
  "max_concurrency": 2,
  "items": [
    {
      "id": "worker_a",
      "type": "local_command",
      "node_goal": "Return worker A JSON.",
      "command": ["${PYTHON_BIN}", "-c", "import json; print(json.dumps({'worker':'a'}))"]
    },
    {
      "id": "worker_b",
      "type": "local_command",
      "node_goal": "Return worker B JSON.",
      "command": ["${PYTHON_BIN}", "-c", "import json; print(json.dumps({'worker':'b'}))"]
    }
  ]
}
JSON

GRAPH_ID="$(run_id sanity-exec-graph)"
TMP_REPO="$(mktemp -d)"
printf '== /v1/scillm/exec/graph (%s) ==\n' "$GRAPH_ID"
curl -sf -X POST "${BASE}/v1/scillm/exec/graph" "${AUTH[@]}" -d @- | jq . <<JSON
{
  "graph_id": "${GRAPH_ID}",
  "graph_goal": "Run a small dependency DAG with deterministic local commands.",
  "cwd": "${TMP_REPO}",
  "max_concurrency": 2,
  "nodes": [
    {
      "id": "write_marker",
      "type": "local_command",
      "node_goal": "Write a marker file.",
      "command": ["${PYTHON_BIN}", "-c", "from pathlib import Path; Path('marker.txt').write_text('ok')"]
    },
    {
      "id": "read_marker",
      "type": "local_command",
      "node_goal": "Read the marker file after the dependency completes.",
      "depends_on": ["write_marker"],
      "command": ["${PYTHON_BIN}", "-c", "import json; from pathlib import Path; print(json.dumps({'marker': Path('marker.txt').read_text()}))"]
    }
  ]
}
JSON
rm -rf "$TMP_REPO"

CANCEL_ID="$(run_id sanity-exec-cancel)"
printf '== /v1/scillm/exec cancel (%s) ==\n' "$CANCEL_ID"
cat > /tmp/scillm-exec-cancel-payload.json <<JSON
{
  "run_id": "${CANCEL_ID}",
  "id": "long_sleep",
  "type": "local_command",
  "node_goal": "Sleep long enough to be cancelled.",
  "timeout_s": 60,
  "command": ["${PYTHON_BIN}", "-c", "import time; time.sleep(30)"]
}
JSON
curl -s -X POST "${BASE}/v1/scillm/exec" "${AUTH[@]}" -d @/tmp/scillm-exec-cancel-payload.json > /tmp/scillm-exec-cancel-response.json &
LONG_PID=$!
sleep 2
curl -sf -X POST "${BASE}/v1/scillm/exec/${CANCEL_ID}/cancel" "${AUTH[@]}" | jq .
wait "$LONG_PID" || true
cat /tmp/scillm-exec-cancel-response.json | jq .

printf '== optional codex_exec smoke ==\n'
if command -v codex >/dev/null 2>&1; then
  CODEX_ID="$(run_id sanity-codex-exec)"
  curl -sf -X POST "${BASE}/v1/scillm/exec" "${AUTH[@]}" -d @- | jq . <<JSON
{
  "run_id": "${CODEX_ID}",
  "id": "codex_smoke",
  "type": "codex_exec",
  "model": "gpt-5.5",
  "sandbox": "read-only",
  "node_goal": "Return a tiny JSON smoke result.",
  "prompt": "Return JSON only: {\"status\":\"ok\",\"runner\":\"codex_exec\"}",
  "output_schema": {
    "type": "object",
    "required": ["status", "runner"]
  }
}
JSON
else
  echo "codex not found; skipping optional codex_exec smoke"
fi

printf '== optional claude_print smoke ==\n'
if command -v claude >/dev/null 2>&1; then
  CLAUDE_ID="$(run_id sanity-claude-print)"
  curl -sf -X POST "${BASE}/v1/scillm/exec" "${AUTH[@]}" -d @- | jq . <<JSON
{
  "run_id": "${CLAUDE_ID}",
  "id": "claude_smoke",
  "type": "claude_print",
  "sandbox": "read-only",
  "node_goal": "Return a tiny JSON smoke result.",
  "prompt": "Return JSON only: {\"status\":\"ok\",\"runner\":\"claude_print\"}",
  "output_schema": {
    "type": "object",
    "required": ["status", "runner"]
  }
}
JSON
else
  echo "claude not found; skipping optional claude_print smoke"
fi

echo "PASS scillm exec endpoint sanity"
