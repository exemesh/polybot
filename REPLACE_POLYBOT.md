# Replace your existing polybot repo with v4

Run these commands from your current polybot directory (`cd ~/polybot`).
Steps are deliberately explicit so you can abort at any stage.

## 1. Stop the old bot

```bash
# If launchd is running it
launchctl unload ~/Library/LaunchAgents/com.polybot.trader.plist 2>/dev/null || true
launchctl unload ~/Library/LaunchAgents/com.polybot.v3.plist 2>/dev/null || true

# Confirm no polybot process is running
pgrep -fl polybot || echo "no polybot process running"
```

## 2. Archive the old code (safety net)

```bash
cd ~/polybot
git checkout -b "archive-v3-$(date +%Y%m%d)"
git push -u origin "archive-v3-$(date +%Y%m%d)"
git checkout main
```

You can always recover v3 with `git checkout archive-v3-YYYYMMDD`.

## 3. Wipe main and drop in v4

```bash
# From ~/polybot — WIPES everything tracked on main
git rm -rf .
git clean -fdx  # also removes untracked (venv, __pycache__, logs, .env)

# Copy v4 in — adjust SOURCE path to wherever you saved the polybot-v4/ dir
SOURCE="$HOME/Downloads/polybot-v4"  # <-- change this to your actual path
cp -R "$SOURCE"/. .
```

Confirm the files are present:

```bash
ls -la
# Should see: README.md, requirements.txt, config/, src/, scripts/, launchd/ etc.
```

## 4. Preserve your old `.env` if you want to reuse credentials

```bash
# The .env in the old repo is gitignored so the clean didn't delete it from
# your working tree — unless you ran `git clean -fdx`, which wipes it.
# If you did clean it away, recover from your archive branch:
git checkout "archive-v3-$(date +%Y%m%d)" -- .env
# or if that also fails, copy from the template and re-fill:
cp .env.example .env
nano .env
```

## 5. Run setup for v4

```bash
chmod +x scripts/setup_mac.sh scripts/run.sh
./scripts/setup_mac.sh
```

This creates a fresh `venv`, installs requirements, and drops a launchd plist
into `~/Library/LaunchAgents/com.polybot.v4.plist` (but does NOT load it —
you want to dry-run first).

## 6. Dry-run smoke test

```bash
./scripts/run.sh
```

In a second terminal:

```bash
tail -f logs/bot.log
tail -f logs/trades.jsonl | jq .
```

Let it run for 1–2 hours. You should see:
- Heartbeat lines every 60s
- "FIRE" lines when a signal passes all 5 gates
- Corresponding "entry" JSON in trades.jsonl with `"dry_run": true`

If you see no fires after 2 hours, that's normal — the 5-gate filter is
strict. Try widening `strategy.min_vwap_deviation_pct` from 3.0 to 2.0 in
config/config.json as a first loosening step.

## 7. Force-push v4 to GitHub

Once you're happy with the local dry-run output:

```bash
cd ~/polybot
git add .
git commit -m "v4: single-strategy Late Entry V3 replacement

Replaces 13-strategy v3 with one signal + aggressive safety rails:
- Hardcoded \$5 max bet, \$30 daily loss cap, \$60 weekly cap
- Late Entry V3 (txbabaxyz-inspired) on BTC/ETH 15-min up/down markets
- Binance momentum + VWAP deviation as confirmation
- Flip-stop + stop-loss exit logic
- Emergency stop file + confirmation-token live mode

Archive branch: archive-v3-$(date +%Y%m%d)"

git push --force-with-lease origin main
```

`--force-with-lease` is safer than `--force`: it refuses to overwrite remote
changes you don't know about.

## 8. Load the v4 launchd (only after a full dry-run session)

```bash
launchctl load -w ~/Library/LaunchAgents/com.polybot.v4.plist
launchctl list | grep polybot  # confirm it's loaded
```

The default plist runs in **dry-run** mode. To switch to live, edit the
plist's `ProgramArguments` to add `--live` and reload — but do that only
after 50+ resolved dry-run trades with a win rate at or above the break-even
wall (if avg entry = 0.82, realized win rate must be ≥ 82%).

## 9. Go-live gate — do not skip

Before adding `--live`:

- [ ] `./scripts/analyze_trades.py` shows win_rate ≥ avg_entry
- [ ] At least 50 resolved trades in `logs/trades.jsonl`
- [ ] `logs/error.log` has no unexplained entries in the last 24h
- [ ] You've funded a **dedicated** Polygon wallet with $50 USDC (not $300)
- [ ] You've read `src/safety_guard.py` end-to-end
- [ ] You've set a calendar reminder to review trades at 12h / 24h / 72h
- [ ] You've tested the emergency stop: `touch EMERGENCY_STOP` and watch the
      bot flatten + exit within 2s, then `rm EMERGENCY_STOP`

## 10. If anything goes wrong

```bash
# Immediate flatten + stop
touch ~/polybot/EMERGENCY_STOP

# Check process
pgrep -fl polybot

# Unload launchd
launchctl unload ~/Library/LaunchAgents/com.polybot.v4.plist

# Rollback to v3
cd ~/polybot
git checkout "archive-v3-$(date +%Y%m%d)"
```

You have a rollback path at every stage. Use it without hesitation.
