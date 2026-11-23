"""FastAPI dashboard + REST API for Money Minter.

Endpoints cover three things a tester actually needs:

1. **Paper trading** — start/stop/close, live state, equity curve.
2. **Demo trading** — run the real MT5 code path against the simulated terminal
   (:class:`~moneyminter.live.sim_terminal.SimTerminal`), or connect to a real
   MT5/Exness demo account when one is available.
3. **Evidence** — backtests and the full validation battery, so a strategy has
   to earn its way onto the demo account rather than being trusted.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import (Depends, FastAPI, HTTPException, Request, WebSocket,
                     WebSocketDisconnect)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..backtest import run_backtest
from ..broker import ExecutionConfig
from ..engine import BotConfig, TradingBot
from ..instruments import INSTRUMENTS
from ..risk import RiskConfig
from ..strategies import available_strategies

HERE = Path(__file__).parent
REPORT_PATH = Path("reports/validation.json")


class StartRequest(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: ["EUR/USD", "GBP/USD", "USD/JPY"])
    timeframe: str = "M1"
    strategy: str = "trend_pullback"
    balance: float = 10_000.0
    speed: float = 60.0
    risk_per_trade: float = 0.005
    max_positions: int = 3
    max_daily_loss: float = 0.03
    max_drawdown: float = 0.15


class BacktestRequest(BaseModel):
    symbol: str = "EUR/USD"
    timeframe: str = "H1"
    strategy: str = "trend_pullback"
    balance: float = 10_000.0
    bars: int = 3000
    source: str = "synthetic"
    risk_per_trade: float = 0.005
    seed: int | None = 7
    params: dict = Field(default_factory=dict)


class ValidateRequest(BaseModel):
    symbol: str = "EUR/USD"
    timeframe: str = "H1"
    strategy: str = "trend_pullback"
    samples: int = 8
    bars: int = 2500
    params: dict = Field(default_factory=dict)


class DemoRequest(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: ["EUR/USD", "GBP/USD"])
    timeframe: str = "M5"
    strategy: str = "trend_pullback"
    balance: float = 10_000.0
    risk_per_trade: float = 0.005
    bars_per_poll: int = 1
    polls: int = 2000
    params: dict = Field(default_factory=dict)
    close_on_exit: bool = True


_security = HTTPBasic(auto_error=False)

#: paths a phone must be able to load before it has credentials (the page and
#: its assets); everything else, including every mutating endpoint, is gated.
#: the page shell must load for the browser to show its credentials prompt, so
#: "/" is public; every /api/* and /ws* route stays gated.
_PUBLIC_PATHS = ("/static", "/favicon.ico", "/healthz")
_PUBLIC_EXACT = {"/"}


def _resolve_auth(enable: Optional[bool], user: Optional[str],
                  password: Optional[str]) -> tuple[bool, str, str]:
    """Decide whether the dashboard requires a password.

    If the operator did not say, require one whenever we are listening on a
    public interface. ``0.0.0.0`` means "reachable from the network", and on a
    shared or borrowed machine an unauthenticated ``POST /api/stop`` can halt
    someone else's bot — and ``/api/start`` can arm it.
    """
    env_user = os.getenv("MM_DASH_USER")
    env_pass = os.getenv("MM_DASH_PASSWORD")
    user = user or env_user or "admin"
    password = password if password is not None else env_pass
    if enable is None:
        enable = bool(password)
    if enable and not password:
        # generate one rather than shipping a default, and shout about it
        password = secrets.token_urlsafe(9)
        print("\n" + "=" * 66)
        print("  DASHBOARD PASSWORD (generated, this run only)")
        print(f"    user:     {user}")
        print(f"    password: {password}")
        print("  Set MM_DASH_PASSWORD (or pass --dashboard-password) to keep it.")
        print("=" * 66 + "\n")
    return bool(enable), user, password or ""


def create_app(config: Optional[BotConfig] = None, autostart: bool = True,
               auth: Optional[bool] = None, auth_user: Optional[str] = None,
               auth_password: Optional[str] = None) -> FastAPI:
    state: dict = {"bot": TradingBot(config or BotConfig()), "demo": None}
    auth_on, auth_user, auth_password = _resolve_auth(auth, auth_user, auth_password)
    state["auth"] = auth_on

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if autostart:
            state["bot"].start()
        yield
        state["bot"].stop()
        if state["demo"] is not None:
            state["demo"].stop(close_positions=False)

    app = FastAPI(title="Money Minter", version="2.0.0",
                  description="Automated FX trading robot", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        """Gate every route by default.

        A per-endpoint dependency would be safer to reason about but easy to
        forget on a new route, and forgetting it on /api/start or /api/stop is
        exactly the mistake that matters.
        """
        if (state["auth"] and request.url.path not in _PUBLIC_EXACT
                and not request.url.path.startswith(_PUBLIC_PATHS)):
            header = request.headers.get("authorization", "")
            ok = False
            if header.lower().startswith("basic "):
                try:
                    import base64
                    raw = base64.b64decode(header.split(None, 1)[1]).decode()
                    user, _, pwd = raw.partition(":")
                    ok = (hmac.compare_digest(user, auth_user)
                          and hmac.compare_digest(pwd, auth_password))
                except Exception:  # noqa: BLE001 - malformed header
                    ok = False
            if not ok:
                from starlette.responses import Response
                return Response("Authentication required", status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="Money Minter"'})
        return await call_next(request)

    def bot() -> TradingBot:
        return state["bot"]

    def _check(credentials: Optional[HTTPBasicCredentials]) -> None:
        """Constant-time credential check; no-op when auth is disabled."""
        if not state["auth"]:
            return
        if credentials is None:
            raise HTTPException(401, "Authentication required",
                                headers={"WWW-Authenticate": "Basic"})
        user_ok = hmac.compare_digest(credentials.username, auth_user)
        pass_ok = hmac.compare_digest(credentials.password, auth_password)
        if not (user_ok and pass_ok):
            raise HTTPException(401, "Bad credentials",
                                headers={"WWW-Authenticate": "Basic"})

    async def require_auth(
        request: Request,
        credentials: Optional[HTTPBasicCredentials] = Depends(_security),
    ) -> None:
        if (request.url.path in _PUBLIC_EXACT
                or request.url.path.startswith(_PUBLIC_PATHS)):
            return
        _check(credentials)

    # ------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (HERE / "templates" / "index.html").read_text()

    # ------------------------------------------------------------ metadata
    @app.get("/api/meta")
    async def api_meta():
        return {
            "version": "2.0.0",
            "strategies": {n: {"doc": (c.__doc__ or "").strip().split("\n")[0],
                               "style": getattr(c, "style", "")}
                           for n, c in available_strategies().items()},
            "instruments": list(INSTRUMENTS),
            "timeframes": ["M1", "M5", "M15", "M30", "H1", "H4", "D1"],
            "modes": ["paper", "demo-sim"],
            "validation": _load_validation_summary(),
        }

    @app.get("/api/state")
    async def api_state():
        return bot().state()

    @app.get("/api/risk")
    async def api_risk():
        b = bot()
        return b.risk.summary(b.equity())

    # ------------------------------------------------------------ paper bot
    @app.post("/api/start")
    async def api_start(req: StartRequest):
        b = bot()
        if b.running:
            b.stop()
        cfg = BotConfig(symbols=req.symbols, timeframe=req.timeframe, strategy=req.strategy,
                        balance=req.balance, speed=req.speed,
                        risk=RiskConfig(risk_per_trade=req.risk_per_trade,
                                        max_positions=req.max_positions,
                                        max_daily_loss=req.max_daily_loss,
                                        max_drawdown=req.max_drawdown))
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
        b.risk.record_result(pos.symbol, trade.pnl, b.now())
        b._emit("close", f"Manually closed {trade.symbol} P&L ${trade.pnl:,.2f}",
                trade.to_dict())
        return {"ok": True, "trade": trade.to_dict()}

    # ------------------------------------------------------------ research
    @app.post("/api/backtest")
    async def api_backtest(req: BacktestRequest):
        def _run():
            return run_backtest(req.symbol, req.timeframe, req.strategy, req.balance,
                                source=req.source, bars=req.bars, seed=req.seed,
                                risk=RiskConfig(risk_per_trade=req.risk_per_trade),
                                execution=ExecutionConfig(),
                                strategy_params=req.params or None).to_dict()
        return JSONResponse(await asyncio.to_thread(_run))

    @app.post("/api/validate")
    async def api_validate(req: ValidateRequest):
        from ..validation import validate as run_validation

        def _run():
            return run_validation(req.strategy, symbol=req.symbol,
                                  timeframe=req.timeframe, samples=req.samples,
                                  bars=req.bars,
                                  params=req.params or None).to_dict()
        return JSONResponse(await asyncio.to_thread(_run))

    @app.get("/api/validation")
    async def api_validation():
        return _load_validation_summary() or {"reports": []}

    # ------------------------------------------------------------ demo sim
    @app.post("/api/demo/start")
    async def api_demo_start(req: DemoRequest):
        from ..live import LiveConfig, LiveTrader
        from ..live.sim_terminal import install

        if state["demo"] is not None and state["demo"].running:
            state["demo"].stop(close_positions=req.close_on_exit)
        install(symbols=req.symbols, timeframe=req.timeframe,
                bars=max(1200, req.bars_per_poll * 600), balance=req.balance)
        cfg = LiveConfig(symbols=req.symbols, timeframe=req.timeframe,
                         strategy=req.strategy, strategy_params=req.params or {},
                         risk=RiskConfig(risk_per_trade=req.risk_per_trade),
                         sim=True, sim_bars_per_poll=req.bars_per_poll,
                         history_bars=max(300, req.bars_per_poll * 100),
                         poll_seconds=0, journal_path=None)
        trader = LiveTrader(cfg)
        state["demo"] = trader
        trader.start()
        return {"ok": True, "state": trader.state()}

    @app.post("/api/demo/stop")
    async def api_demo_stop():
        if state["demo"] is None:
            raise HTTPException(404, "no demo session")
        state["demo"].stop(close_positions=True)
        return {"ok": True, "state": state["demo"].state()}

    @app.get("/api/demo/state")
    async def api_demo_state():
        if state["demo"] is None:
            return {"running": False, "mode": None, "positions": [], "trades": [],
                    "events": [], "equity": None}
        return state["demo"].state()

    # ------------------------------------------------------------ streams
    def _ws_ok(ws: WebSocket) -> bool:
        if not state["auth"]:
            return True
        import base64
        header = ws.headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            return False
        try:
            raw = base64.b64decode(header.split(None, 1)[1]).decode()
            user, _, pwd = raw.partition(":")
        except Exception:  # noqa: BLE001
            return False
        return (hmac.compare_digest(user, auth_user)
                and hmac.compare_digest(pwd, auth_password))

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        if not _ws_ok(socket):
            await socket.close(code=1008)      # policy violation
            return
        await socket.accept()
        try:
            while True:
                await socket.send_text(json.dumps(bot().state()))
                await asyncio.sleep(1.0)
        except (WebSocketDisconnect, RuntimeError):
            return

    @app.websocket("/ws/demo")
    async def ws_demo(socket: WebSocket):
        if not _ws_ok(socket):
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            while True:
                if state["demo"] is None:
                    await socket.send_text(json.dumps({"running": False,
                                                       "positions": [], "trades": [],
                                                       "events": []}))
                else:
                    await socket.send_text(json.dumps(state["demo"].state()))
                await asyncio.sleep(1.0)
        except (WebSocketDisconnect, RuntimeError):
            return

    return app


def _load_validation_summary() -> Optional[dict]:
    """Serve the last `moneyminter validate --json` run, if there is one."""
    from ..validation import load_reports
    data = load_reports(REPORT_PATH)
    if not data:
        return None
    return {"generated_at": data.get("generated_at"),
            "thresholds": data.get("thresholds"),
            "reports": [{**r, "samples": r.get("samples", [])}
                        for r in data.get("reports", [])]}


app = create_app()
