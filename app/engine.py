from __future__ import annotations

import asyncio
import time
from collections import deque
from decimal import Decimal
from typing import Awaitable, Callable, Dict, Optional

from .kalshi import KalshiClient
from .store import EntryPlan, StopConfig, StopStore


class Book:
    def __init__(self):
        self.yes: Dict[Decimal, Decimal] = {}
        self.no: Dict[Decimal, Decimal] = {}
        self.ready = False
        self.ts_ms = 0

    @staticmethod
    def _parse_levels(levels) -> Dict[Decimal, Decimal]:
        out: Dict[Decimal, Decimal] = {}
        for level in levels or []:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                continue
            p, q = Decimal(str(level[0])), Decimal(str(level[1]))
            if q > 0:
                out[p] = q
        return out

    def snapshot(self, msg: dict):
        self.yes = self._parse_levels(msg.get("yes_dollars_fp") or msg.get("yes_dollars") or msg.get("yes"))
        self.no = self._parse_levels(msg.get("no_dollars_fp") or msg.get("no_dollars") or msg.get("no"))
        self.ready = True
        self.ts_ms = int(msg.get("ts_ms") or 0)

    def delta(self, msg: dict):
        side = msg.get("side")
        if side not in {"yes", "no"}:
            return
        raw_price = msg.get("price_dollars") or msg.get("price")
        if raw_price is None:
            return
        price = Decimal(str(raw_price))
        delta = Decimal(str(msg.get("delta_fp", msg.get("delta", "0"))))
        book = self.yes if side == "yes" else self.no
        new_qty = book.get(price, Decimal("0")) + delta
        if new_qty <= 0:
            book.pop(price, None)
        else:
            book[price] = new_qty
        self.ready = True
        self.ts_ms = int(msg.get("ts_ms") or 0)

    def best_bid(self, direction: str) -> Optional[Decimal]:
        book = self.yes if direction == "yes" else self.no
        return max(book) if book else None

    def entry_price(self, direction: str) -> Optional[Decimal]:
        opposite = self.no if direction == "yes" else self.yes
        return Decimal("1") - max(opposite) if opposite else None


