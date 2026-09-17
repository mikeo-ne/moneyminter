"""FastAPI dashboard + REST API for Money Minter."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..backtest import run_backtest
from ..engine import BotConfig, TradingBot
from ..instruments import INSTRUMENTS
from ..risk import RiskConfig
from ..strategies import available_strategies

HERE = Path(__file__).parent


class StartRequest(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: ["EUR/USD", "GBP/USD", "USD/JPY"])
    timeframe: str = "M1"
    strategy: str = "ma_crossover"
    balance: float = 10_000.0
    speed: float = 60.0
    risk_per_trade: float = 0.01
    max_positions: int = 3


class BacktestRequest(BaseModel):
    symbol: str = "EUR/USD"
    timeframe: str = "H1"
    strategy: str = "ma_crossover"
    balance: float = 10_000.0
    bars: int = 3000
    source: str = "synthetic"
    risk_per_trade: float = 0.01
    seed: int | None = 7


def create_app(config: Optional[BotConfig] = None, autostart: bool = True) -> FastAPI:
    state: dict = {"bot": TradingBot(config or BotConfig())}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if autostart:
            state["bot"].start()
        yield
        state["bot"].stop()

    app = FastAPI(title="Money Minter", version="1.0.0",
                  description="Automated FX trading robot", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def bot() -> TradingBot:
        return state["bot"]

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (HERE / "templates" / "index.html").read_text()

    @app.get("/api/state")
    async def api_state():
        return bot().state()

    @app.get("/api/meta")
    async def api_meta():
        return {
            "strategies": {n: {"doc": (c.__doc__ or "").strip().split("\n")[0]}
                           for n, c in available_strategies().items()},
            "instruments": list(INSTRUMENTS),
            "timeframes": ["M1", "M5", "M15", "M30", "H1", "H4", "D1"],
        }

    @app.post("/api/start")
    async def api_start(req: StartRequest):
        b = bot()
        if b.running:
            b.stop()
        cfg = BotConfig(symbols=req.symbols, timeframe=req.timeframe, strategy=req.strategy,
                        balance=req.balance, speed=req.speed,
                        risk=RiskConfig(risk_per_trade=req.risk_per_trade,
                                        max_positions=req.max_positions))
        state["bot"] = TradingBot(cfg)
        state["bot"].start()
        return {"ok": True, "state": state["bot"].state()}

    @app.post("/api/stop")
    async def api_stop():
        bot().stop()
        return {"ok": True, "state": bot().state()}

    @app.post("/api/close/{position_id}")
    async def api_close(position_id: str):
        b = bot()
        pos = b.broker.positions.get(position_id)
        if not pos:
            raise HTTPException(404, "position not found")
        trade = b.broker.close(pos, b.prices[pos.symbol], reason="manual close")
        b._emit("close", f"Manually closed {trade.symbol} P&L ${trade.pnl:,.2f}", trade.to_dict())
        return {"ok": True, "trade": trade.to_dict()}

    @app.post("/api/backtest")
    async def api_backtest(req: BacktestRequest):
        def _run():
            return run_backtest(req.symbol, req.timeframe, req.strategy, req.balance,
                                source=req.source, bars=req.bars, seed=req.seed,
                                risk=RiskConfig(risk_per_trade=req.risk_per_trade)).to_dict()
        return JSONResponse(await asyncio.to_thread(_run))

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        try:
            while True:
                await socket.send_text(json.dumps(bot().state()))
                await asyncio.sleep(1.0)
        except (WebSocketDisconnect, RuntimeError):
            return

    return app


app = create_app()
