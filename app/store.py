from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional


@dataclass
class StopConfig:
    ticker: str
    trigger: Decimal
    slippage: Decimal
    trigger_mode: str = "price"
    operator: str = "lte"
    armed: bool = False
    direction: Optional[str] = None
    fired: bool = False
    last_error: Optional[str] = None

    def json_dict(self) -> dict:
        d = asdict(self)
        d["trigger"] = str(self.trigger)
        d["slippage"] = str(self.slippage)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "StopConfig":
        return cls(
            ticker=d["ticker"],
            trigger=Decimal(str(d.get("trigger", "0.50"))),
            slippage=Decimal(str(d.get("slippage", "0.03"))),
            trigger_mode=d.get("trigger_mode", "price"),
            operator=d.get("operator", "lte"),
            armed=bool(d.get("armed", False)),
            direction=d.get("direction"),
            fired=bool(d.get("fired", False)),
            last_error=d.get("last_error"),
        )


@dataclass
class EntryPlan:
    plan_id: str
    ticker: str
    direction: str
    quantity: Decimal
    entry_mode: str
    entry_operator: str
    entry_trigger: Decimal
    entry_slippage: Decimal
    exit_mode: str
    exit_operator: str
    exit_trigger: Decimal
    exit_slippage: Decimal
    armed: bool = True
    status: str = "waiting_entry"
    filled_qty: Decimal = Decimal("0")
    open_qty: Decimal = Decimal("0")
    entry_price: Optional[Decimal] = None
    last_error: Optional[str] = None
    created_at: float = 0.0

    @classmethod
    def new(cls, **kwargs) -> "EntryPlan":
        return cls(plan_id=str(uuid.uuid4()), created_at=time.time(), **kwargs)

    def json_dict(self) -> dict:
        d = asdict(self)
        for key in (
            "quantity", "entry_trigger", "entry_slippage", "exit_trigger",
            "exit_slippage", "filled_qty", "open_qty", "entry_price"
        ):
            value = d.get(key)
            d[key] = None if value is None else str(value)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "EntryPlan":
        return cls(
            plan_id=d["plan_id"],
            ticker=d["ticker"],
            direction=d["direction"],
            quantity=Decimal(str(d["quantity"])),
            entry_mode=d.get("entry_mode", "price"),
            entry_operator=d.get("entry_operator", "lte"),
            entry_trigger=Decimal(str(d["entry_trigger"])),
            entry_slippage=Decimal(str(d.get("entry_slippage", "0.03"))),
            exit_mode=d.get("exit_mode", "price"),
            exit_operator=d.get("exit_operator", "gte"),
            exit_trigger=Decimal(str(d["exit_trigger"])),
            exit_slippage=Decimal(str(d.get("exit_slippage", "0.03"))),
            armed=bool(d.get("armed", True)),
            status=d.get("status", "waiting_entry"),
            filled_qty=Decimal(str(d.get("filled_qty", "0"))),
            open_qty=Decimal(str(d.get("open_qty", "0"))),
            entry_price=Decimal(str(d["entry_price"])) if d.get("entry_price") is not None else None,
            last_error=d.get("last_error"),
            created_at=float(d.get("created_at", 0) or 0),
        )


class StopStore:
    def __init__(self, data_dir: str):
        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        self.stop_path = root / "stops.json"
        self.plan_path = root / "entry_plans.json"
        self._lock = asyncio.Lock()
        self.stops: Dict[str, StopConfig] = {}
        self.entry_plans: Dict[str, EntryPlan] = {}

    async def load(self) -> None:
        async with self._lock:
            self.stops = self._load_map(self.stop_path, StopConfig.from_dict)
            self.entry_plans = self._load_map(self.plan_path, EntryPlan.from_dict)

    @staticmethod
    def _load_map(path: Path, parser):
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text())
            return {key: parser(value) for key, value in raw.items()}
        except Exception:
            return {}

    async def save(self) -> None:
        async with self._lock:
            self._atomic_write(self.stop_path, {k: v.json_dict() for k, v in self.stops.items()})
            self._atomic_write(self.plan_path, {k: v.json_dict() for k, v in self.entry_plans.items()})

    @staticmethod
    def _atomic_write(path: Path, payload: dict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(path)

    async def upsert(self, stop: StopConfig) -> None:
        self.stops[stop.ticker] = stop
        await self.save()

    async def upsert_plan(self, plan: EntryPlan) -> None:
        self.entry_plans[plan.plan_id] = plan
        await self.save()

    async def delete_plan(self, plan_id: str) -> None:
        self.entry_plans.pop(plan_id, None)
        await self.save()
