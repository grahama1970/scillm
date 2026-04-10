#!/bin/bash
# Auto-select fastest Chutes model and update scillm config
# Run on startup or via cron

set -e

SKILL_DIR="$HOME/.claude/skills/ops-chutes"
CONFIG="/home/graham/workspace/experiments/scillm/local/proxy_server_config.yaml"

# Get current model from config
CURRENT=$(grep -A3 "model_name: text$" "$CONFIG" | grep "model:" | head -1 | awk '{print $2}')

# Get recommended model (JSON output, parse best)
BEST=$("$SKILL_DIR/run.sh" recommend "$CURRENT" --json 2>/dev/null | jq -r '.best // empty')

if [ -z "$BEST" ]; then
    echo "Could not determine best model"
    exit 1
fi

if [ "$CURRENT" = "$BEST" ]; then
    echo "Already using best model: $CURRENT"
    exit 0
fi

echo "Switching: $CURRENT -> $BEST"

# Update config
sed -i "s|model: $CURRENT|model: $BEST|" "$CONFIG"

# Restart proxy
cd /home/graham/workspace/experiments/scillm
docker compose -p scillm -f deploy/docker/compose.scillm.core.yml restart scillm-proxy

echo "Done. Now using: $BEST"
