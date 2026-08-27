from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .engine_v2 import StopEngine
from .kalshi import KalshiClient
from .store import StopStore

DATA_DIR = os.getenv("DATA_DIR", "/data" if Path("/data").exists() else "./data")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
COOKIE_NAME = "kalshi_stop_session"
COOKIE_SECRET = os.getenv("COOKIE_SECRET", DASHBOARD_PASSWORD or secrets.token_hex(32))
EXECUTION_ENABLED = os.getenv("EXECUTION_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
STATIC_DIR = Path(__file__).parent / "static"

kalshi = KalshiClient()
store = StopStore(DATA_DIR)
engine = StopEngine(kalshi, store, EXECUTION_ENABLED)
ws_task = None

def session_token() -> str:
    return hmac.new(COOKIE_SECRET.encode(), b"kalshi-stop-session-v1", hashlib.sha256).hexdigest()

def authed(request: Request) -> bool:
    return bool(DASHBOARD_PASSWORD) and hmac.compare_digest(request.cookies.get(COOKIE_NAME, ""), session_token())

def require_auth(request: Request):
    if not authed(request):
        raise HTTPException(status_code=401, detail="Not authenticated")

async def runtime_probe():
    await asyncio.sleep(2)
    open_positions = sum(1 for p in engine.positions.values() if p != 0)
    print(f"KALSHI_RUNTIME configured={kalshi.configured} ws_connected={engine.ws_connected} environment={kalshi.env} execution_enabled={EXECUTION_ENABLED} open_positions={open_positions}", flush=True)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global ws_task
    await store.load()
    try:
        await engine.bootstrap()
    except Exception as e:
        engine.log("error", f"Startup sync failed: {e}")
        print(f"KALSHI_STARTUP_ERROR {type(e).__name__}: {str(e)[:300]}", flush=True)
    ws_task = asyncio.create_task(kalshi.ws_loop(engine.on_ws_message, engine.markets_to_watch, engine.on_connected))
    asyncio.create_task(runtime_probe())
    yield
    if ws_task:
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass
    await kalshi.close()

app = FastAPI(title="Kalchi Kill", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response

@app.get("/health")
async def health():
    return {"ok": True, "ws_connected": engine.ws_connected, "kalshi_configured": kalshi.configured, "environment": kalshi.env, "execution_enabled": EXECUTION_ENABLED}

@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")

class LoginBody(BaseModel):
    password: str

@app.post("/api/login")
async def login(body: LoginBody, response: Response):
    if not DASHBOARD_PASSWORD:
        raise HTTPException(status_code=503, detail="DASHBOARD_PASSWORD is not configured")
    if not hmac.compare_digest(body.password, DASHBOARD_PASSWORD):
        raise HTTPException(status_code=401, detail="Wrong password")
    response.set_cookie(COOKIE_NAME, session_token(), httponly=True, secure=os.getenv("COOKIE_SECURE", "true").lower() != "false", samesite="strict", max_age=60 * 60 * 24 * 30)
    return {"ok": True}

@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}

@app.get("/api/state", dependencies=[Depends(require_auth)])
async def state():
    return engine.state()

@app.get("/api/markets/{ticker}", dependencies=[Depends(require_auth)])
async def market_lookup(ticker: str):
    try:
        return await engine.lookup_market(ticker.strip().upper())
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)[:300])

class StopBody(BaseModel):
    trigger_mode: str = "price"
    operator: str = "lte"
    trigger_value: Decimal
    slippage_cents: Decimal = Field(ge=Decimal("0"), le=Decimal("25"))
    armed: bool

@app.put("/api/stops/{ticker}", dependencies=[Depends(require_auth)])
async def set_stop(ticker: str, body: StopBody):
    ticker = ticker.strip().upper()
    trigger = body.trigger_value / Decimal("100") if body.trigger_mode == "price" else body.trigger_value
    try:
        stop = await engine.set_stop(ticker, trigger, body.slippage_cents / Decimal("100"), body.trigger_mode, body.operator, body.armed)
        return stop.json_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

class EntryBody(BaseModel):
    ticker: str
    direction: str
    quantity: Decimal = Field(gt=Decimal("0"))
    entry_mode: str = "price"
    entry_operator: str = "lte"
    entry_trigger_value: Optional[Decimal] = None
    entry_trigger_cents: Optional[Decimal] = None
    entry_slippage_cents: Decimal = Field(ge=Decimal("0"), le=Decimal("25"))
    exit_mode: str = "price"
    exit_operator: str = "gte"
    exit_trigger_value: Decimal
    exit_slippage_cents: Decimal = Field(ge=Decimal("0"), le=Decimal("25"))

@app.post("/api/entries", dependencies=[Depends(require_auth)])
async def create_entry(body: EntryBody):
    raw_entry = body.entry_trigger_value if body.entry_trigger_value is not None else body.entry_trigger_cents
    if raw_entry is None:
        raise HTTPException(status_code=400, detail="Entry trigger value is required")
    entry_trigger = raw_entry / Decimal("100") if body.entry_mode == "price" else raw_entry
    exit_trigger = body.exit_trigger_value / Decimal("100") if body.exit_mode == "price" else body.exit_trigger_value
    try:
        plan = await engine.create_entry_plan(
            body.ticker, body.direction.lower(), body.quantity, body.entry_mode, body.entry_operator,
            entry_trigger, body.entry_slippage_cents / Decimal("100"),
            body.exit_mode, body.exit_operator, exit_trigger, body.exit_slippage_cents / Decimal("100")
        )
        return plan.json_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/entries/{plan_id}", dependencies=[Depends(require_auth)])
async def delete_entry(plan_id: str):
    try:
        await engine.cancel_entry_plan(plan_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    engine.log("error", f"HTTP error: {str(exc)[:300]}")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
