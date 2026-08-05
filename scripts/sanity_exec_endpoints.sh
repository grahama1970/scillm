#!/usr/bin/env bash
# Fail-closed sanity for scillm exec: deterministic HTTP contract + live LLM runners.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
curl -sf -X POST "${BASE}/v1/scillm/exec" "${AUTH[@]}" -d @- <<JSON | jq .
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
curl -sf -X POST "${BASE}/v1/scillm/exec/batch" "${AUTH[@]}" -d @- <<JSON | jq .
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
curl -sf -X POST "${BASE}/v1/scillm/exec/graph" "${AUTH[@]}" -d @- <<JSON | jq .
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
CANCEL_STATUS="$(jq -r '.status' /tmp/scillm-exec-cancel-response.json)"
if [[ "${CANCEL_STATUS}" != "failed" && "${CANCEL_STATUS}" != "cancelled" ]]; then
  echo "FAIL cancel endpoint: expected failed/cancelled, got ${CANCEL_STATUS}" >&2
  jq . /tmp/scillm-exec-cancel-response.json >&2
  exit 1
fi
cat /tmp/scillm-exec-cancel-response.json | jq .

printf '== live runner + chat + agents (fail-closed bash, no pytest) ==\n'
export SCILLM_EXEC_SMOKE_CWD="${REPO_ROOT}"
"${REPO_ROOT}/scripts/sanity_live_runners.sh"

echo "PASS scillm exec+agents live sanity (deterministic HTTP + real runners + standing agent)"

if [[ "${SCILLM_CURSOR_STREAM_PROBE:-}" == "1" ]]; then
  FAKE_AGENT="${REPO_ROOT}/scripts/test_fixtures/fake_cursor_stream_agent.py"
  printf '== cursor_exec stream probe (fake agent; restart proxy with SCILLM_CURSOR_AGENT_BINARY=%s) ==\n' "$FAKE_AGENT"
  CURSOR_RUN_ID="$(run_id sanity-cursor-stream)"
  WORKSPACE="${REPO_ROOT}/.scillm/exec-stream-probe"
  mkdir -p "$WORKSPACE"
  BODY="$(jq -n --arg run_id "$CURSOR_RUN_ID" --arg cwd "$WORKSPACE" '{
    run_id: $run_id,
    id: "cursor_stream",
    type: "cursor_exec",
    model: "cursor-auto",
    node_goal: "Sanity: oversized stream-json line completes",
    graph_goal: "cursor stream probe",
    cwd: $cwd,
    sandbox: "read-only",
    timeout_s: 30,
    idle_timeout_s: 10,
    prompt: "Return success."
  }')"
  RESP="$(curl -sf -X POST "${BASE}/v1/scillm/exec" "${AUTH[@]}" -d "$BODY")"
  echo "$RESP" | jq '{status, ok: .result.ok, failure_type: .result.failure_type, stream_completed: .result.stream_completed}'
  OK="$(echo "$RESP" | jq -r '.result.ok')"
  FC="$(echo "$RESP" | jq -r '.result.failure_type // empty')"
  SC="$(echo "$RESP" | jq -r '.result.stream_completed')"
  if [[ "$OK" != "true" || "$SC" != "true" ]]; then
    echo "cursor stream probe failed: ok=$OK stream_completed=$SC failure_type=$FC" >&2
    exit 1
  fi
fi
