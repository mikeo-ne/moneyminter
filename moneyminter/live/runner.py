"""Live trading runner: real MT5 market data -> strategies -> risk -> real orders.

Unlike the paper engine this uses **wall-clock time only** (no speed multiplier)
and acts once per closed bar, which is what a real strategy expects.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from ..data.feeds import timeframe_seconds
from ..instruments import get_instrument, normalize
from ..models import SignalType, utcnow
from ..risk import RiskConfig, RiskManager
from ..strategies import Strategy, get_strategy
from .mt5_broker import MT5Broker, MT5Error

log = logging.getLogger("moneyminter.live")


@dataclass
class LiveConfig:
    symbols: List[str] = field(default_factory=lambda: ["EUR/USD"])
    timeframe: str = "M5"
    strategy: str = "ma_crossover"
    strategy_params: dict = field(default_factory=dict)
    risk: RiskConfig = field(default_factory=RiskConfig)
    # MT5 connection
    login: Optional[int] = None
    password: Optional[str] = None
    server: Optional[str] = None
    terminal_path: Optional[str] = None
    symbol_suffix: Optional[str] = None
    allow_live: bool = False
    max_live_balance: float = 500.0
    dry_run: bool = False
    # remote / alternative backends (macOS, Linux+Wine, LAN Windows box)
    host: Optional[str] = None
    port: int = 18812
    backend: str = "auto"
    poll_seconds: float = 5.0
    history_bars: int = 600


class LiveTrader:
    def __init__(self, config: LiveConfig):
        self.config = config
        self.config.symbols = [normalize(s) for s in config.symbols]
        self.broker = MT5Broker(
            login=config.login, password=config.password, server=config.server,
            path=config.terminal_path, allow_live=config.allow_live,
            max_live_balance=config.max_live_balance, symbol_suffix=config.symbol_suffix,
            dry_run=config.dry_run, host=config.host, port=config.port,
            backend=config.backend)
        self.risk = RiskManager(config.risk)
        self.strategies: Dict[str, Strategy] = {
            s: get_strategy(config.strategy, **config.strategy_params) for s in self.config.symbols
        }
        self._last_bar: Dict[str, datetime] = {}
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self.events: List[dict] = []
        eq = self.broker.equity()
        self.risk.update_equity(eq, utcnow().date())
        self.start_equity = eq

    # ------------------------------------------------------------------
    def preflight(self) -> dict:
        """Validate the account, symbols and sizing before any order is sent."""
        acct = self.broker.account_summary()
        rows = []
        for sym in self.config.symbols:
            broker_sym = self.broker.resolve_symbol(sym)
            spec = self.broker.spec(sym)
            df = self.broker.candles(sym, self.config.timeframe, 50)
            strat = self.strategies[sym]
            price = self.broker.price(sym)
            inst = get_instrument(sym)
            units = self.risk.position_size(acct["equity"], price, price - 20 * inst.pip, inst)
            rows.append({
                "symbol": sym, "broker_symbol": broker_sym, "price": price,
                "digits": spec.digits, "contract_size": spec.trade_contract_size,
                "min_lot": spec.volume_min, "lot_step": spec.volume_step,
                "spread_points": spec.spread,
                "example_lots": self.broker.units_to_lots(sym, units),
                "bars_available": len(df),
                "warmup_needed": strat.warmup,
                "ready": len(df) >= 50,
            })
        return {"account": acct, "symbols": rows}

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._emit("info", f"Live trading started on {'DEMO' if self.broker.is_demo else 'LIVE'} "
                           f"#{self.broker.account.login}")
        self._thread = threading.Thread(target=self._loop, name="mm-live", daemon=True)
        self._thread.start()

    def stop(self, close_positions: bool = False) -> None:
        self.running = False
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        if close_positions:
            for p in list(self.broker.positions.values()):
                try:
                    self.broker.close(p, reason="trader stopped")
                except MT5Error as exc:
                    log.error("close failed: %s", exc)
        self._emit("info", "Live trading stopped")

    def _loop(self) -> None:
        """Poll forever, surviving disconnects.

        A VPS will drop the terminal link occasionally (broker restarts, network
        blips, MT5 updates). Rather than dying or spamming, back off and try to
        reconnect; consecutive failures widen the delay up to a minute.
        """
        failures = 0
        while self.running:
            try:
                self.poll()
                if failures:
                    self._emit("info", "Recovered — connection restored")
                failures = 0
            except MT5Error as exc:
                failures += 1
                log.error("broker error (%d): %s", failures, exc)
                if failures == 1 or failures % 10 == 0:
                    self._emit("error", str(exc))
                if not self.broker.is_connected():
                    self._emit("warn", "Terminal link lost — attempting reconnect")
                    if self.broker.reconnect():
                        self._emit("info", "Reconnected to MT5")
                        failures = 0
            except Exception as exc:  # noqa: BLE001
                failures += 1
                log.exception("live loop error")
                self._emit("error", f"{type(exc).__name__}: {exc}")
            delay = self.config.poll_seconds if not failures else \
                min(self.config.poll_seconds * (2 ** min(failures, 4)), 60.0)
            time.sleep(delay)

    def poll(self) -> None:
        """Check each symbol; act only when a new bar has closed."""
        now = utcnow()
        equity = self.broker.equity()
        self.risk.update_equity(equity, now.date())

        errors = []
        for sym in self.config.symbols:
            try:
                df = self.broker.candles(sym, self.config.timeframe, self.config.history_bars)
            except MT5Error as exc:
                # one unavailable symbol must not stop the others
                errors.append(f"{sym}: {exc}")
                continue
            if len(df) < 3:
                continue
            # the last row is the still-forming bar; act on the last CLOSED one
            closed = df.iloc[:-1]
            bar_ts = closed.index[-1].to_pydatetime()
            if self._last_bar.get(sym) == bar_ts:
                continue
            self._last_bar[sym] = bar_ts
            try:
                self._on_closed_bar(sym, closed, now, equity)
            except MT5Error as exc:
                errors.append(f"{sym}: {exc}")
        if errors and len(errors) == len(self.config.symbols):
            raise MT5Error("; ".join(errors[:3]))   # everything failed -> reconnect
        for e in errors:
            self._emit("error", e)

    def _on_closed_bar(self, symbol: str, df, now: datetime, equity: float) -> None:
        strat = self.strategies[symbol]
        if len(df) < strat.warmup + 2:
            log.debug("%s warming up (%d/%d bars)", symbol, len(df), strat.warmup)
            return
        prepared = strat.prepare(df)
        prepared.symbol_name = symbol
        pos = self.broker.position_for(symbol)
        signal = strat.on_bar(prepared, pos)

        if pos is not None and signal.type is SignalType.CLOSE:
            trade = self.broker.close(pos, reason=signal.reason or "signal")
            self._emit("close", f"Closed {symbol} {trade.side.value} "
                                f"P&L {trade.pnl:.2f} {self.broker.currency}")
            return

        if pos is None and signal.is_entry:
            ok, why = self.risk.can_open(equity, len(self.broker.positions), now.date())
            if not ok:
                self._emit("blocked", f"{signal.type.value} {symbol} blocked: {why}")
                return
            price = self.broker.price(symbol)
            units = self.risk.position_size(equity, price, signal.stop_loss,
                                            get_instrument(symbol), signal.confidence)
            if units <= 0:
                return
            p = self.broker.open(symbol, signal.side, units, price, now,
                                 signal.stop_loss, signal.take_profit, strat.name)
            if p:
                self._emit("open", f"Opened {symbol} {p.side.value} @ {p.entry_price:.5f} "
                                   f"SL {signal.stop_loss} TP {signal.take_profit} — {signal.reason}")

    # ------------------------------------------------------------------
    def _emit(self, kind: str, message: str) -> None:
        ev = {"ts": utcnow().isoformat(), "kind": kind, "message": message}
        self.events.insert(0, ev)
        del self.events[200:]
        log.info("%s | %s", kind.upper(), message)

    def state(self) -> dict:
        acct = self.broker.account_summary()
        positions = []
        for p in self.broker.positions.values():
            px = self.broker.price(p.symbol)
            positions.append({**p.to_dict(), "current_price": px})
        return {"running": self.running, "account": acct, "positions": positions,
                "trades": [t.to_dict() for t in self.broker.trades[-50:]][::-1],
                "events": self.events[:50],
                "equity": acct["equity"],
                "pnl": round(acct["equity"] - self.start_equity, 2)}
