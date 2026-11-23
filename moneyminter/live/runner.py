"""Live trading runner: real MT5 market data -> strategies -> risk -> real orders.

Unlike the paper engine this uses **wall-clock time only** (no speed multiplier)
and acts once per closed bar, which is what a real strategy expects.

Three modes share this code path:

* ``live``   — against a real MetaTrader 5 terminal (demo account by default),
* ``sim``    — against the in-process :class:`~moneyminter.live.sim_terminal.SimTerminal`,
               which lets you exercise every order/stop/reconciliation path with
               no terminal, no network and no waiting,
* ``dry-run`` — full loop, orders logged not sent.

What is new in this version
---------------------------
* **Trailing stops are actually trailed.** The stop lives at the broker (so you
  are protected if your machine dies) *and* is re-sent as the trade moves. The
  same ``management`` module the backtester uses decides the new stop, so a
  strategy's exits behave identically in test and in production.
* **Reconciliation.** Trades closed by the broker's own SL/TP (or by you, by
  hand) are detected and recorded with their real P&L instead of being silently
  forgotten — which is what makes the demo P&L trustworthy.
* **Partial-fill and retry accounting**, plus a per-symbol loss-streak pause.
* **A demo clock.** In ``sim`` mode the terminal advances a configurable number
  of bars per poll, so an "overnight" demo session runs in seconds.
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
from ..management import hard_breach, manage_bar
from ..models import Position, Side, SignalType, Trade, utcnow
from ..risk import RiskConfig, RiskManager
from ..strategies import Strategy, get_strategy
from .mt5_broker import MT5Broker, MT5Error, OrderRejected, StaleSignal

log = logging.getLogger("moneyminter.live")


@dataclass
class LiveConfig:
    symbols: List[str] = field(default_factory=lambda: ["EUR/USD"])
    timeframe: str = "M5"
    strategy: str = "trend_pullback"
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
    # simulation mode (see module docstring)
    sim: bool = False
    sim_bars_per_poll: int = 1
    sim_seed: int = 42
    #: trail/re-send the broker-side stop as the trade moves
    trail_broker_stops: bool = True
    #: seconds between stop-update checks
    manage_seconds: float = 20.0
    #: maximum bars a position may be held before a time exit (None = strategy)
    journal_path: Optional[str] = None


class LiveTrader:
    """Runs a strategy against an MT5 account, demo by default."""

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
            s: get_strategy(config.strategy, **config.strategy_params)
            for s in self.config.symbols
        }
        self._last_bar: Dict[str, datetime] = {}
        self._last_manage: float = 0.0
        self._stop_sent: Dict[str, float] = {}
        self._known_tickets: set[str] = set()
        self._ticket_symbols: Dict[str, str] = {}
        self._open_positions: Dict[str, Position] = {}
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self.events: List[dict] = []
        self.start_ts: Optional[datetime] = None
        self.equity_history: List[tuple] = []
        eq = self.broker.equity()
        self.risk.update_equity(eq, utcnow().date())
        self.start_equity = eq
        self._known_tickets = set(self.broker.positions.keys())
        self._ticket_symbols = {t: p.symbol for t, p in self.broker.positions.items()}

    # ------------------------------------------------------------------
    def preflight(self) -> dict:
        """Validate the account, symbols and sizing before any order is sent."""
        acct = self.broker.account_summary()
        rows = []
        for sym in self.config.symbols:
            broker_sym = self.broker.resolve_symbol(sym)
            spec = self.broker.spec(sym)
            strat = self.strategies[sym]
            df = self.broker.candles(sym, self.config.timeframe,
                                     min(self.config.history_bars, strat.warmup + 10))
            price = self.broker.price(sym)
            inst = get_instrument(sym)
            units = self.risk.position_size(acct["equity"], price, price - 20 * inst.pip, inst)
            lots = int(units / inst.contract_size / inst.lot_step) * inst.lot_step
            rows.append({
                "symbol": sym, "broker_symbol": broker_sym, "price": price,
                "digits": spec.digits, "contract_size": spec.trade_contract_size,
                "min_lot": spec.volume_min, "lot_step": spec.volume_step,
                "spread_points": spec.spread,
                "example_lots": round(lots, 2),
                "risk_per_trade": self.config.risk.risk_per_trade,
                "bars_available": len(df),
                "warmup_needed": strat.warmup,
                "ready": len(df) >= strat.warmup + 2,
            })
        return {"account": acct, "symbols": rows, "strategy": self.config.strategy,
                "timeframe": self.config.timeframe}

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.start_ts = utcnow()
        self._emit("info", f"Trading started on "
                           f"{'DEMO' if self.broker.is_demo else 'LIVE'} "
                           f"#{self.broker.account.login} — {self.config.strategy} "
                           f"{self.config.timeframe} on {', '.join(self.config.symbols)}")
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
                    t = self.broker.close(p, reason="trader stopped")
                    self.risk.record_result(p.symbol, t.pnl, utcnow())
                    self._emit("close", f"Closed {p.symbol} on shutdown "
                                        f"(P&L {t.pnl:+.2f})")
                except MT5Error as exc:
                    log.error("close failed: %s", exc)
                    self._emit("error", f"close failed for {p.symbol}: {exc}")
        self._emit("info", "Trading stopped")

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
            if self.config.sim:
                delay = min(delay, 0.01)
            time.sleep(delay)

    # ------------------------------------------------------------------
    def poll(self) -> None:
        """One cycle: advance the sim clock, reconcile, manage, act on new bars."""
        now = utcnow()
        if self.config.sim:
            # in simulation the "clock" is the terminal's bar cursor
            self.broker.mt5.advance(self.config.sim_bars_per_poll)
        equity = self.broker.equity()
        self.risk.update_equity(equity, now.date())

        # Read the terminal's positions EXACTLY ONCE per cycle.
        # ``broker.positions`` compares the current set against the previous
        # read to spot tickets the broker closed. Calling it twice per cycle
        # made the second call see an empty "vanished" set, so reconciliation
        # never learned which position had been stopped out — and the trade was
        # recorded with no side, no stop and no entry price.
        live_positions = self.broker.positions
        self._reconcile(now, live_positions)
        if not live_positions:
            # bank equity points while flat so the curve reflects realised P&L
            self.equity_history.append((now, equity))
            del self.equity_history[:-2000]
        if self.config.trail_broker_stops:
            self._manage_open_positions(now)

        errors = []
        for sym in self.config.symbols:
            try:
                df = self.broker.candles(sym, self.config.timeframe, self.config.history_bars)
            except MT5Error as exc:
                errors.append(f"{sym}: {exc}")
                continue
            if df is None or len(df) < 3:
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

    # ------------------------------------------------------------------
    def _reconcile(self, now: datetime, live: Optional[Dict[str, Position]] = None) -> None:
        """Detect positions the broker closed (SL/TP) or a human closed."""
        if live is None:
            live = self.broker.positions
        current = set(live.keys())
        vanished = set(self.broker.drain_vanished()) | (self._known_tickets - current)
        for ticket in sorted(vanished):
            pnl, reason = self._lookup_closed(ticket)
            self._record_external_close(ticket, pnl, reason, now)
            self.risk.record_result(self._ticket_symbol(ticket), pnl, now)
            row = {"ticket": ticket, "pnl": round(pnl, 2), "closed_at": now.isoformat(),
                   "reason": reason}
            self._emit("close", f"Position {ticket} closed by broker: "
                                f"{reason or 'external'} (P&L {pnl:+.2f})", row)
            self._known_tickets.discard(ticket)
            self._stop_sent.pop(ticket, None)
            self._ticket_symbols.pop(ticket, None)
        for ticket in current - self._known_tickets:
            self._known_tickets.add(ticket)
            self._emit("info", f"Adopted existing position {ticket} (opened by this robot)")
        for ticket, pos in live.items():
            self._open_positions[ticket] = pos
            self._ticket_symbols.setdefault(ticket, pos.symbol)

    def _record_external_close(self, ticket: str, pnl: float, reason: str,
                               now: datetime) -> None:
        """Turn a broker-initiated close into a first-class Trade record.

        Without this the demo P&L would only count trades the strategy closed
        itself, and every stop-out (the majority of exits) would vanish from the
        statistics — making a losing robot look flat.
        """
        pos = self._open_positions.pop(ticket, None)
        sym = self._ticket_symbol(ticket)
        deal = self._lookup_deal(ticket)
        side = pos.side if pos else None
        if side is None and deal is not None:
            # infer the side from the closing deal's order type
            side = self._side_from_deal(deal)
        self.broker.trades.append(Trade(
            symbol=sym, side=side, units=pos.units if pos else 0.0,
            entry_price=pos.entry_price if pos else 0.0,
            exit_price=float(getattr(deal, "price", 0.0)) if deal else (
                pos.closed_exit_price if pos and pos.closed_exit_price else 0.0),
            opened_at=pos.opened_at if pos else now,
            closed_at=now, pnl=pnl, reason=reason or "broker exit",
            strategy=pos.strategy if pos else self.config.strategy,
            initial_stop=pos.initial_stop if pos else None,
            bars_held=pos.bars_held if pos else 0,
            mfe_r=pos.mfe_r if pos else 0.0, mae_r=pos.mae_r if pos else 0.0))

    def _lookup_deal(self, ticket: str):
        try:
            deals = self.broker.mt5.history_deals_get(
                datetime(2000, 1, 1), utcnow()) or []
        except Exception:  # noqa: BLE001
            return None
        for d in deals:
            if str(getattr(d, "ticket", "")) == str(ticket) or \
                    str(getattr(d, "order", "")) == str(ticket):
                return d
        return None

    @staticmethod
    def _side_from_deal(deal):
        """The closing deal is the opposite of the position it closed."""
        kind = getattr(deal, "type", None)
        if kind is None:
            return None
        # MT5 DEAL_TYPE_BUY == 0, DEAL_TYPE_SELL == 1
        return Side.SELL if kind == 0 else Side.BUY

    def _lookup_closed(self, ticket: str) -> tuple[float, str]:
        """P&L and the broker's own reason for a position we did not close."""
        try:
            deals = self.broker.mt5.history_deals_get(
                datetime(2000, 1, 1), utcnow()) or []
        except Exception:  # noqa: BLE001
            return 0.0, ""
        for d in deals:
            if str(getattr(d, "ticket", "")) == str(ticket) or \
                    str(getattr(d, "order", "")) == str(ticket):
                reason = getattr(d, "reason", "") or ""
                if reason in ("sl", "tp"):
                    reason = "stop loss" if reason == "sl" else "take profit"
                return float(getattr(d, "profit", 0.0)), reason
        return 0.0, ""

    def _ticket_symbol(self, ticket: str) -> str:
        return self._ticket_symbols.get(ticket, self.config.symbols[0])

    def _manage_open_positions(self, now: datetime) -> None:
        """Trail the broker-side stop for each open position."""
        if time.time() - self._last_manage < self.config.manage_seconds and not self.config.sim:
            return
        self._last_manage = time.time()
        for pos in self.broker.positions.values():
            if not (pos.trail_atr or pos.breakeven_at_r or pos.max_bars):
                continue
            try:
                df = self.broker.candles(pos.symbol, self.config.timeframe, 60)
            except MT5Error:
                continue
            if df is None or len(df) < 2:
                continue
            last = df.iloc[-2]
            atr_series = _atr(df, 14)
            atr_val = float(atr_series.iloc[-2]) if len(atr_series) else None
            before = pos.stop_loss
            decision = manage_bar(pos, float(last.high), float(last.low),
                                  float(last.close), atr_val)
            if decision is not None:
                try:
                    t = self.broker.close(pos, reason=decision.reason)
                    self.risk.record_result(pos.symbol, t.pnl, now)
                    self._emit("close", f"Closed {pos.symbol} {pos.side.value} "
                                        f"({decision.reason}, P&L {t.pnl:+.2f})")
                except MT5Error as exc:
                    self._emit("error", f"close failed {pos.symbol}: {exc}")
                continue
            if pos.stop_loss is not None and pos.stop_loss != before:
                sent = self._stop_sent.get(pos.id)
                if sent is None or abs(sent - pos.stop_loss) > 1e-9:
                    try:
                        self.broker.modify_stop(pos, pos.stop_loss)
                        self._stop_sent[pos.id] = pos.stop_loss
                        self._emit("info", f"{pos.symbol} stop -> {pos.stop_loss:.5f}")
                    except MT5Error as exc:
                        self._emit("warn", f"stop update rejected {pos.symbol}: {exc}")

    # ------------------------------------------------------------------
    def _on_closed_bar(self, symbol: str, df, now: datetime, equity: float) -> None:
        strat = self.strategies[symbol]
        if len(df) < strat.warmup + 2:
            log.debug("%s warming up (%d/%d bars)", symbol, len(df), strat.warmup)
            return
        prepared = strat.prepare(df)
        prepared.attrs["symbol"] = symbol
        prepared.symbol_name = symbol
        pos = self.broker.position_for(symbol)
        signal = strat.on_bar(prepared, pos)

        if pos is not None and signal.type is SignalType.CLOSE:
            trade = self.broker.close(pos, reason=signal.reason or "signal")
            self.risk.record_result(symbol, trade.pnl, now)
            self._emit("close", f"Closed {symbol} {trade.side.value} "
                                f"P&L {trade.pnl:+.2f} {self.broker.currency}")
            return

        if pos is None and signal.is_entry:
            ok, why = self.risk.can_open(
                equity, len(self.broker.positions), now.date(), symbol=symbol,
                open_symbols=[p.symbol for p in self.broker.positions.values()],
                portfolio_risk=self._portfolio_risk(equity), now=now)
            if not ok:
                self._emit("blocked", f"{signal.type.value} {symbol} blocked: {why}")
                return
            price = self.broker.price(symbol)
            # The signal was computed on a bar that has since closed. If the
            # market has already run through the stop, the setup is gone: size
            # from a stop that is now inside the spread would be meaningless and
            # the broker would reject the order anyway.
            if signal.stop_loss:
                past = ((signal.side is Side.BUY and price <= signal.stop_loss)
                        or (signal.side is Side.SELL and price >= signal.stop_loss))
                if past:
                    self._emit("skip", f"{symbol}: price moved past the stop "
                                       f"({price:.5f} vs {signal.stop_loss:.5f}) — setup gone")
                    return
            units = self.risk.position_size(equity, price, signal.stop_loss,
                                            get_instrument(symbol), signal.confidence)
            if units <= 0:
                return
            try:
                p = self.broker.open(symbol, signal.side, units, price, now,
                                     signal.stop_loss, signal.take_profit, strat.name,
                                     trail_atr=signal.trail_atr,
                                     breakeven_at_r=signal.breakeven_at_r,
                                     max_bars=signal.max_bars)
            except (OrderRejected, StaleSignal) as exc:
                self._emit("skip", str(exc))
                return
            if p:
                self._known_tickets.add(p.id)
                self._ticket_symbols[p.id] = symbol
                # Cache the details NOW. A position can be opened and stopped
                # out by the broker between two position snapshots, in which
                # case it never appears in one and reconciliation would have
                # nothing to rebuild the trade from.
                self._open_positions[p.id] = p
                self._emit("open", f"Opened {symbol} {p.side.value} "
                                   f"{self.broker.units_to_lots(symbol, p.units)} lots @ "
                                   f"{p.entry_price:.5f} SL {signal.stop_loss} "
                                   f"TP {signal.take_profit} — {signal.reason}")

    def _portfolio_risk(self, equity: float) -> float:
        total = 0.0
        for p in self.broker.positions.values():
            if p.stop_loss is None:
                continue
            inst = get_instrument(p.symbol)
            total += abs(p.entry_price - p.stop_loss) * p.units * inst.usd_per_quote()
        return total / equity if equity > 0 else 0.0

    # ------------------------------------------------------------------
    def _emit(self, kind: str, message: str, data: Optional[dict] = None) -> None:
        ev = {"ts": utcnow().isoformat(), "kind": kind, "message": message,
              "data": data or {}}
        self.events.insert(0, ev)
        del self.events[200:]
        log.info("%s | %s", kind.upper(), message)
        if self.config.journal_path:
            try:
                import json
                from pathlib import Path
                p = Path(self.config.journal_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a") as fh:
                    fh.write(json.dumps(ev) + "\n")
            except OSError:
                pass

    def state(self) -> dict:
        acct = self.broker.account_summary()
        positions = []
        for p in self.broker.positions.values():
            px = self.broker.price(p.symbol)
            inst = get_instrument(p.symbol)
            d = p.to_dict()
            d.update(current_price=px,
                     unrealized=round(p.unrealized_pnl(px), 2),
                     pips=round(inst.pips((px - p.entry_price) * p.side.sign), 1),
                     r_multiple=round(p.r_multiple(px), 2) if p.r_multiple(px) else None)
            positions.append(d)
        trades = [t.to_dict() for t in self.broker.trades[-100:]][::-1]
        wins = sum(1 for t in self.broker.trades if t.is_win)
        n = len(self.broker.trades)
        return {
            "running": self.running,
            "mode": "sim" if self.config.sim else ("dry-run" if self.config.dry_run else "live"),
            "started_at": self.start_ts.isoformat() if self.start_ts else None,
            "account": acct,
            "strategy": self.config.strategy,
            "timeframe": self.config.timeframe,
            "symbols": self.config.symbols,
            "positions": positions,
            "trades": trades,
            "events": self.events[:60],
            "equity": acct["equity"],
            "balance": acct["balance"],
            "pnl": round(acct["equity"] - self.start_equity, 2),
            "pnl_pct": round((acct["equity"] / self.start_equity - 1) * 100, 3),
            "stats": {
                "trades": n, "wins": wins, "losses": n - wins,
                "win_rate": round(wins / n * 100, 1) if n else 0.0,
                "avg_r": round(sum(t.r_multiple or 0 for t in self.broker.trades) / n, 2) if n else 0.0,
                "total_pnl": round(sum(t.pnl for t in self.broker.trades), 2),
                "risk": self.risk.summary(acct["equity"]),
            },
            "equity_curve": [{"t": t.isoformat(), "equity": round(e, 2)}
                             for t, e in self.equity_history[-600:]],
        }


def _atr(df, period: int = 14):
    from ..indicators import atr
    return atr(df["high"], df["low"], df["close"], period)
