# Kalshi Stop

A minimal server-side stop-loss controller for positions entered manually in the Kalshi app.

## What it does

- Authenticated Kalshi WebSocket stays connected server-side.
- Loads existing open positions from REST, then receives live position/fill updates.
- Subscribes only to order books for markets you actually hold.
- Dashboard lets you set a stop in the same held-side cents you see in the app.
- Long YES watches best executable YES bid.
- Long NO watches best executable NO bid.
- Trigger submits an Immediate-or-Cancel (IOC) V2 order with `reduce_only=true`.
- `reduce_only` prevents an oversized/stale exit request from reversing the position.
- Stop engine keeps running if your phone/browser disconnects.
- Stops are persisted in `DATA_DIR/stops.json`.

## Exit pricing

Example: long YES, stop 54¢, max slippage 3¢ → trigger at <=54¢ and submit an IOC sell-YES order with a 51¢ limit.

Example: long NO, stop 54¢, max slippage 3¢ → trigger at <=54¢ NO and submit the equivalent buy-YES IOC at up to 49¢ (1 - 51¢).

## Required environment variables

See `.env.example`.

For production:

- `KALSHI_ENV=prod`
- `KALSHI_KEY_ID=...`
- `KALSHI_PRIVATE_KEY=-----BEGIN PRIVATE KEY-----...`
- `DASHBOARD_PASSWORD=...`
- `COOKIE_SECRET=...`
- `EXECUTION_ENABLED=true` only after demo testing
- `DATA_DIR=/data`

Never put the private key in browser code or commit it to GitHub.

## Railway

1. Push this repository to GitHub.
2. Create a Railway project/service from the repo.
3. Add a persistent volume mounted at `/data`.
4. Add the environment variables above.
5. Generate a Railway domain.
6. Confirm `/health` shows `ok: true`, `ws_connected: true`, `kalshi_configured: true`.

For lowest latency, run the service in a US-East region near Kalshi's API infrastructure if your hosting plan allows region selection.

## Safety behavior

- Cannot arm with zero position.
- Cannot arm while the Kalshi WebSocket is disconnected.
- Position direction flip automatically disarms the old stop.
- Position going flat automatically disarms the stop.
- Stale armed stops with no position are disarmed on restart.
- IOC prevents an unfilled remainder from resting unexpectedly.
- `reduce_only` caps the exit at current position.
- Default is `EXECUTION_ENABLED=false`.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# export variables from .env, then:
uvicorn app.main:app --reload --port 8080
```
