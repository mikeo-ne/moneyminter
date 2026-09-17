"""Live/paper trading engine — multi-symbol, multi-strategy, event loop."""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional

import pandas as pd

from .broker import ExecutionConfig, PaperBroker
from .data import load_feed
from .data.feeds import LiveTickStream, timeframe_seconds
from .instruments import get_instrument, normalize
from .models import Position, SignalType, Trade, utcnow
from .risk import RiskConfig, RiskManager
from .strategies import Strategy, get_strategy

log = logging.getLogger("moneyminter")


@dataclass
class BotConfig:
    symbols: List[str] = field(default_factory=lambda: ["EUR/USD", "GBP/USD", "USD/JPY"])
    timeframe: str = "M1"
    strategy: str = "ma_crossover"
    strategy_params: dict = field(default_factory=dict)
    balance: float = 10_000.0
    mode: str = "paper"                 # paper | backtest
    speed: float = 60.0                 # simulation speedup for paper trading
    tick_interval: float = 0.5          # seconds between ticks
    source: str = "synthetic"
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    journal_path: Optional[str] = "data/journal.jsonl"

    @classmethod
    def load(cls, path: str | Path) -> "BotConfig":
        raw = json.loads(Path(path).read_text())
        risk = RiskConfig(**raw.pop("risk", {}))
        execution = ExecutionConfig(**raw.pop("execution", {}))
        return cls(risk=risk, execution=execution, **raw)

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


