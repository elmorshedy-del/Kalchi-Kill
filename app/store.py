from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional


@dataclass
class StopConfig:
    ticker: str
    trigger: Decimal
    slippage: Decimal
    armed: bool = False
    direction: Optional[str] = None  # yes | no, captured when armed
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
            trigger=Decimal(str(d["trigger"])),
            slippage=Decimal(str(d.get("slippage", "0.03"))),
            armed=bool(d.get("armed", False)),
            direction=d.get("direction"),
            fired=bool(d.get("fired", False)),
            last_error=d.get("last_error"),
        )


class StopStore:
    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "stops.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self.stops: Dict[str, StopConfig] = {}

    async def load(self) -> None:
        async with self._lock:
            if not self.path.exists():
                self.stops = {}
                return
            try:
                raw = json.loads(self.path.read_text())
                self.stops = {
                    ticker: StopConfig.from_dict(value)
                    for ticker, value in raw.items()
                }
            except Exception:
                # Fail closed: a corrupt stop file never auto-arms unknown orders.
                self.stops = {}

    async def save(self) -> None:
        async with self._lock:
            payload = {k: v.json_dict() for k, v in self.stops.items()}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
            tmp.replace(self.path)

    async def upsert(self, stop: StopConfig) -> None:
        self.stops[stop.ticker] = stop
        await self.save()

    async def disarm(self, ticker: str, error: Optional[str] = None) -> None:
        s = self.stops.get(ticker)
        if not s:
            return
        s.armed = False
        s.last_error = error
        await self.save()
