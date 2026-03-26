#!/bin/bash

# 1. SET THE ENVIRONMENT
# We force LakeClaw to look in the /home/app directory for its runtime state.
export OPENCLAW_HOME="/home/app"
export OPENCLAW_STATE_DIR="/home/app/.openclaw"
export OPENCLAW_CONFIG_PATH="/app/python/source_code/openclaw.json"

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

# 7. BOOT LAKECLAW
# We use 'npm start' which will now inherit the OPENCLAW_CONFIG_PATH.
echo "--- [START] Launching LakeClaw Gateway ---"
npm start