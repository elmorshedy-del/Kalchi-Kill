from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
import uuid
from decimal import Decimal
from typing import Awaitable, Callable, Dict, Optional

import httpx
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

PROD_REST = "https://external-api.kalshi.com/trade-api/v2"
DEMO_REST = "https://external-api.demo.kalshi.co/trade-api/v2"
PROD_WS = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
DEMO_WS = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"

class KalshiError(RuntimeError):
    pass

def _normalize_pem(raw: str) -> str:
    s = raw.strip().strip('"').strip("'")
    if "\\n" in s or "\\r" in s:
        s = s.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "")
    m = re.search(r"-----BEGIN ([A-Z0-9 ]+?)-----(.*?)-----END \1-----", s, re.DOTALL)
    if not m:
        return s
    label, body = m.group(1).strip(), m.group(2)
    b64 = re.sub(r"[^A-Za-z0-9+/=]", "", body)
    wrapped = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
    return f"-----BEGIN {label}-----\n{wrapped}\n-----END {label}-----\n"

def _candidate_key_blobs() -> list[bytes]:
    blobs: list[bytes] = []
    b64_value = os.getenv("KALSHI_PRIVATE_KEY_B64", "")
    pem_value = os.getenv("KALSHI_PRIVATE_KEY", "")
    path_value = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    if b64_value:
        raw = re.sub(r"\s+", "", b64_value.strip().strip('"').strip("'"))
        try:
            decoded = base64.b64decode(raw)
            blobs.append(decoded)
            try:
                blobs.append(_normalize_pem(decoded.decode()).encode())
            except Exception:
                pass
        except Exception as e:
            raise ValueError(f"KALSHI_PRIVATE_KEY_B64 is not valid base64: {e}") from e
    if pem_value:
        blobs.append(_normalize_pem(pem_value).encode())
        blobs.append(pem_value.encode())
    if path_value:
        with open(path_value, "rb") as f:
            data = f.read()
        blobs.append(data)
        try:
            blobs.append(_normalize_pem(data.decode()).encode())
        except Exception:
            pass
    return blobs

def _load_private_key():
    blobs = _candidate_key_blobs()
    if not blobs:
        return None
    errors = []
    for data in blobs:
        for loader in (serialization.load_pem_private_key, serialization.load_der_private_key):
            try:
                return loader(data, password=None)
            except Exception as e:
                errors.append(f"{loader.__name__}: {type(e).__name__}")
    raise ValueError(
        "Kalshi private key could not be parsed (tried PEM and DER across all supported forms). "
        "Prefer KALSHI_PRIVATE_KEY_B64 containing base64 of the complete unencrypted key file. "
        "Attempts: " + ", ".join(dict.fromkeys(errors))
    )

class KalshiClient:
    def __init__(self):
        self.env = os.getenv("KALSHI_ENV", "prod").strip().lower()
        if self.env not in {"demo", "prod", "production"}:
            raise ValueError("KALSHI_ENV must be demo or prod")
        is_prod = self.env in {"prod", "production"}
        self.rest_base = os.getenv("KALSHI_REST", PROD_REST if is_prod else DEMO_REST).rstrip("/")
        self.ws_url = os.getenv("KALSHI_WS", PROD_WS if is_prod else DEMO_WS)
        self.key_id = os.getenv("KALSHI_API_KEY_ID", "").strip() or os.getenv("KALSHI_KEY_ID", "").strip()
        self.subaccount = int(os.getenv("KALSHI_SUBACCOUNT", "0"))
        self.private_key = _load_private_key() if self.key_id else None
        self.http = httpx.AsyncClient(http2=True, timeout=httpx.Timeout(2.5, connect=1.5))

    @property
    def configured(self) -> bool:
        return bool(self.key_id and self.private_key)

    def _signature(self, timestamp_ms: str, method: str, path: str) -> str:
        if not self.private_key:
            raise KalshiError("Kalshi private key is not configured")
        msg = f"{timestamp_ms}{method.upper()}{path.split('?')[0]}".encode()
        sig = self.private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": self._signature(ts, method, path),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    async def request(self, method: str, path: str, *, params=None, json_body=None) -> dict:
        if not self.configured:
            raise KalshiError("Kalshi credentials are not configured")
        r = await self.http.request(
            method,
            self.rest_base + path,
            params=params,
            json=json_body,
            headers=self.headers(method, "/trade-api/v2" + path),
        )
        if r.status_code >= 400:
            raise KalshiError(f"Kalshi {r.status_code}: {r.text[:500]}")
        return r.json()

    async def get_positions(self) -> Dict[str, Decimal]:
        positions: Dict[str, Decimal] = {}
        cursor: Optional[str] = None
        while True:
            params = {"limit": 1000, "count_filter": "position", "subaccount": self.subaccount}
            if cursor:
                params["cursor"] = cursor
            data = await self.request("GET", "/portfolio/positions", params=params)
            for p in data.get("market_positions", []):
                positions[p["ticker"]] = Decimal(str(p.get("position_fp", "0")))
            cursor = data.get("cursor") or None
            if not cursor:
                break
        return positions

    async def create_reduce_ioc(self, ticker: str, position: Decimal, held_floor: Decimal) -> dict:
        count = abs(position)
        if count <= 0:
            raise KalshiError("No position to reduce")
        if position > 0:
            side, yes_price = "ask", held_floor
        else:
            side, yes_price = "bid", Decimal("1") - held_floor
        yes_price = min(Decimal("0.9999"), max(Decimal("0.0001"), yes_price))
        payload = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": side,
            "count": f"{count:.2f}",
            "price": f"{yes_price:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "maker",
            "post_only": False,
            "cancel_order_on_pause": True,
            "reduce_only": True,
            "subaccount": self.subaccount,
            "exchange_index": -1,
        }
        return await self.request("POST", "/portfolio/events/orders", json_body=payload)

    async def ws_loop(self, on_message: Callable[[dict, Callable[[str], Awaitable[None]]], Awaitable[None]], initial_markets: Callable[[], list[str]], on_connected: Callable[[bool], Awaitable[None]]) -> None:
        if not self.configured:
            await on_connected(False)
            return
        backoff = 0.25
        while True:
            try:
                headers = self.headers("GET", "/trade-api/ws/v2")
                async with websockets.connect(self.ws_url, additional_headers=headers, ping_interval=15, ping_timeout=10, max_queue=4096, close_timeout=1) as ws:
                    await on_connected(True)
                    backoff = 0.25
                    next_id = 1
                    subscribed_markets: set[str] = set()
                    send_lock = asyncio.Lock()

                    async def subscribe_market(ticker: str) -> None:
                        nonlocal next_id
                        if not ticker or ticker in subscribed_markets:
                            return
                        async with send_lock:
                            if ticker in subscribed_markets:
                                return
                            await ws.send(json.dumps({"id": next_id, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_ticker": ticker}}))
                            next_id += 1
                            subscribed_markets.add(ticker)

                    async with send_lock:
                        await ws.send(json.dumps({"id": next_id, "cmd": "subscribe", "params": {"channels": ["market_positions", "fill"]}}))
                        next_id += 1
                    for ticker in initial_markets():
                        await subscribe_market(ticker)
                    async for raw in ws:
                        await on_message(json.loads(raw), subscribe_market)
            except asyncio.CancelledError:
                raise
            except Exception:
                await on_connected(False)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

    async def close(self):
        await self.http.aclose()