class TradingBot:
    """The Money Minter robot.

    Runs a background thread that streams ticks, aggregates candles, asks each
    strategy for a signal, sizes positions with the risk manager and executes
    through the broker.
    """

    def __init__(self, config: Optional[BotConfig] = None):
        self.config = config or BotConfig()
        self.config.symbols = [normalize(s) for s in self.config.symbols]
        self.broker = PaperBroker(self.config.balance, self.config.execution)
        self.risk = RiskManager(self.config.risk)
        self.strategies: Dict[str, Strategy] = {
            s: get_strategy(self.config.strategy, **self.config.strategy_params)
            for s in self.config.symbols
        }
        self.streams: Dict[str, LiveTickStream] = {}
        self.prices: Dict[str, float] = {}
        self.events: Deque[dict] = deque(maxlen=500)
        self.started_at: Optional[datetime] = None
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self.bars_seen = 0
        self._sim_epoch = utcnow()
        self._sim_elapsed = 0.0
        self._bootstrap()

    def now(self) -> datetime:
        """Virtual market time: wall clock accelerated by ``config.speed``.

        Paper trading compresses days into minutes, so risk windows (daily loss
        limits, swap charges, bar timestamps) must follow simulated time.
        """
        from datetime import timedelta
        return self._sim_epoch + timedelta(seconds=self._sim_elapsed)

    # ---- setup ---------------------------------------------------------
    def _bootstrap(self) -> None:
        for i, sym in enumerate(self.config.symbols):
            feed = load_feed(sym, self.config.timeframe, self.config.source, bars=800, seed=11 + i)
            self.streams[sym] = LiveTickStream(sym, self.config.timeframe, feed.df,
                                               seed=101 + i, speed=self.config.speed)
            self.prices[sym] = float(feed.df.close.iloc[-1])
        self.risk.update_equity(self.broker.balance, self.now().date())
        self.broker.mark(self.now(), self.prices)

    # ---- lifecycle ------------------------------------------------------
    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.started_at = utcnow()
        self._emit("info", "Money Minter started", {
            "symbols": self.config.symbols, "strategy": self.config.strategy,
            "timeframe": self.config.timeframe, "balance": self.broker.balance})
        self._thread = threading.Thread(target=self._loop, name="mm-engine", daemon=True)
        self._thread.start()

    def stop(self, close_positions: bool = True) -> None:
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        if close_positions:
            with self._lock:
                for p in list(self.broker.positions.values()):
                    t = self.broker.close(p, self.prices[p.symbol], self.now(), "bot stopped")
                    self._emit("close", f"Closed {t.symbol} {t.side.value} @ {t.exit_price:.5f}", t.to_dict())
        self._emit("info", "Money Minter stopped", {"equity": self.equity()})

    def reset(self) -> None:
        self.stop(close_positions=False)
        self.__init__(self.config)  # noqa: PLC2801 - deliberate full reset

    # ---- main loop -------------------------------------------------------
    def _loop(self) -> None:
        while self.running:
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - keep the robot alive
                log.exception("engine error")
                self._emit("error", f"engine error: {exc}", {})
            time.sleep(self.config.tick_interval)

    def step(self) -> None:
        """One engine iteration: tick -> stops -> bar close -> signals."""
        with self._lock:
            self._sim_elapsed += self.config.tick_interval * self.config.speed
            now = self.now()
            for sym, stream in self.streams.items():
                tick = stream.next_tick(self.config.tick_interval * self.config.speed)
                self.prices[sym] = tick.mid
                self._check_stops(sym, tick.mid, now)
                candle = stream.maybe_close_bar(now)
                if candle is not None:
                    self.bars_seen += 1
                    self._on_bar(sym, stream.df, now)
            equity = self.broker.mark(now, self.prices)
            self.risk.update_equity(equity, now.date())

    def _check_stops(self, symbol: str, price: float, now: datetime) -> None:
        pos = self.broker.position_for(symbol)
        if pos is None:
            return
        hit = pos.hit_stop(price, price)
        if hit is not None:
            reason = "stop loss" if pos.stop_loss is not None and abs(hit - pos.stop_loss) < 1e-12 else "take profit"
            trade = self.broker.close(pos, hit, now, reason)
            self._emit("close", f"{reason.title()} {symbol} {trade.side.value} "
                                f"P&L ${trade.pnl:,.2f}", trade.to_dict())

    def _on_bar(self, symbol: str, df: pd.DataFrame, now: datetime) -> None:
        strat = self.strategies[symbol]
        prepared = strat.prepare(df)
        if len(prepared) < strat.warmup + 2:
            return
        prepared.symbol_name = symbol
        pos = self.broker.position_for(symbol)
        signal = strat.on_bar(prepared, pos)
        price = self.prices[symbol]

        if pos is not None and signal.type is SignalType.CLOSE:
            trade = self.broker.close(pos, price, now, signal.reason or "signal")
            self._emit("close", f"Closed {symbol} {trade.side.value} P&L ${trade.pnl:,.2f}", trade.to_dict())
            return
        if pos is None and signal.is_entry:
            equity = self.broker.equity(self.prices)
            ok, why = self.risk.can_open(equity, len(self.broker.positions), now.date())
            if not ok:
                self._emit("blocked", f"Signal {signal.type.value} {symbol} blocked: {why}", {})
                return
            units = self.risk.position_size(equity, price, signal.stop_loss,
                                            get_instrument(symbol), signal.confidence)
            if units <= 0:
                return
            p = self.broker.open(symbol, signal.side, units, price, now,
                                 signal.stop_loss, signal.take_profit, strat.name)
            if p:
                self._emit("open", f"Opened {symbol} {p.side.value} {units:,.0f} units @ {p.entry_price:.5f}"
                                   f" — {signal.reason}", p.to_dict())

    # ---- reporting --------------------------------------------------------
    def _emit(self, kind: str, message: str, payload: dict) -> None:
        ev = {"ts": utcnow().isoformat(), "kind": kind, "message": message, "data": payload}
        self.events.appendleft(ev)
        log.info("%s | %s", kind.upper(), message)
        if self.config.journal_path:
            try:
                p = Path(self.config.journal_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a") as fh:
                    fh.write(json.dumps(ev) + "\n")
            except OSError:
                pass

    def equity(self) -> float:
        return self.broker.equity(self.prices)

    def state(self) -> dict:
        with self._lock:
            eq = self.equity()
            curve = self.broker.equity_curve[-600:]
            open_pos = []
            for p in self.broker.positions.values():
                px = self.prices.get(p.symbol, p.entry_price)
                inst = get_instrument(p.symbol)
                d = p.to_dict()
                d.update(current_price=px, unrealized=round(p.unrealized_pnl(px), 2),
                         pips=round(inst.pips((px - p.entry_price) * p.side.sign), 1))
                open_pos.append(d)
            trades = [t.to_dict() for t in self.broker.trades[-100:]][::-1]
            wins = sum(1 for t in self.broker.trades if t.is_win)
            n = len(self.broker.trades)
            return {
                "running": self.running,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "config": {"symbols": self.config.symbols, "timeframe": self.config.timeframe,
                           "strategy": self.config.strategy, "speed": self.config.speed,
                           "mode": self.config.mode},
                "balance": round(self.broker.balance, 2),
                "equity": round(eq, 2),
                "initial_balance": self.broker.initial_balance,
                "pnl": round(eq - self.broker.initial_balance, 2),
                "pnl_pct": round((eq / self.broker.initial_balance - 1) * 100, 3),
                "open_positions": open_pos,
                "prices": {k: round(v, 5) for k, v in self.prices.items()},
                "trades": trades,
                "stats": {"trades": n, "wins": wins, "losses": n - wins,
                          "win_rate": round(wins / n * 100, 1) if n else 0.0,
                          "bars": self.bars_seen,
                          "drawdown_pct": round(self.risk.drawdown_pct(eq) * 100, 2),
                          "daily_pnl_pct": round(self.risk.daily_pnl_pct(eq) * 100, 2),
                          "halted": self.risk.halted_reason},
                "equity_curve": [{"t": t.isoformat(), "equity": round(e, 2)} for t, e in curve],
                "events": list(self.events)[:60],
            }
