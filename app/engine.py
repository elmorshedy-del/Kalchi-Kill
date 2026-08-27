from __future__ import annotations

import asyncio
import time
from collections import deque
from decimal import Decimal
from typing import Awaitable, Callable, Dict

from .kalshi import KalshiClient
from .store import StopConfig, StopStore


class Book:
    def __init__(self):
        self.yes: Dict[Decimal, Decimal] = {}
        self.no: Dict[Decimal, Decimal] = {}
        self.ready = False
        self.ts_ms = 0

    @staticmethod
    def _parse_levels(levels) -> Dict[Decimal, Decimal]:
        out = {}
        for price, qty in levels or []:
            p, q = Decimal(str(price)), Decimal(str(qty))
            if q > 0:
                out[p] = q
        return out

    def snapshot(self, msg: dict):
        self.yes = self._parse_levels(msg.get("yes_dollars_fp"))
        self.no = self._parse_levels(msg.get("no_dollars_fp"))
        self.ready = True

    def delta(self, msg: dict):
        side = msg.get("side")
        if side not in {"yes", "no"}:
            return
        price = Decimal(str(msg.get("price_dollars")))
        delta = Decimal(str(msg.get("delta_fp", "0")))
        book = self.yes if side == "yes" else self.no
        new_qty = book.get(price, Decimal("0")) + delta
        if new_qty <= 0:
            book.pop(price, None)
        else:
            book[price] = new_qty
        self.ts_ms = int(msg.get("ts_ms") or 0)

    def best_bid(self, direction: str):
        book = self.yes if direction == "yes" else self.no
        return max(book) if book else None


