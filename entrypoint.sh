#!/bin/bash

# 1. SET THE ENVIRONMENT
# We force LakeClaw to look in the /home/app directory for its runtime state.
export OPENCLAW_HOME="/home/app"
export OPENCLAW_STATE_DIR="/home/app/.openclaw"
export OPENCLAW_CONFIG_PATH="/app/python/source_code/openclaw.json"

# OAuth → AI Gateway: OpenClaw uses loopback + static key; proxy attaches M2M Bearer.
export AI_GATEWAY_PROXY_PORT="${AI_GATEWAY_PROXY_PORT:-18080}"
if [ -z "${AI_GATEWAY_PROXY_LOCAL_KEY:-}" ]; then
  export AI_GATEWAY_PROXY_LOCAL_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
fi

# 2. PREPARE THE RUNTIME HOME
# Create the directory structure in the container's writable home area.
mkdir -p "$OPENCLAW_STATE_DIR/workspace"
mkdir -p "$OPENCLAW_STATE_DIR/skills"

# 3. LINK THE MASTER CONFIG
# We symlink the 'source code' config into the 'runtime' directory.
# This means LakeClaw sees it in both places, but you only maintain it in one.
ln -sf /app/python/source_code/openclaw.json "$OPENCLAW_STATE_DIR/openclaw.json"

# 4. INITIAL SYNC (VOLUME -> APP)
# Pull the latest Soul, Memories, and Sessions from Unity Catalog before booting.
echo "--- [INIT] Syncing State from Unity Catalog Volume ---"
python sync_volume.py 

# 5. DEFINE THE PERSISTENCE TRAP
# If the Databricks App is stopped or redeployed, we trigger one last push.
cleanup() {
    echo "--- [SHUTDOWN] Final Sync to Unity Catalog Volume ---"
    python sync_volume.py --push
    exit 0
}
trap cleanup SIGTERM SIGINT

# 6. START THE BACKGROUND HEARTBEAT
# Every hour, we push updates (Sessions/Memories) back to the Volume.
(
  while true; do
    sleep 3600
    echo "--- [HEARTBEAT] Periodic Sync to Volume ---"
    python sync_volume.py --push
  done
) &

# 7. AI GATEWAY PROXY (OAuth M2M → upstream OpenAI-compatible API)
echo "--- [START] Launching AI Gateway OAuth proxy on 127.0.0.1:${AI_GATEWAY_PROXY_PORT} ---"
python ai_gateway_proxy.py &
PROXY_PID=$!
for _ in $(seq 1 50); do
  if python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${AI_GATEWAY_PROXY_PORT}/healthz', timeout=2)" 2>/dev/null; then
    echo "--- [OK] AI Gateway proxy is healthy ---"
    break
  fi
  if ! kill -0 "$PROXY_PID" 2>/dev/null; then
    echo "--- [ERR] AI Gateway proxy exited early ---"
    exit 1
  fi
  sleep 0.2
done

# 8. BOOT LAKECLAW
# We use 'npm start' which will now inherit the OPENCLAW_CONFIG_PATH.
echo "--- [START] Launching LakeClaw Gateway ---"
npm start