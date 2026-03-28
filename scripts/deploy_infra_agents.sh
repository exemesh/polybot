#!/bin/bash
# deploy_infra_agents.sh
# Deploys the three infrastructure automation agents on Mac mini.
# Run once after git pull to register log-relay, dep-watchdog, and infra-health.
#
# Usage: bash ~/polybot/scripts/deploy_infra_agents.sh

set -e

LAUNCHD_DIR=~/Library/LaunchAgents
PLIST_SRC=~/polybot/launchd
LOG_DIR=~/polybot/logs

echo "📦 Deploying polybot infrastructure agents..."

# Ensure log directory exists
mkdir -p "$LOG_DIR"

# Install Python deps for new scripts (httpx should already be installed)
echo "🐍 Checking Python dependencies..."
/opt/homebrew/bin/python3.11 -m pip install httpx --quiet --upgrade

SERVICES=(
    "com.polybot.log-relay"
    "com.polybot.dep-watchdog"
    "com.polybot.infra-health"
)

for SVC in "${SERVICES[@]}"; do
    PLIST="$PLIST_SRC/${SVC}.plist"
    DEST="$LAUNCHD_DIR/${SVC}.plist"

    if [ ! -f "$PLIST" ]; then
        echo "❌ Missing: $PLIST"
        continue
    fi

    # Unload if already loaded (ignore errors if not loaded)
    launchctl unload "$DEST" 2>/dev/null || true

    # Copy plist to LaunchAgents
    cp "$PLIST" "$DEST"

    # Load the service
    launchctl load "$DEST"
    echo "✅ Loaded: $SVC"
done

echo ""
echo "🔍 Current polybot launchd services:"
launchctl list | grep polybot

echo ""
echo "✅ Infra agents deployed. Schedule:"
echo "   log-relay     → every 5 minutes (ships errors/trades to Discord)"
echo "   dep-watchdog  → every Sunday 09:00 WAT (Python/pip/brew update check)"
echo "   infra-health  → every day 07:30 WAT (deep health check + Discord report)"
