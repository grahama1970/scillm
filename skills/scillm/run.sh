#!/bin/bash
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PROJECT_ROOT="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")"

dotenv_value() {
    ENV_FILE="$PROJECT_ROOT/.env" python3 - "$@" <<'PY'
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

# Ensure uv is available
if ! command -v uv &> /dev/null; then
    echo "Error: uv is not installed. Please install uv."
    exit 1
fi

# Clear parent VIRTUAL_ENV to avoid mismatch warning when uv resolves its own .venv
unset VIRTUAL_ENV

PROXY_URL="$(dotenv_value SCILLM_API_BASE "http://localhost:4001")"
PROXY_KEY="$(dotenv_value SCILLM_MASTER_KEY,LITELLM_MASTER_KEY,SCILLM_PROXY_KEY "sk-dev-proxy-123")"
DEFAULT_MODEL="${SCILLM_MODEL:-text}"
CALLER_SKILL="${SCILLM_CALLER_SKILL:-scillm}"

usage() {
    cat <<'EOF'
Usage: run.sh [--model MODEL] [--system TEXT] [PROMPT...]
       run.sh prove [args...]

Examples:
  run.sh "Explain quantum computing in one sentence"
  run.sh --model moonshot-text "Explain quantum computing in one sentence"
  run.sh --model text-kimi --system "Be concise." "Summarize this repo"
  run.sh prove 'Prove n+0=n'

Notes:
  - Default model is "text" unless SCILLM_MODEL is set.
  - Requests go through the local scillm proxy at localhost:4001.
  - The wrapper always sends X-Caller-Skill for traceability.
EOF
}

# Dispatch to sub-scripts
case "$1" in
    prove)
        shift
        exec uv run --directory "$SCRIPT_DIR" python prove.py "$@"
        ;;
    -h|--help|help)
        usage
        exit 0
        ;;
    *)
        MODEL="$DEFAULT_MODEL"
        SYSTEM_PROMPT=""

        while [[ $# -gt 0 ]]; do
            case "$1" in
                --model)
                    if [[ -z "${2:-}" ]]; then
                        echo "Error: --model requires a value" >&2
                        exit 1
                    fi
                    MODEL="$2"
                    shift 2
                    ;;
                --system)
                    if [[ -z "${2:-}" ]]; then
                        echo "Error: --system requires a value" >&2
                        exit 1
                    fi
                    SYSTEM_PROMPT="$2"
                    shift 2
                    ;;
                --)
                    shift
                    break
                    ;;
                -*)
                    echo "Error: unknown option: $1" >&2
                    echo "" >&2
                    usage >&2
                    exit 1
                    ;;
                *)
                    break
                    ;;
            esac
        done

        if [[ $# -eq 0 ]]; then
            usage >&2
            exit 1
        fi

        PROMPT="$*"
        PAYLOAD="$(
            MODEL="$MODEL" SYSTEM_PROMPT="$SYSTEM_PROMPT" PROMPT="$PROMPT" python3 - <<'PY'
import json
import os

messages = []
system = os.environ.get("SYSTEM_PROMPT", "")
if system:
    messages.append({"role": "system", "content": system})
messages.append({"role": "user", "content": os.environ["PROMPT"]})

print(json.dumps({
    "model": os.environ["MODEL"],
    "messages": messages,
}))
PY
        )"

        RESPONSE="$(
            curl -fsS "$PROXY_URL/v1/chat/completions" \
                -H "Authorization: Bearer $PROXY_KEY" \
                -H "X-Caller-Skill: $CALLER_SKILL" \
                -H "Content-Type: application/json" \
                -d "$PAYLOAD"
        )" || exit $?

        RESPONSE="$RESPONSE" python3 - <<'PY'
import json
import os
import sys

response = json.loads(os.environ["RESPONSE"])
choices = response.get("choices") or []
if not choices:
    print(json.dumps(response, indent=2))
    raise SystemExit(1)

message = choices[0].get("message") or {}
content = message.get("content")

if isinstance(content, str):
    print(content)
elif content is None:
    print(json.dumps(message, indent=2))
else:
    print(json.dumps(content, indent=2))
PY
        ;;
esac
