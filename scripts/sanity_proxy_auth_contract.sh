#!/usr/bin/env bash
# Live auth-contract sanity for issue #114.
#
# This script avoids provider/model calls. It verifies that the configured key
# authenticates against the protected OAuth-health surface and that 401 advice
# does not point callers at a stale literal key.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

dotenv_value() {
  ENV_FILE="$REPO_ROOT/.env" python3 - "$@" <<'PY'
import os
import sys
from pathlib import Path

names = sys.argv[1].split(",")
default = sys.argv[2]
env_file = Path(os.environ["ENV_FILE"])
values = {}
if env_file.is_file():
    for raw_line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")

for name in names:
    value = os.environ.get(name) or values.get(name)
    if value:
        print(value)
        raise SystemExit(0)

print(default)
PY
}

PROXY_URL="$(dotenv_value SCILLM_API_BASE "http://localhost:4001")"
PROXY_KEY="$(dotenv_value SCILLM_MASTER_KEY,LITELLM_MASTER_KEY,SCILLM_PROXY_KEY "sk-dev-proxy-123")"
CALLER_SKILL="${SCILLM_CALLER_SKILL:-scillm-auth-contract}"

if [[ -z "$PROXY_KEY" ]]; then
  echo "FAIL proxy key resolved empty" >&2
  exit 1
fi

positive_status="$(
  curl -sS -o /tmp/scillm-auth-contract-positive.json -w '%{http_code}' \
    "$PROXY_URL/v1/scillm/auth" \
    -H "Authorization: Bearer $PROXY_KEY" \
    -H "X-Caller-Skill: $CALLER_SKILL"
)"

if [[ "$positive_status" != "200" ]]; then
  echo "FAIL configured proxy key did not authenticate: HTTP $positive_status" >&2
  python3 -m json.tool /tmp/scillm-auth-contract-positive.json >&2 || true
  exit 1
fi

negative_status="$(
  curl -sS -o /tmp/scillm-auth-contract-negative.json -w '%{http_code}' \
    "$PROXY_URL/v1/scillm/auth" \
    -H "Authorization: Bearer sk-scillm-invalid-ticket-114" \
    -H "X-Caller-Skill: $CALLER_SKILL"
)"

if [[ "$negative_status" != "401" ]]; then
  echo "FAIL invalid proxy key should return 401, got HTTP $negative_status" >&2
  python3 -m json.tool /tmp/scillm-auth-contract-negative.json >&2 || true
  exit 1
fi

python3 - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("/tmp/scillm-auth-contract-negative.json").read_text())
advice = str(payload.get("error", {}).get("advice", ""))
if "sk-dev-proxy-123" in advice:
    raise SystemExit("FAIL auth advice still hardcodes sk-dev-proxy-123")
if "SCILLM_MASTER_KEY" not in advice or "LITELLM_MASTER_KEY" not in advice:
    raise SystemExit("FAIL auth advice does not name configured key environment variables")
print("auth advice names configured key variables")
PY

echo "PASS scillm auth contract"
