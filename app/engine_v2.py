from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Optional

from .engine import StopEngine as BaseStopEngine
from .store import EntryPlan


class StopEngine(BaseStopEngine):
    """Extension layer for entry triggers that can use price or portfolio P/L.

    EntryPlan predates entry_mode in persistence. To remain backwards compatible
    without migrating live plan files, non-price entry modes are encoded in the
    persisted entry_operator as '<mode>:<operator>'. Existing plans with lte/gte
    remain price-based.
    """

    ENTRY_MODES = {"price", "pnl_dollars", "pnl_percent"}

    @staticmethod
    def _decode_entry_condition(plan: EntryPlan) -> tuple[str, str]:
        raw = plan.entry_operator or "lte"
        if ":" in raw:
            mode, operator = raw.split(":", 1)
            if mode in StopEngine.ENTRY_MODES and operator in {"lte", "gte"}:
                return mode, operator
        return "price", raw if raw in {"lte", "gte"} else "lte"

    @staticmethod
    def _encode_entry_condition(mode: str, operator: str) -> str:
        return operator if mode == "price" else f"{mode}:{operator}"

    def _entry_metric(self, plan: EntryPlan) -> Optional[Decimal]:
        mode, _ = self._decode_entry_condition(plan)
        if mode == "price":
            return self.entry_price(plan.ticker, plan.direction)
        pnl, pct = self.position_pnl(plan.ticker)
        return pnl if mode == "pnl_dollars" else pct

    async def create_entry_plan(
        self, ticker: str, direction: str, quantity: Decimal, entry_mode: str,
        entry_operator: str, entry_trigger: Decimal, entry_slippage: Decimal,
        exit_mode: str, exit_operator: str, exit_trigger: Decimal,
        exit_slippage: Decimal,
    ) -> EntryPlan:
        ticker = ticker.strip().upper()
        if direction not in {"yes", "no"}:
            raise ValueError("Entry side must be YES or NO")
        if quantity <= 0:
            raise ValueError("Quantity must be positive")
        if entry_mode not in self.ENTRY_MODES:
            raise ValueError("Unknown entry trigger mode")
        if entry_operator not in {"lte", "gte"} or exit_operator not in {"lte", "gte"}:
            raise ValueError("Condition must be <= or >=")
        if entry_mode == "price" and not (Decimal("0.0001") <= entry_trigger <= Decimal("0.9999")):
            raise ValueError("Entry price must be between 0.01¢ and 99.99¢")
        if exit_mode not in {"price", "pnl_dollars", "pnl_percent"}:
            raise ValueError("Unknown exit mode")
        if exit_mode == "price" and not (Decimal("0.0001") <= exit_trigger <= Decimal("0.9999")):
            raise ValueError("Exit price must be between 0.01¢ and 99.99¢")
        if not (
            Decimal("0") <= entry_slippage <= Decimal("0.25")
            and Decimal("0") <= exit_slippage <= Decimal("0.25")
        ):
            raise ValueError("Slippage must be between 0¢ and 25¢")
        if not self.ws_connected:
            raise ValueError("Cannot arm an entry while Kalshi WebSocket is disconnected")

        pos = self.positions.get(ticker, Decimal("0"))
        if pos != 0 and ("yes" if pos > 0 else "no") != direction:
            raise ValueError("Cannot arm an entry opposite the existing position in this market")
        if entry_mode != "price":
            if pos == 0:
                raise ValueError("Entry-by-P/L requires an existing position in this market")
            if self.cost_basis.get(ticker) is None:
                await self._load_cost_basis(ticker, pos)
            if self.cost_basis.get(ticker) is None:
                raise ValueError("P/L cost basis could not be reconstructed for this position")

        await self.lookup_market(ticker)
        encoded_operator = self._encode_entry_condition(entry_mode, entry_operator)
        plan = EntryPlan.new(
            ticker=ticker,
            direction=direction,
            quantity=quantity,
            entry_operator=encoded_operator,
            entry_trigger=entry_trigger,
            entry_slippage=entry_slippage,
            exit_mode=exit_mode,
            exit_operator=exit_operator,
            exit_trigger=exit_trigger,
            exit_slippage=exit_slippage,
        )
        await self.store.upsert_plan(plan)
        if self._subscribe_market:
            await self._subscribe_market(ticker)
        metric_name = "price" if entry_mode == "price" else ("P/L $" if entry_mode == "pnl_dollars" else "P/L %")
        self.log("entry", f"Entry armed: {direction.upper()} {quantity} on {metric_name} {entry_operator} {entry_trigger}", ticker)
        await self.evaluate(ticker)
        return plan

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
                _, operator = self._decode_entry_condition(plan)
                metric = self._entry_metric(plan)
                if self._condition(metric, operator, plan.entry_trigger):
                    asyncio.create_task(self._fire_entry(plan.plan_id))
            elif plan.status == "waiting_exit" and plan.open_qty > 0:
                metric = self._linked_exit_metric(plan)
                if self._condition(metric, plan.exit_operator, plan.exit_trigger):
                    asyncio.create_task(self._fire_linked_exit(plan.plan_id))

    async def _fire_entry(self, plan_id: str):
        lock = self._entry_locks.setdefault(plan_id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            plan = self.store.entry_plans.get(plan_id)
            if not plan or not plan.armed or plan.status != "waiting_entry":
                return
            mode, operator = self._decode_entry_condition(plan)
            metric = self._entry_metric(plan)
            if not self._condition(metric, operator, plan.entry_trigger):
                return
            key = "entry:" + plan_id
            if time.monotonic() - self._last_attempt.get(key, 0) < 0.75:
                return
            self._last_attempt[key] = time.monotonic()

            live = self.entry_price(plan.ticker, plan.direction)
            if live is None:
                return
            pos = self.positions.get(plan.ticker, Decimal("0"))
            if pos != 0 and ("yes" if pos > 0 else "no") != plan.direction:
                plan.last_error = "Entry blocked because account position is now opposite"
                await self.store.save()
                return

            max_price = min(Decimal("0.9999"), live + plan.entry_slippage)
            self.log("trigger", f"Entry triggered at executable {live * 100:.2f}¢; IOC cap {max_price * 100:.2f}¢", plan.ticker)
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

    def state(self) -> dict:
        state = super().state()
        for row in state.get("entry_plans", []):
            raw = row.get("entry_operator") or "lte"
            if ":" in raw:
                mode, operator = raw.split(":", 1)
                if mode not in self.ENTRY_MODES:
                    mode, operator = "price", "lte"
            else:
                mode, operator = "price", raw
            row["entry_mode"] = mode
            row["entry_operator"] = operator
            plan = self.store.entry_plans.get(row.get("plan_id"))
            if plan and plan.status == "waiting_entry":
                metric = self._entry_metric(plan)
                row["entry_metric"] = str(metric) if metric is not None else None
            else:
                row["entry_metric"] = None
        return state
