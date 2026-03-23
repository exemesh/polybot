#!/bin/bash
# One-time setup for Cloudflare Tunnel to expose the HITL interaction server
# Run this ONCE on the Mac mini after pushing the code

echo "=== PolyBot HITL — Cloudflare Tunnel Setup ==="

# 1. Install cloudflared
if ! command -v cloudflared &> /dev/null; then
    echo "Installing cloudflared..."
    brew install cloudflare/cloudflare/cloudflared
fi

# 2. Login to Cloudflare
echo "Logging in to Cloudflare (browser will open)..."
cloudflared tunnel login

# 3. Create the tunnel
echo "Creating polybot-hitl tunnel..."
cloudflared tunnel create polybot-hitl

# 4. Get tunnel ID
TUNNEL_ID=$(cloudflared tunnel list --output json | python3 -c "
import sys, json
tunnels = json.load(sys.stdin)
for t in tunnels:
    if t['name'] == 'polybot-hitl':
        print(t['id'])
        break
")
echo "Tunnel ID: $TUNNEL_ID"

# 5. Create tunnel config
mkdir -p ~/.cloudflared
cat > ~/.cloudflared/polybot-hitl.yaml << CFEOF
tunnel: $TUNNEL_ID
credentials-file: /Users/brij/.cloudflared/$TUNNEL_ID.json
ingress:
  - hostname: polybot-hitl.YOUR_DOMAIN.workers.dev
    service: http://localhost:8765
  - service: http_status:404
CFEOF

echo ""
echo "=== NEXT STEPS ==="
echo "1. Run: cloudflared tunnel run polybot-hitl"
echo "   Note the public URL (e.g. https://polybot-hitl.xxxxx.trycloudflare.com)"
echo ""
echo "2. Go to Discord Developer Portal → Your App → General Information"
echo "   Set 'Interactions Endpoint URL' to:"
echo "   https://YOUR_TUNNEL_URL/interactions"
echo ""
echo "3. Copy DISCORD_PUBLIC_KEY from Discord Developer Portal to your .env"
echo ""
echo "4. Install the interactions service:"
echo "   cp launchd/com.polybot-interactions.plist ~/Library/LaunchAgents/"
echo "   launchctl load ~/Library/LaunchAgents/com.polybot-interactions.plist"
echo ""
echo "Buttons will now work in Discord! ✅"
