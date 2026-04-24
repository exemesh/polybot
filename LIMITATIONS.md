# Known limitations & things to verify before going live

I built this bot without being able to run it against live APIs. The following
items are **best-guess** implementations that you MUST verify during the
dry-run phase before enabling `--live`.

## 1. Polymarket CLOB order response field names

In `src/polymarket_client.py`, I parse response fields like `making_amount`,
`filled_size`, `orderID` from `py_clob_client.post_order()`. The real response
shape may differ. **Action:** during dry-run, add a line to log the raw
`result.raw` for at least one real test entry to see the actual keys, then
adjust the parser.

Quick test:
```bash
# With DRY_RUN=false but a tiny amount, manually run:
python -c "
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, ApiCreds
from py_clob_client.order_builder.constants import BUY
# ... your creds
c = ClobClient(host='https://clob.polymarket.com', key=YOUR_PK, chain_id=137, signature_type=0)
c.set_api_creds(ApiCreds(api_key=..., api_secret=..., api_passphrase=...))
o = c.create_order(OrderArgs(price=0.50, size=5, side=BUY, token_id=ANY_ACTIVE_TOKEN))
r = c.post_order(o, OrderType.FAK)
print(r)
"
```

## 2. Polymarket WebSocket subscription payload shape

In `src/polymarket_ws.py` I send `{"type": "MARKET", "assets_ids": [...]}`.
The actual accepted format for the CLOB market channel may be
`{"type": "subscribe", "channel": "market", "assets_ids": [...]}` or similar.
**Action:** tail `logs/bot.log` after startup — you should see book events
flowing within 5s of market discovery. If you see `pm_ws subscribed to N
tokens` but no book updates, fix the payload shape.

## 3. Gamma API token-ID ordering

In `src/market_discovery.py::_extract_token_ids()`, I assume the first
`clobTokenIds` entry is UP/YES and the second is DOWN/NO — and I try to
correct for it using the `outcomes` list when available.

**Action:** during dry-run, log one entry and verify against the market page
on polymarket.com that we're correctly identifying UP vs DOWN. A mis-mapping
means every trade is on the wrong side. I mitigate this by also checking the
outcomes list, but verify before live.

## 4. Slug patterns for market discovery

The slug prefixes in `config/config.json` (`bitcoin-up-or-down-`, etc.) are
what Polymarket has historically used. If they've renamed the series, market
discovery returns nothing.

**Action:** test with:
```bash
curl -s "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=50" \
  | jq -r '.[].slug' | grep -i 'up-or-down\|updown\|up_down'
```

If the returned slugs differ, update the `market_slug_pattern` in config.

## 5. USDC balance fetch assumes EOA wallet

`PolymarketClient.get_usdc_balance()` reads USDC.bridged from the signer EOA
directly. If you use a proxy wallet (`SIGNATURE_TYPE=1`) or Gnosis Safe
(`SIGNATURE_TYPE=2`), USDC is held in the proxy contract, not your signer
EOA.

**Action:** if `SIGNATURE_TYPE != 0`, replace the balance-fetch logic to
query `FUNDER_ADDRESS` instead of the signer.

## 6. Winning-position redemption is NOT implemented

When a market resolves, winners need an on-chain `redeemPositions()` call on
the CTF exchange contract to convert winning tokens → USDC. This bot does not
do that. It just marks the position closed at 1.0 or 0.0 in the internal
ledger based on the Binance window return.

**Action (MVP):** manually redeem via polymarket.com every morning. Go to
account → positions → "Redeem all".

**Action (production):** implement a redeemer using
`py_clob_client.get_notifications()` + direct CTF contract calls. See
`txbabaxyz/4coinsbot/src/simple_redeem_collector.py` for a reference
implementation you can adapt.

## 7. Binance window-return calculation is approximate

The end-of-window "did we win?" check in `main.py::_maybe_exit()` computes
the window return using `price_n_seconds_ago(900)` — the Binance price 15
minutes ago. Polymarket actually resolves off Chainlink oracle readings at
specific block timestamps. These can differ by a few seconds around the
window boundary.

**Action:** treat the bot's in-memory "won/lost" marking as indicative only.
Actual P&L is always the on-chain settlement. Trust `logs/trades.jsonl` for
signal generation and polymarket.com for true settlement P&L.

## 8. No rate limiting on Gamma API

Gamma API has an unofficial rate limit (≈ 5 req/s). The current code fetches
on demand per coin. At 4 coins on 30s cache TTL that's fine, but if you
enable more coins, add a global token bucket.

## 9. Single-process; no redundancy

If your Mac mini reboots mid-trade, the open position is logged to
`data/positions.json` and will be flattened on next startup via
`_shutdown()`. But there's no watchdog — if the process dies silently
(OOM, power, etc.) the position sits open until the next time you start the
bot. At $5/trade this is an acceptable risk. At larger sizes, add a watchdog.

## 10. VWAP uses Binance trade volume (not PM order flow)

The strategy's VWAP deviation is computed on Binance 1-second kline volume.
This is the correct reference for "is BTC extending its intra-window move?"
but *not* a direct signal on Polymarket order flow. If PM retail floods the
book with lot-sized orders that disagree with Binance, you won't see it in
the VWAP. Flip-stop is your backstop for this.

---

## Before you enable `--live`

Work through each of the above. Most can be validated with a single `curl`
or `print` during dry-run. None requires writing more Python — they're
verifications, not features. Budget 1–2 hours to do them properly.
