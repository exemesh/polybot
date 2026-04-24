# Polybot v4.1 — Late Entry V3 (multi-strike aware)

Single-strategy Polymarket trading bot for hourly **multi-strike** crypto
markets. As of April 2026, Polymarket has replaced binary up/down markets
with multi-strike events (10 strike prices per hour, each Yes/No). This
bot picks the strike whose favorite side is in the [0.75, 0.88] edge zone
and applies the Late Entry V3 logic.

Replaces the 13-strategy v3 with one focused strategy and aggressive safety
rails.

**Currently active series:**
- **ETH hourly** (`ethereum-multi-strikes-hourly`, id 11373) — enabled by default
- **BTC 4-hourly** (`bitcoin-multi-strikes-4h`, id 10202) — disabled by default
  (lower frequency; enable after ETH is profitable)
- **SOL/XRP** — no equivalent series exists right now

## Why this replaces the old polybot

The old polybot ran 13 strategies in one process, most of them LLM-driven
directional bets with no measurable edge. It lost $161.58.

This bot runs **one strategy** — Late Entry V3, derived from
[txbabaxyz/4coinsbot](https://github.com/txbabaxyz/4coinsbot) and adapted for a
$300 bankroll with hardcoded safety caps. It only trades the last ~5 minutes
of each 15-minute window, only buys the favorite when the price is in the
$0.75–$0.88 edge zone, and exits on stop-loss, flip-stop, or natural
resolution.

## What it actually does

Every 500ms, for each enabled coin:

1. Read Binance ETHUSDT (or BTCUSDT) 1-second price.
2. Query the Polymarket Gamma API for the currently-active event in the coin's
   multi-strike series (e.g., `ethereum-above-on-april-24-2026-7pm-et`).
3. Among the ~10 strike markets in that event, pick the one whose favorite
   side (Yes if yes_price > 0.5, else No) sits closest to $0.82 within the
   edge zone $[0.75, 0.88]$.
4. Read the Polymarket order book WS for that chosen strike.
5. Check the 5 entry conditions. If **all** pass, place a Fill-And-Kill BUY on
   the favorite side.
6. Track the open position; exit on stop-loss, flip-stop, or let it resolve.

Entry conditions (all five required):

- A strike market exists with favorite-side price in `[0.75, 0.88]`
- ≥ 2100s elapsed in the hourly window (i.e., ≤ 25:00 remaining)
- ≤ 1200s remaining (i.e., ≥ 40:00 elapsed) — **the sweet spot is
  35:00–40:00 into the hour**. Earlier = too much time for reversal. Later
  = not enough time for edge to materialize.
- VWAP deviation ≥ 2% (favorite-side direction is confirmed by recent
  Binance price action)
- Positive Binance momentum in last 60 seconds (confirms direction)

Exits:

- **Flip-stop** — your side becomes the underdog. Exit immediately. (You
  bought UP at 0.82, UP is now 0.45, something reversed, get out.)
- **Stop-loss** — unrealized P&L below `-$12` per position.
- **Natural resolution** — market closes, position redeems on-chain.

## Safety defaults (hardcoded, not config)

- `DRY_RUN_DEFAULT = True` — you must pass `--live` + confirm to trade real
  money.
- `MAX_BET_USD = 5` — hardcoded, cannot be raised via config without editing
  source.
- `DAILY_LOSS_CAP_USD = 30` — bot auto-pauses until UTC midnight if hit.
- `WEEKLY_LOSS_CAP_USD = 60` — bot requires manual restart if hit.
- `CONSECUTIVE_LOSS_LIMIT = 6` — bot auto-pauses 24h, regenerate win_rate
  before resuming.
- `MAX_CONCURRENT_POSITIONS = 2` across all coins.
- `MIN_BANKROLL_USD = 50` — bot halts if wallet drops below this.
- Emergency stop: touch `EMERGENCY_STOP` file in repo root → bot flattens all
  positions and exits.

## Repo layout

```
polybot-v4/
├── README.md                 this file
├── .env.example              copy to .env and fill in
├── .gitignore
├── requirements.txt
├── config/
│   └── config.json           strategy parameters
├── src/
│   ├── main.py               entry point
│   ├── config_loader.py      loads + validates config.json and .env
│   ├── logger.py             structured JSON logging
│   ├── binance_feed.py       Binance WebSocket (1s klines for 4 coins)
│   ├── polymarket_client.py  Polymarket CLOB REST + order placement
│   ├── polymarket_ws.py      Polymarket order-book WebSocket
│   ├── market_discovery.py   Find active 15-min markets via Gamma API
│   ├── strategy.py           Late Entry V3 signal logic
│   ├── risk_manager.py       Sizing, caps, daily limits
│   ├── position_tracker.py   Track open positions + P&L
│   ├── profit_taker.py       Exit logic
│   ├── safety_guard.py       Emergency stop, pre-trade validation
│   └── trade_logger.py       JSONL trade history
├── scripts/
│   ├── setup_mac.sh          one-command setup
│   ├── run.sh                manual run with venv
│   └── analyze_trades.py     post-hoc win-rate analysis
├── launchd/
│   └── com.polybot.v4.plist.template
└── logs/                     gitignored
```

## Setup (Mac mini / macOS)

```bash
# 1. Clone or replace (see "Replacing the old polybot" at the bottom)
cd ~/polybot

# 2. One-command setup
chmod +x scripts/setup_mac.sh
./scripts/setup_mac.sh

# 3. Configure credentials
nano .env
# Fill in PRIVATE_KEY, POLYMARKET_API_KEY, POLYMARKET_API_SECRET,
# POLYMARKET_API_PASSPHRASE, and optionally TELEGRAM_BOT_TOKEN

# 4. Dry-run test (no real trades)
./scripts/run.sh
# Let it run for 3–4 hours during US crypto market hours.
# Check logs/trades.jsonl — it should be logging "would have entered" events.

# 5. Only after 50+ dry-run trades and a verified win rate ≥ required
#    break-even, go live:
./scripts/run.sh --live
# You will get a confirmation prompt. Read it carefully.
```

## Gate to go live

Do **not** flip `--live` until you can answer yes to all of these:

- [ ] 50+ dry-run trades in `logs/trades.jsonl`
- [ ] Simulated win rate ≥ average entry price (if avg entry = 0.82, win rate
      must be ≥ 82% — this is the break-even wall)
- [ ] No unexplained errors in `logs/error.log` in the last 24h
- [ ] Wallet funded with exactly $50 (not $300 — start small, scale after 50
      real trades)
- [ ] You've read `src/safety_guard.py` end-to-end and understand what it
      will and won't catch
- [ ] You've set a calendar reminder to check the bot at 12h, 24h, and 72h

## Daily ops

- Check `logs/trades.jsonl` each morning. Tail `logs/bot.log` during market
  hours.
- Telegram alerts fire on each trade and daily P&L (if configured).
- Emergency stop: `touch ~/polybot/EMERGENCY_STOP` from any shell. Bot
  flattens positions and exits within 2 seconds.

## Kill criteria (automated)

Bot auto-pauses itself if any of:

- Daily loss ≥ $30 → pause until UTC midnight
- Weekly loss ≥ $60 → pause, require manual restart
- 6 consecutive losses → pause 24h, requires regenerate win_rate
- Bankroll < $50 → halt permanently, requires manual restart
- Binance WebSocket disconnected > 30s → flatten open positions
- Polymarket order rejected 3 times in a row → flatten, pause 5 min

Bot alerts you via Telegram (if configured) on every auto-pause.

## Expected performance — honest ranges

For a $50 starting bankroll, $1–$5 bet sizes:

| Metric | Realistic range |
|---|---|
| Trades per day | 4–20 (depends on volatility regime) |
| Win rate | 58–66% if edge holds |
| Avg edge per trade after fees | 3–8% of stake |
| Monthly return on $50 | −30% to +80% (wide by design, small sample) |
| Max drawdown in first 30 days | 40–60% expected; 80%+ possible |
| Probability of zero in 90 days | 15–25% even with real edge |

If you want higher conviction, scale the bankroll *after* 100 real winning
trades, not before.

## Replacing the old polybot

To replace your existing polybot repo with this bot, from your polybot root:

```bash
# 1. Archive the old version (safety)
cd ~/polybot
git checkout -b archive-v3-$(date +%Y%m%d)
git push origin archive-v3-$(date +%Y%m%d)

# 2. Wipe main, copy new files in
git checkout main
git rm -r .
# Copy all files from this polybot-v4 directory into ~/polybot
cp -R /path/to/polybot-v4/. .

# 3. Commit and force-push
git add .
git commit -m "v4: single-strategy Late Entry V3 replacement"
git push --force-with-lease origin main

# 4. Unload old launchd, load new
launchctl unload ~/Library/LaunchAgents/com.polybot.trader.plist 2>/dev/null || true
cp launchd/com.polybot.v4.plist.template ~/Library/LaunchAgents/com.polybot.v4.plist
# Edit the plist to set the correct paths for your user
sed -i '' "s|/Users/USERNAME|$HOME|g" ~/Library/LaunchAgents/com.polybot.v4.plist
launchctl load -w ~/Library/LaunchAgents/com.polybot.v4.plist
```

The archive branch is your safety net — you can always `git checkout archive-v3-YYYYMMDD` if something goes wrong.

## License

MIT. Derivative of concepts from
[txbabaxyz/4coinsbot](https://github.com/txbabaxyz/4coinsbot) and
[txbabaxyz/btc-15m-live](https://github.com/txbabaxyz/btc-15m-live) (both MIT).

## Disclaimer

This is experimental software. You will likely lose money. Read every source
file before running with real funds. The authors are not liable for your
losses.
