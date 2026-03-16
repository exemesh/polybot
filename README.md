# PolyBot - Polymarket Trading Bot

Automated trading bot for Polymarket prediction markets. Runs on GitHub Actions (free) with a GitHub Pages dashboard.

## Strategies

- **Weather Arbitrage** - Compares Open-Meteo weather forecasts against Polymarket temperature markets. Buys underpriced outcomes when forecast confidence exceeds market price.
- **Cross-Platform Arbitrage** - Detects YES+NO < $1.00 pricing inefficiencies on Polymarket, and cross-platform price discrepancies with Kalshi.

## Setup

### 1. Create GitHub Repository

Create a new **private** repository on GitHub, then push this code:

```bash
git remote add origin https://github.com/YOUR_USERNAME/polybot.git
git push -u origin main
```

### 2. Add Secrets

Go to **Settings > Secrets and variables > Actions** and add:

| Secret | Required | Description |
|--------|----------|-------------|
| `PRIVATE_KEY` | Yes | Polygon wallet private key (0x...) |
| `DRY_RUN` | No | `true` (default) or `false` for live |
| `INITIAL_CAPITAL` | No | Starting capital, default `100` |
| `DISCORD_WEBHOOK_URL` | No | Discord webhook URL for alerts |
| `KALSHI_API_KEY` | No | For cross-platform arbitrage |
| `KALSHI_API_SECRET` | No | For cross-platform arbitrage |
| `OPENAI_API_KEY` | No | For AI forecaster strategy |

### 3. Enable GitHub Actions

Go to **Actions** tab and enable workflows. The bot runs every 10 minutes automatically.

You can also trigger a manual run: **Actions > PolyBot Trading > Run workflow**.

### 4. Enable Dashboard

Go to **Settings > Pages**:
- Source: **Deploy from a branch**
- Branch: **gh-pages** / **(root)**

Dashboard will be available at: `https://YOUR_USERNAME.github.io/polybot/`

## Architecture

```
GitHub Actions (cron every 10min)
    ├── Restore SQLite DB from artifact
    ├── Run single scan cycle (weather + arb strategies)
    ├── Export dashboard JSON
    ├── Save DB artifact (90-day retention)
    └── Deploy dashboard to GitHub Pages
```

## Risk Management

- Quarter-Kelly position sizing
- Max 5% per trade, 40% total exposure
- 10% daily loss limit auto-halts trading
- Starts in DRY_RUN mode (paper trading)

## Mac mini Local Runner (Recommended)

Running on a Mac mini gives you reliable 5-minute intervals (GitHub Actions free
tier often delays by 20–60 minutes). The bot runs as a launchd service that
starts on login and fires every 5 minutes.

### Prerequisites

- macOS 12+ (Monterey or later)
- Python 3.11+ (`brew install python@3.11`)
- The repo cloned to `~/polybot`

### One-command setup

```bash
cd ~/polybot
chmod +x scripts/setup_mac_mini.sh
./scripts/setup_mac_mini.sh
```

This will:
1. Create a virtual environment at `~/polybot-env`
2. Install all Python requirements
3. Create a `.env` file template at `~/polybot/.env`
4. Install the launchd plist to `~/Library/LaunchAgents/`
5. Load the service (bot starts running immediately)

### Configure secrets

Edit `~/polybot/.env` and fill in your values:

```bash
nano ~/polybot/.env
```

### Manage the service

```bash
# Stop the bot
launchctl unload ~/Library/LaunchAgents/com.polybot.trader.plist

# Start the bot
launchctl load -w ~/Library/LaunchAgents/com.polybot.trader.plist

# Run once manually (with live log output)
./scripts/run_local.sh

# Watch logs
tail -f ~/polybot/logs/polybot.log
```

### Discord Alerts

Set `DISCORD_WEBHOOK_URL` in your `.env` to receive real-time alerts:
- Trade executions (rich embed with market, side, size, price, reason)
- Daily PnL summaries (green/red embed based on profit/loss)
- Error alerts (red embed with strategy name and error message)
- Bot heartbeat (green for LIVE, yellow for DRY RUN)

Create a webhook: Discord server → Channel settings → Integrations → Webhooks

---

## Going Live

1. Fund a Polygon wallet with $100 USDC
2. Set `PRIVATE_KEY` in `~/.env` (local) or as a repository secret (GitHub Actions)
3. Set `DRY_RUN=false`
4. Monitor via dashboard and Discord alerts
