# Kalshi Stop

A minimal server-side stop-loss controller for positions entered manually in the Kalshi app.

## What it does

- Authenticated Kalshi WebSocket stays connected server-side.
- Loads existing open positions from REST, then receives live position/fill updates.
- Subscribes only to order books for markets you actually hold.
- Dashboard lets you set a stop in the same held-side cents you see in the app.
- Long YES watches best executable YES bid; long NO watches best executable NO bid.
- Trigger submits an Immediate-or-Cancel (IOC) V2 order with `reduce_only=true`.
- Stop engine keeps running if your phone/browser disconnects.
- Stops persist in `DATA_DIR/stops.json`.

## Kalshi credentials

The service deliberately uses the same variable names and hardened key parsing as Football-Bot:

- `KALSHI_API_KEY_ID`
- `KALSHI_PRIVATE_KEY`
- `KALSHI_PRIVATE_KEY_B64`
- `KALSHI_PRIVATE_KEY_PATH`

The loader repairs quoted/flattened PEM values, literal `\\n` newlines, whitespace-damaged PEM bodies, and also accepts base64 of either PEM or DER key bytes. `KALSHI_PRIVATE_KEY_B64` is the most robust Railway representation.

The default production REST/WebSocket endpoints are Kalshi's current dedicated `external-api` endpoints. `KALSHI_REST` and `KALSHI_WS` can override them if needed.

## Exit pricing

Example: long YES, stop 54¢, max slippage 3¢ → trigger at <=54¢ and submit an IOC sell-YES order with a 51¢ limit.

Example: long NO, stop 54¢, max slippage 3¢ → trigger at <=54¢ NO and submit the equivalent buy-YES IOC at up to 49¢ (1 - 51¢).

## Production variables

- `KALSHI_ENV=prod`
- Kalshi credentials above
- `DASHBOARD_PASSWORD=...`
- `COOKIE_SECRET=...`
- `EXECUTION_ENABLED=true`
- `DATA_DIR=/data`

Never put the private key in browser code or commit it to GitHub.

## Safety behavior

- Cannot arm with zero position.
- Cannot arm while the Kalshi WebSocket is disconnected.
- Position direction flip automatically disarms the old stop.
- Position going flat automatically disarms the stop.
- Stale armed stops with no position are disarmed on restart.
- IOC prevents an unfilled remainder from resting unexpectedly.
- `reduce_only` caps the exit at the current position.

## Railway

Mount a persistent volume at `/data`, expose the service, then confirm `/health` reports `ok`, `ws_connected`, and `kalshi_configured` as true before relying on live stops.