class StopEngine:
    def __init__(self, kalshi: KalshiClient, store: StopStore, execution_enabled: bool):
        self.kalshi = kalshi
        self.store = store
        self.execution_enabled = execution_enabled
        self.positions: Dict[str, Decimal] = {}
        self.books: Dict[str, Book] = {}
        self.ws_connected = False
        self.events = deque(maxlen=100)
        self._exit_locks: Dict[str, asyncio.Lock] = {}
        self._last_attempt: Dict[str, float] = {}

    def log(self, kind: str, message: str, ticker: str | None = None):
        self.events.appendleft({"ts": time.time(), "kind": kind, "message": message, "ticker": ticker})

    async def bootstrap(self):
        if not self.kalshi.configured:
            self.log("warning", "Kalshi credentials are not configured")
            return
        self.positions = await self.kalshi.get_positions()
        dirty = False
        for ticker, stop in self.store.stops.items():
            if stop.armed and self.positions.get(ticker, Decimal("0")) == 0:
                stop.armed = False
                stop.last_error = "Disarmed on startup: position is zero"
                dirty = True
        if dirty:
            await self.store.save()
        self.log("system", f"Loaded {sum(1 for p in self.positions.values() if p != 0)} open position(s)")

    def markets_to_watch(self) -> list[str]:
        markets = {t for t, p in self.positions.items() if p != 0}
        markets.update(t for t, s in self.store.stops.items() if s.armed)
        return sorted(markets)

    async def on_connected(self, connected: bool):
        self.ws_connected = connected
        self.log("connection", "Kalshi WebSocket connected" if connected else "Kalshi WebSocket disconnected")

    async def on_ws_message(self, data: dict, subscribe_market: Callable[[str], Awaitable[None]]):
        typ = data.get("type")
        msg = data.get("msg") or {}

        if typ == "market_position":
            ticker = msg.get("market_ticker")
            if ticker:
                pos = Decimal(str(msg.get("position_fp", "0")))
                old = self.positions.get(ticker, Decimal("0"))
                self.positions[ticker] = pos
                if pos != 0:
                    await subscribe_market(ticker)
                await self._handle_position_change(ticker, old, pos)
                await self.evaluate(ticker)
        elif typ == "orderbook_snapshot":
            ticker = msg.get("market_ticker")
            if ticker:
                book = self.books.setdefault(ticker, Book())
                book.snapshot(msg)
                await self.evaluate(ticker)
        elif typ == "orderbook_delta":
            ticker = msg.get("market_ticker")
            if ticker:
                book = self.books.setdefault(ticker, Book())
                book.delta(msg)
                await self.evaluate(ticker)
        elif typ == "fill":
            ticker = msg.get("market_ticker")
            if ticker:
                self.log("fill", f"Fill: {msg.get('action')} {msg.get('count_fp')} {msg.get('side')}", ticker)
                await subscribe_market(ticker)
        elif typ == "error":
            self.log("error", f"WebSocket error: {msg}")

    async def _handle_position_change(self, ticker: str, old: Decimal, new: Decimal):
        stop = self.store.stops.get(ticker)
        if not stop or not stop.armed:
            return
        if new == 0:
            stop.armed = False
            stop.fired = False
            stop.last_error = None
            await self.store.save()
            self.log("stop", "Position closed; stop disarmed", ticker)
            return
        direction = "yes" if new > 0 else "no"
        if stop.direction and direction != stop.direction:
            stop.armed = False
            stop.last_error = "Position direction changed; stop disarmed"
            await self.store.save()
            self.log("warning", stop.last_error, ticker)

    def held_bid(self, ticker: str):
        pos = self.positions.get(ticker, Decimal("0"))
        if pos == 0:
            return None
        book = self.books.get(ticker)
        if not book or not book.ready:
            return None
        return book.best_bid("yes" if pos > 0 else "no")

    async def set_stop(self, ticker: str, trigger: Decimal, slippage: Decimal, armed: bool):
        if not (Decimal("0.0001") <= trigger <= Decimal("0.9999")):
            raise ValueError("Trigger must be between 0.01¢ and 99.99¢")
        if not (Decimal("0") <= slippage <= Decimal("0.25")):
            raise ValueError("Slippage must be between 0¢ and 25¢")
        pos = self.positions.get(ticker, Decimal("0"))
        direction = None
        if armed:
            if pos == 0:
                raise ValueError("Cannot arm a stop without an open position")
            if not self.ws_connected:
                raise ValueError("Cannot arm while Kalshi WebSocket is disconnected")
            direction = "yes" if pos > 0 else "no"
        stop = self.store.stops.get(ticker) or StopConfig(ticker, trigger, slippage)
        stop.trigger = trigger
        stop.slippage = slippage
        stop.armed = armed
        stop.direction = direction if armed else stop.direction
        stop.fired = False
        stop.last_error = None
        await self.store.upsert(stop)
        self.log("stop", f"Stop {'armed' if armed else 'disarmed'} at {trigger * 100:.2f}¢", ticker)
        if armed:
            await self.evaluate(ticker)
        return stop

    async def evaluate(self, ticker: str):
        stop = self.store.stops.get(ticker)
        if not stop or not stop.armed or stop.fired:
            return
        pos = self.positions.get(ticker, Decimal("0"))
        if pos == 0:
            return
        direction = "yes" if pos > 0 else "no"
        if stop.direction and direction != stop.direction:
            return
        held = self.held_bid(ticker)
        if held is not None and held <= stop.trigger:
            asyncio.create_task(self._fire(ticker))

    async def _fire(self, ticker: str):
        lock = self._exit_locks.setdefault(ticker, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            stop = self.store.stops.get(ticker)
            if not stop or not stop.armed or stop.fired:
                return
            now = time.monotonic()
            if now - self._last_attempt.get(ticker, 0) < 0.20:
                return
            self._last_attempt[ticker] = now
            pos = self.positions.get(ticker, Decimal("0"))
            held = self.held_bid(ticker)
            if pos == 0 or held is None or held > stop.trigger:
                return
            floor = max(Decimal("0.0001"), stop.trigger - stop.slippage)
            self.log("trigger", f"Triggered at {held * 100:.2f}¢; IOC floor {floor * 100:.2f}¢", ticker)
            if not self.execution_enabled:
                stop.fired = True
                stop.armed = False
                stop.last_error = "Simulation only: EXECUTION_ENABLED=false"
                await self.store.save()
                self.log("simulation", "Would submit reduce-only IOC exit", ticker)
                return
            try:
                result = await self.kalshi.create_reduce_ioc(ticker, pos, floor)
                fill = Decimal(str(result.get("fill_count", "0")))
                remaining = Decimal(str(result.get("remaining_count", "0")))
                self.log("order", f"Exit IOC accepted: filled {fill:.2f}, unfilled/canceled {remaining:.2f}", ticker)
                if remaining == 0 and fill >= abs(pos):
                    stop.fired = True
                await self.store.save()
            except Exception as e:
                stop.last_error = str(e)[:300]
                await self.store.save()
                self.log("error", f"Exit failed: {stop.last_error}", ticker)

    def state(self) -> dict:
        rows = []
        tickers = sorted(set(self.positions) | set(self.store.stops))
        for ticker in tickers:
            pos = self.positions.get(ticker, Decimal("0"))
            if pos == 0 and ticker not in self.store.stops:
                continue
            stop = self.store.stops.get(ticker)
            held = self.held_bid(ticker)
            rows.append({
                "ticker": ticker,
                "position": str(pos),
                "direction": "YES" if pos > 0 else "NO" if pos < 0 else "FLAT",
                "quantity": str(abs(pos)),
                "held_bid": str(held) if held is not None else None,
                "stop": stop.json_dict() if stop else None,
                "book_ready": bool(self.books.get(ticker) and self.books[ticker].ready),
            })
        return {
            "ws_connected": self.ws_connected,
            "execution_enabled": self.execution_enabled,
            "kalshi_configured": self.kalshi.configured,
            "environment": self.kalshi.env,
            "positions": rows,
            "events": list(self.events)[:40],
        }
