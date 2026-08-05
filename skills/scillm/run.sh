#!/bin/bash
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PROJECT_ROOT="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")"

# Load .env if present
if [ -f "$PROJECT_ROOT/.env" ]; then
    set -a
    source "$PROJECT_ROOT/.env"
    set +a
fi

# Ensure uv is available
if ! command -v uv &> /dev/null; then
    echo "Error: uv is not installed. Please install uv."
    exit 1
fi

# Clear parent VIRTUAL_ENV to avoid mismatch warning when uv resolves its own .venv
unset VIRTUAL_ENV

PROXY_URL="${SCILLM_API_BASE:-http://localhost:4001}"
PROXY_KEY="${SCILLM_PROXY_KEY:-${SCILLM_MASTER_KEY:-${LITELLM_MASTER_KEY:-sk-dev-proxy-123}}}"
DEFAULT_MODEL="${SCILLM_MODEL:-text}"
CALLER_SKILL="${SCILLM_CALLER_SKILL:-scillm}"
SCILLM_REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"

usage() {
    cat <<'EOF'
Usage: run.sh [--model MODEL] [--system TEXT] [PROMPT...]
       run.sh generate-image --prompt-file PATH --out PATH [options]
       run.sh prove [args...]

Chat (one-shot text/VLM):
  run.sh "Explain quantum computing in one sentence"
  run.sh --model moonshot-text "Describe this screenshot context"

Image generation (NOT chat — use this for create-image tasks):
  run.sh generate-image \
    --prompt-file path/to/spec.prompt.md \
    --out artifacts/images/asset.png \
    --auth openai-api-key \
    --model gpt-image-2 \
    --quality high

Termination contract (image):
  - Progress NDJSON on stderr: type scillm.image.started | scillm.image.completed | scillm.image.failed
  - Success: exit 0 AND final stderr line has "scillm.image.completed" with terminal=true
  - Do NOT use chat run.sh for image generation (it calls /v1/chat/completions and will not produce PNGs).

Notes:
  - Default chat model is "text" unless SCILLM_MODEL is set.
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
    generate-image)
        shift
        GEN_ARGS=()
        while [[ $# -gt 0 ]]; do
            GEN_ARGS+=("$1")
            shift
        done
        if [[ ${#GEN_ARGS[@]} -eq 0 ]]; then
            echo "Error: generate-image requires --prompt-file and --out" >&2
            exit 1
        fi
        exec python3 "$SCILLM_REPO/scripts/generate_image.py" "${GEN_ARGS[@]}"
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
