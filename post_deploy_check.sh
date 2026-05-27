#!/bin/bash
# Post-deploy health check — runs automatically via Claude Code PostToolUse hook.
# Fires after every Bash tool call; only acts when the command was a git push.

HEALTH_URL="https://ghl-webhook-server-62c2.onrender.com/health"
POLL_INTERVAL=10
MAX_WAIT=120

# Read hook event JSON from stdin
EVENT=$(cat 2>/dev/null)
COMMAND=$(echo "$EVENT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('tool_input', {}).get('command', ''))
except Exception:
    pass
" 2>/dev/null)

# Only act on git push commands
if [[ "$COMMAND" != *"git push"* ]]; then
    exit 0
fi

echo ""
echo "🚀 Deploy detected — waiting for server to come back up..."

# Poll until HTTP 200 or timeout
elapsed=0
while [ $elapsed -lt $MAX_WAIT ]; do
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$HEALTH_URL" 2>/dev/null)
    if [ "$STATUS" = "200" ]; then
        break
    fi
    sleep $POLL_INTERVAL
    elapsed=$((elapsed + POLL_INTERVAL))
done

if [ $elapsed -ge $MAX_WAIT ]; then
    echo "❌ Server did not come back within ${MAX_WAIT}s — check Render dashboard."
    exit 1
fi

# Check GHL health
RESPONSE=$(curl -s "$HEALTH_URL" 2>/dev/null)
GHL_STATUS=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('ghl', 'unknown'))
except Exception:
    print('unknown')
" 2>/dev/null)

REASON=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('reason', ''))
except Exception:
    print('')
" 2>/dev/null)

if [ "$GHL_STATUS" = "ok" ]; then
    echo "✅ GHL health OK after deploy — all dependencies verified."
    exit 0
else
    echo "❌ GHL health degraded after deploy: ${REASON}"
    echo "   → Check Render logs for startup ❌ errors and fix env vars."
    exit 1
fi