class StopEngine:
    def __init__(self, kalshi: KalshiClient, store: StopStore, execution_enabled: bool):
        self.kalshi = kalshi
        self.store = store
        self.execution_enabled = execution_enabled
        self.positions: Dict[str, Decimal] = {}
        self.books: Dict[str, Book] = {}
        self.market_meta: Dict[str, dict] = {}
        self.cost_basis: Dict[str, Decimal] = {}
        self.ws_connected = False
        self.events = deque(maxlen=160)
        self._exit_locks: Dict[str, asyncio.Lock] = {}
        self._entry_locks: Dict[str, asyncio.Lock] = {}
        self._last_attempt: Dict[str, float] = {}
        self._subscribe_market: Optional[Callable[[str], Awaitable[None]]] = None

    def log(self, kind: str, message: str, ticker: str | None = None):
        self.events.appendleft({"ts": time.time(), "kind": kind, "message": message, "ticker": ticker})

    async def bootstrap(self):
        if not self.kalshi.configured:
            self.log("warning", "Kalshi credentials are not configured")
            return
        self.positions = await self.kalshi.get_positions()
        tickers = set(t for t, p in self.positions.items() if p != 0)
        tickers.update(p.ticker for p in self.store.entry_plans.values() if p.armed)
        for ticker in sorted(tickers):
            await self.ensure_market(ticker)
        for ticker, pos in list(self.positions.items()):
            if pos != 0:
                await self._load_cost_basis(ticker, pos)
        dirty = False
        for ticker, stop in self.store.stops.items():
            if stop.armed and self.positions.get(ticker, Decimal("0")) == 0:
                stop.armed = False
                stop.last_error = "Disarmed on startup: position is zero"
                dirty = True
        for plan in self.store.entry_plans.values():
            if plan.status == "waiting_exit" and plan.open_qty <= 0:
                plan.armed = False
                plan.status = "completed"
                dirty = True
        if dirty:
            await self.store.save()
        self.log("system", f"Loaded {sum(1 for p in self.positions.values() if p != 0)} open position(s)")

    async def ensure_market(self, ticker: str) -> dict:
        if ticker not in self.market_meta:
            try:
                self.market_meta[ticker] = await self.kalshi.get_market(ticker)
            except Exception as e:
                self.market_meta[ticker] = {"ticker": ticker, "title": ticker, "subtitle": "", "yes_label": "YES", "no_label": "NO", "status": ""}
                self.log("warning", f"Market name lookup failed: {str(e)[:120]}", ticker)
        return self.market_meta[ticker]

    async def lookup_market(self, ticker: str) -> dict:
        ticker = ticker.strip().upper()
        self.market_meta.pop(ticker, None)
        return await self.ensure_market(ticker)

    async def _load_cost_basis(self, ticker: str, actual_position: Decimal) -> None:
        try:
            fills = await self.kalshi.get_fills(ticker)
            q = Decimal("0")
            avg: Optional[Decimal] = None
            for f in fills:
                q, avg = self._basis_after_fill(q, avg, f)
            if avg is not None and q == actual_position:
                self.cost_basis[ticker] = avg
            else:
                self.cost_basis.pop(ticker, None)
        except Exception as e:
            self.cost_basis.pop(ticker, None)
            self.log("warning", f"Cost basis unavailable: {str(e)[:120]}", ticker)

    @staticmethod
    def _basis_after_fill(q: Decimal, avg: Optional[Decimal], fill: dict) -> tuple[Decimal, Optional[Decimal]]:
        side = (fill.get("side") or fill.get("outcome_side") or "").lower()
        action = (fill.get("action") or "").lower()
        count = Decimal(str(fill.get("count_fp") or fill.get("count") or "0"))
        if count <= 0 or side not in {"yes", "no"} or action not in {"buy", "sell"}:
            return q, avg
        yes_price = Decimal(str(fill.get("yes_price_dollars") or fill.get("yes_price") or "0"))
        no_price = Decimal(str(fill.get("no_price_dollars") or fill.get("no_price") or (Decimal("1") - yes_price)))
        if side == "yes":
            delta = count if action == "buy" else -count
            held_price = yes_price
        else:
            delta = -count if action == "buy" else count
            held_price = no_price
        if q == 0:
            return delta, held_price if delta != 0 else None
        if q * delta > 0:
            new_q = q + delta
            new_avg = ((avg or held_price) * abs(q) + held_price * abs(delta)) / abs(new_q)
            return new_q, new_avg
        new_q = q + delta
        if new_q == 0:
            return Decimal("0"), None
        if q * new_q > 0:
            return new_q, avg
        return new_q, held_price

    def markets_to_watch(self) -> list[str]:
        markets = {t for t, p in self.positions.items() if p != 0}
        markets.update(t for t, s in self.store.stops.items() if s.armed)
        markets.update(p.ticker for p in self.store.entry_plans.values() if p.armed)
        return sorted(markets)

    async def on_connected(self, connected: bool):
        self.ws_connected = connected
        self.log("connection", "Kalshi WebSocket connected" if connected else "Kalshi WebSocket disconnected")

    async def on_ws_message(self, data: dict, subscribe_market: Callable[[str], Awaitable[None]]):
        self._subscribe_market = subscribe_market
        typ = data.get("type")
        msg = data.get("msg") or {}
        if typ == "market_position":
            ticker = msg.get("market_ticker") or msg.get("ticker")
            if ticker:
                pos = Decimal(str(msg.get("position_fp", "0")))
                old = self.positions.get(ticker, Decimal("0"))
                self.positions[ticker] = pos
                if pos != 0:
                    await subscribe_market(ticker)
                    await self.ensure_market(ticker)
                await self._handle_position_change(ticker, old, pos)
                await self.evaluate(ticker)
        elif typ == "orderbook_snapshot":
            ticker = msg.get("market_ticker")
            if ticker:
                self.books.setdefault(ticker, Book()).snapshot(msg)
                await self.evaluate(ticker)
        elif typ == "orderbook_delta":
            ticker = msg.get("market_ticker")
            if ticker:
                self.books.setdefault(ticker, Book()).delta(msg)
                await self.evaluate(ticker)
        elif typ == "fill":
            ticker = msg.get("market_ticker") or msg.get("ticker")
            if ticker:
                q = self.positions.get(ticker, Decimal("0"))
                self.log("fill", f"Fill: {msg.get('action')} {msg.get('count_fp')} {msg.get('side')}", ticker)
                await subscribe_market(ticker)
                if q != 0:
                    asyncio.create_task(self._refresh_basis_later(ticker))
        elif typ == "error":
            self.log("error", f"WebSocket error: {msg}")

    async def _refresh_basis_later(self, ticker: str):
        await asyncio.sleep(0.4)
        pos = self.positions.get(ticker, Decimal("0"))
        if pos != 0:
            await self._load_cost_basis(ticker, pos)

    async def _handle_position_change(self, ticker: str, old: Decimal, new: Decimal):
        if new == 0:
            self.cost_basis.pop(ticker, None)
        stop = self.store.stops.get(ticker)
        if not stop or not stop.armed:
            return
        if new == 0:
            stop.armed = False
            stop.fired = False
            stop.last_error = None
            await self.store.save()
            self.log("stop", "Position closed; exit trigger disarmed", ticker)
            return
        direction = "yes" if new > 0 else "no"
        if stop.direction and direction != stop.direction:
            stop.armed = False
            stop.last_error = "Position direction changed; trigger disarmed"
            await self.store.save()
            self.log("warning", stop.last_error, ticker)

    def held_bid(self, ticker: str, direction: Optional[str] = None) -> Optional[Decimal]:
        if direction is None:
            pos = self.positions.get(ticker, Decimal("0"))
            if pos == 0:
                return None
            direction = "yes" if pos > 0 else "no"
        book = self.books.get(ticker)
        return book.best_bid(direction) if book and book.ready else None

    def entry_price(self, ticker: str, direction: str) -> Optional[Decimal]:
        book = self.books.get(ticker)
        return book.entry_price(direction) if book and book.ready else None

    def position_pnl(self, ticker: str) -> tuple[Optional[Decimal], Optional[Decimal]]:
        pos = self.positions.get(ticker, Decimal("0"))
        avg = self.cost_basis.get(ticker)
        held = self.held_bid(ticker)
        if pos == 0 or avg is None or held is None:
            return None, None
        pnl = (held - avg) * abs(pos)
        pct = ((held - avg) / avg * Decimal("100")) if avg > 0 else None
        return pnl, pct

    @staticmethod
    def _condition(value: Optional[Decimal], operator: str, target: Decimal) -> bool:
        if value is None:
            return False
        return value <= target if operator == "lte" else value >= target

    def _manual_metric(self, ticker: str, stop: StopConfig) -> Optional[Decimal]:
        if stop.trigger_mode == "price":
            return self.held_bid(ticker)
        pnl, pct = self.position_pnl(ticker)
        return pnl if stop.trigger_mode == "pnl_dollars" else pct

    async def set_stop(self, ticker: str, trigger: Decimal, slippage: Decimal, trigger_mode: str, operator: str, armed: bool):
        if trigger_mode not in {"price", "pnl_dollars", "pnl_percent"}:
            raise ValueError("Unknown trigger mode")
        if operator not in {"lte", "gte"}:
            raise ValueError("Operator must be <= or >=")
        if trigger_mode == "price" and not (Decimal("0.0001") <= trigger <= Decimal("0.9999")):
            raise ValueError("Price trigger must be between 0.01¢ and 99.99¢")
        if not (Decimal("0") <= slippage <= Decimal("0.25")):
            raise ValueError("Slippage must be between 0¢ and 25¢")
        pos = self.positions.get(ticker, Decimal("0"))
        direction = None
        if armed:
            if pos == 0:
                raise ValueError("Cannot arm without an open position")
            if not self.ws_connected:
                raise ValueError("Cannot arm while Kalshi WebSocket is disconnected")
            direction = "yes" if pos > 0 else "no"
            if trigger_mode != "price" and self.cost_basis.get(ticker) is None:
                await self._load_cost_basis(ticker, pos)
                if self.cost_basis.get(ticker) is None:
                    raise ValueError("P/L cost basis could not be reconstructed for this position")
        stop = self.store.stops.get(ticker) or StopConfig(ticker, trigger, slippage)
        stop.trigger, stop.slippage = trigger, slippage
        stop.trigger_mode, stop.operator = trigger_mode, operator
        stop.armed = armed
        stop.direction = direction if armed else stop.direction
        stop.fired = False
        stop.last_error = None
        await self.store.upsert(stop)
        self.log("stop", f"Exit trigger {'armed' if armed else 'disarmed'}: {trigger_mode} {operator} {trigger}", ticker)
        if armed:
            await self.evaluate(ticker)
        return stop

    async def create_entry_plan(self, ticker: str, direction: str, quantity: Decimal, entry_operator: str,
                                entry_trigger: Decimal, entry_slippage: Decimal, exit_mode: str,
                                exit_operator: str, exit_trigger: Decimal, exit_slippage: Decimal) -> EntryPlan:
        ticker = ticker.strip().upper()
        if direction not in {"yes", "no"}:
            raise ValueError("Entry side must be YES or NO")
        if quantity <= 0:
            raise ValueError("Quantity must be positive")
        if entry_operator not in {"lte", "gte"} or exit_operator not in {"lte", "gte"}:
            raise ValueError("Condition must be <= or >=")
        if not (Decimal("0.0001") <= entry_trigger <= Decimal("0.9999")):
            raise ValueError("Entry price must be between 0.01¢ and 99.99¢")
        if exit_mode not in {"price", "pnl_dollars", "pnl_percent"}:
            raise ValueError("Unknown exit mode")
        if exit_mode == "price" and not (Decimal("0.0001") <= exit_trigger <= Decimal("0.9999")):
            raise ValueError("Exit price must be between 0.01¢ and 99.99¢")
        if not (Decimal("0") <= entry_slippage <= Decimal("0.25") and Decimal("0") <= exit_slippage <= Decimal("0.25")):
            raise ValueError("Slippage must be between 0¢ and 25¢")
        if not self.ws_connected:
            raise ValueError("Cannot arm an entry while Kalshi WebSocket is disconnected")
        await self.lookup_market(ticker)
        plan = EntryPlan.new(
            ticker=ticker, direction=direction, quantity=quantity, entry_operator=entry_operator,
            entry_trigger=entry_trigger, entry_slippage=entry_slippage, exit_mode=exit_mode,
            exit_operator=exit_operator, exit_trigger=exit_trigger, exit_slippage=exit_slippage,
        )
        await self.store.upsert_plan(plan)
        if self._subscribe_market:
            await self._subscribe_market(ticker)
        self.log("entry", f"Entry armed: {direction.upper()} {quantity} @ {entry_operator} {entry_trigger * 100:.2f}¢", ticker)
        await self.evaluate(ticker)
        return plan

    async def cancel_entry_plan(self, plan_id: str):
        plan = self.store.entry_plans.get(plan_id)
        if not plan:
            raise ValueError("Entry plan not found")
        if plan.status == "waiting_exit" and plan.open_qty > 0:
            plan.armed = False
            plan.status = "canceled"
            plan.last_error = "Linked exit canceled; position remains open"
            await self.store.save()
            return
        await self.store.delete_plan(plan_id)

    async def evaluate(self, ticker: str):
        stop = self.store.stops.get(ticker)
        if stop and stop.armed and not stop.fired:
            pos = self.positions.get(ticker, Decimal("0"))
            if pos != 0 and (not stop.direction or stop.direction == ("yes" if pos > 0 else "no")):
                metric = self._manual_metric(ticker, stop)
                if self._condition(metric, stop.operator, stop.trigger):
                    asyncio.create_task(self._fire_manual_exit(ticker))
        for plan in list(self.store.entry_plans.values()):
            if plan.ticker != ticker or not plan.armed:
                continue
            if plan.status == "waiting_entry":
                metric = self.entry_price(ticker, plan.direction)
                if self._condition(metric, plan.entry_operator, plan.entry_trigger):
                    asyncio.create_task(self._fire_entry(plan.plan_id))
            elif plan.status == "waiting_exit" and plan.open_qty > 0:
                metric = self._linked_exit_metric(plan)
                if self._condition(metric, plan.exit_operator, plan.exit_trigger):
                    asyncio.create_task(self._fire_linked_exit(plan.plan_id))

    def _linked_exit_metric(self, plan: EntryPlan) -> Optional[Decimal]:
        held = self.held_bid(plan.ticker, plan.direction)
        if held is None:
            return None
        if plan.exit_mode == "price":
            return held
        if plan.entry_price is None or plan.entry_price <= 0:
            return None
        pnl = (held - plan.entry_price) * plan.open_qty
        if plan.exit_mode == "pnl_dollars":
            return pnl
        return (held - plan.entry_price) / plan.entry_price * Decimal("100")

    async def _fire_manual_exit(self, ticker: str):
        lock = self._exit_locks.setdefault("manual:" + ticker, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            stop = self.store.stops.get(ticker)
            if not stop or not stop.armed or stop.fired:
                return
            pos = self.positions.get(ticker, Decimal("0"))
            held = self.held_bid(ticker)
            metric = self._manual_metric(ticker, stop)
            if pos == 0 or held is None or not self._condition(metric, stop.operator, stop.trigger):
                return
            if time.monotonic() - self._last_attempt.get("manual:" + ticker, 0) < 0.25:
                return
            self._last_attempt["manual:" + ticker] = time.monotonic()
            floor = max(Decimal("0.0001"), (stop.trigger if stop.trigger_mode == "price" else held) - stop.slippage)
            self.log("trigger", f"Exit triggered at {held * 100:.2f}¢; IOC floor {floor * 100:.2f}¢", ticker)
            if not self.execution_enabled:
                stop.last_error = "Simulation only: EXECUTION_ENABLED=false"
                await self.store.save()
                return
            try:
                result = await self.kalshi.create_reduce_ioc(ticker, pos, floor)
                fill = Decimal(str(result.get("fill_count", "0")))
                remaining = Decimal(str(result.get("remaining_count", "0")))
                self.log("order", f"Exit IOC: filled {fill:.2f}, unfilled/canceled {remaining:.2f}", ticker)
                if fill >= abs(pos):
                    stop.fired = True
                    stop.armed = False
                await self.store.save()
            except Exception as e:
                stop.last_error = str(e)[:300]
                await self.store.save()
                self.log("error", f"Exit failed: {stop.last_error}", ticker)

    async def _fire_entry(self, plan_id: str):
        lock = self._entry_locks.setdefault(plan_id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            plan = self.store.entry_plans.get(plan_id)
            if not plan or not plan.armed or plan.status != "waiting_entry":
                return
            live = self.entry_price(plan.ticker, plan.direction)
            if not self._condition(live, plan.entry_operator, plan.entry_trigger):
                return
            key = "entry:" + plan_id
            if time.monotonic() - self._last_attempt.get(key, 0) < 0.75:
                return
            self._last_attempt[key] = time.monotonic()
            if live is None:
                return
            max_price = min(Decimal("0.9999"), live + plan.entry_slippage)
            self.log("trigger", f"Entry triggered at {live * 100:.2f}¢; IOC cap {max_price * 100:.2f}¢", plan.ticker)
            if not self.execution_enabled:
                plan.last_error = "Simulation only: entry not submitted"
                await self.store.save()
                return
            try:
                result = await self.kalshi.create_entry_ioc(plan.ticker, plan.direction, plan.quantity, max_price)
                fill = Decimal(str(result.get("fill_count", "0")))
                if fill <= 0:
                    plan.last_error = "Entry IOC reached Kalshi but received no fill; still armed"
                    await self.store.save()
                    return
                yes_avg = Decimal(str(result.get("average_fill_price", max_price)))
                held_avg = yes_avg if plan.direction == "yes" else Decimal("1") - yes_avg
                plan.filled_qty = fill
                plan.open_qty = fill
                plan.entry_price = held_avg
                plan.status = "waiting_exit"
                plan.last_error = None
                await self.store.save()
                self.log("order", f"ENTRY FILLED {fill:.2f} {plan.direction.upper()} @ {held_avg * 100:.2f}¢; linked exit armed", plan.ticker)
                await self.evaluate(plan.ticker)
            except Exception as e:
                plan.last_error = str(e)[:300]
                await self.store.save()
                self.log("error", f"Entry failed: {plan.last_error}", plan.ticker)

    async def _fire_linked_exit(self, plan_id: str):
        lock = self._exit_locks.setdefault("plan:" + plan_id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            plan = self.store.entry_plans.get(plan_id)
            if not plan or not plan.armed or plan.status != "waiting_exit" or plan.open_qty <= 0:
                return
            metric = self._linked_exit_metric(plan)
            if not self._condition(metric, plan.exit_operator, plan.exit_trigger):
                return
            held = self.held_bid(plan.ticker, plan.direction)
            if held is None:
                return
            account_pos = self.positions.get(plan.ticker, Decimal("0"))
            same_dir_qty = max(Decimal("0"), account_pos if plan.direction == "yes" else -account_pos)
            qty = min(plan.open_qty, same_dir_qty)
            if qty <= 0:
                plan.armed = False
                plan.status = "completed"
                plan.last_error = "Linked quantity is no longer present in account position"
                await self.store.save()
                return
            key = "exit:" + plan_id
            if time.monotonic() - self._last_attempt.get(key, 0) < 0.25:
                return
            self._last_attempt[key] = time.monotonic()
            floor = max(Decimal("0.0001"), (plan.exit_trigger if plan.exit_mode == "price" else held) - plan.exit_slippage)
            self.log("trigger", f"Linked exit triggered at {held * 100:.2f}¢ for {qty:.2f}; floor {floor * 100:.2f}¢", plan.ticker)
            if not self.execution_enabled:
                plan.last_error = "Simulation only: linked exit not submitted"
                await self.store.save()
                return
            try:
                result = await self.kalshi.create_reduce_ioc_count(plan.ticker, plan.direction, qty, floor)
                fill = Decimal(str(result.get("fill_count", "0")))
                plan.open_qty = max(Decimal("0"), plan.open_qty - fill)
                if plan.open_qty <= 0:
                    plan.armed = False
                    plan.status = "completed"
                await self.store.save()
                self.log("order", f"LINKED EXIT filled {fill:.2f}; linked qty remaining {plan.open_qty:.2f}", plan.ticker)
            except Exception as e:
                plan.last_error = str(e)[:300]
                await self.store.save()
                self.log("error", f"Linked exit failed: {plan.last_error}", plan.ticker)

    def state(self) -> dict:
        rows = []
        tickers = sorted(set(self.positions) | set(self.store.stops))
        for ticker in tickers:
            pos = self.positions.get(ticker, Decimal("0"))
            stop = self.store.stops.get(ticker)
            if pos == 0 and not stop:
                continue
            direction = "yes" if pos > 0 else "no" if pos < 0 else None
            meta = self.market_meta.get(ticker, {"title": ticker, "subtitle": "", "yes_label": "YES", "no_label": "NO"})
            held = self.held_bid(ticker)
            pnl, pct = self.position_pnl(ticker)
            rows.append({
                "ticker": ticker, "market": meta, "position": str(pos),
                "direction": direction.upper() if direction else "FLAT", "quantity": str(abs(pos)),
                "held_bid": str(held) if held is not None else None,
                "avg_entry": str(self.cost_basis[ticker]) if ticker in self.cost_basis else None,
                "pnl_dollars": str(pnl) if pnl is not None else None,
                "pnl_percent": str(pct) if pct is not None else None,
                "stop": stop.json_dict() if stop else None,
                "book_ready": bool(self.books.get(ticker) and self.books[ticker].ready),
            })
        plans = []
        for plan in sorted(self.store.entry_plans.values(), key=lambda p: p.created_at, reverse=True):
            meta = self.market_meta.get(plan.ticker, {"title": plan.ticker, "subtitle": "", "yes_label": "YES", "no_label": "NO"})
            live_entry = self.entry_price(plan.ticker, plan.direction)
            held = self.held_bid(plan.ticker, plan.direction)
            exit_metric = self._linked_exit_metric(plan) if plan.status == "waiting_exit" else None
            d = plan.json_dict()
            d.update({"market": meta, "live_entry_price": str(live_entry) if live_entry is not None else None,
                      "held_bid": str(held) if held is not None else None,
                      "exit_metric": str(exit_metric) if exit_metric is not None else None})
            plans.append(d)
        return {
            "ws_connected": self.ws_connected, "execution_enabled": self.execution_enabled,
            "kalshi_configured": self.kalshi.configured, "environment": self.kalshi.env,
            "positions": rows, "entry_plans": plans, "events": list(self.events)[:60],
        }
