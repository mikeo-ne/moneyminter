"""Broker abstraction + a realistic paper broker.

The paper broker deliberately models the things that quietly destroy paper
strategies in production:

* the **spread** paid on entry *and* exit,
* **slippage** that is worse on stops than on targets (gaps overshoot),
* **commission** per notional,
* **swap** for positions held across rollover,
* **lot-grid snapping** so a size that would be skipped by an MT5 broker is
  skipped here too.

That last point matters: if the paper broker happily opens 0.003 lots but the
demo account's minimum is 0.01, a strategy "passes" testing and then cannot
trade. Sizes are snapped to the instrument's lot grid and rejected below the
minimum, exactly like ``MT5Broker``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from .instruments import Instrument, get_instrument
from .models import Position, Side, Trade, utcnow


@dataclass
class ExecutionConfig:
    spread_multiplier: float = 1.0        # scale the instrument's typical spread
    slippage_pips: float = 0.15
    #: extra slippage on stop-outs (stops run through the book in fast markets)
    stop_slippage_pips: float = 0.5
    commission_per_million: float = 35.0  # round-turn USD per $1M notional
    swap_pips_per_day: float = -0.15
    #: swap is triple on Wednesday (spot value date rolls over the weekend)
    wednesday_triple_swap: bool = True
    #: model a gap-through-the-stop when a bar opens beyond it
    gap_fills: bool = True


class Broker(ABC):
    @abstractmethod
    def open(self, symbol: str, side: Side, units: float, price: float, ts: datetime,
             stop_loss=None, take_profit=None, strategy: str = "",
             **mgmt) -> Optional[Position]: ...

    @abstractmethod
    def close(self, position: Position, price: float, ts: datetime, reason: str = "") -> Trade: ...


class PaperBroker(Broker):
    """Simulated broker used for both backtests and paper trading."""

    def __init__(self, balance: float = 10_000.0, execution: Optional[ExecutionConfig] = None,
                 enforce_broker_grid: bool = True):
        self.initial_balance = float(balance)
        self.balance = float(balance)
        self.execution = execution or ExecutionConfig()
        self.enforce_broker_grid = enforce_broker_grid
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.equity_curve: List[tuple[datetime, float]] = []
        self.rejected: List[dict] = []

    # ---- helpers -------------------------------------------------------
    def _inst(self, symbol: str) -> Instrument:
        return get_instrument(symbol)

    def fill_price(self, symbol: str, side: Side, mid: float, closing: bool = False,
                   stop_out: bool = False) -> float:
        """Mid price + half spread + slippage, always against the trader."""
        inst = self._inst(symbol)
        half_spread = inst.pip * inst.typical_spread_pips * self.execution.spread_multiplier / 2
        slip_pips = self.execution.slippage_pips
        if stop_out:
            slip_pips += self.execution.stop_slippage_pips
        direction = side.sign if not closing else -side.sign
        return mid + direction * (half_spread + inst.pip * slip_pips)

    def _commission(self, symbol: str, units: float, price: float) -> float:
        notional = units * price
        return notional / 1_000_000 * self.execution.commission_per_million

    def _swap(self, position: Position, closed_at: datetime) -> float:
        days = (closed_at - position.opened_at).total_seconds() / 86400.0
        if days <= 0:
            return 0.0
        inst = self._inst(position.symbol)
        rollovers = int(days)
        if self.execution.wednesday_triple_swap:
            # count how many of those rollovers were Wednesdays
            extra = 0
            base = position.opened_at
            for d in range(1, rollovers + 1):
                from datetime import timedelta
                if (base + timedelta(days=d)).weekday() == 2:
                    extra += 2
            rollovers += extra
        per_day = self.execution.swap_pips_per_day * inst.pip * position.units
        return rollovers * per_day

    def snap_lots(self, symbol: str, units: float) -> float:
        """Convert units to broker lots, or 0.0 when below the minimum."""
        inst = self._inst(symbol)
        if not self.enforce_broker_grid:
            return units
        lots = inst.round_lots(units / inst.contract_size, mode="down")
        if lots < inst.min_lot:
            return 0.0
        return lots * inst.contract_size

    # ---- api -----------------------------------------------------------
    def open(self, symbol, side, units, price, ts=None, stop_loss=None, take_profit=None,
             strategy="", **mgmt):
        if units <= 0:
            return None
        snapped = self.snap_lots(symbol, units)
        if snapped <= 0:
            inst = self._inst(symbol)
            self.rejected.append({"symbol": symbol, "reason": "below broker minimum lot",
                                  "wanted_lots": round(units / inst.contract_size, 4),
                                  "min_lot": inst.min_lot})
            return None
        ts = ts or utcnow()
        fill = self.fill_price(symbol, side, price)
        pos = Position(symbol=symbol, side=side, units=snapped, entry_price=fill,
                       opened_at=ts, stop_loss=stop_loss, take_profit=take_profit,
                       strategy=strategy, initial_stop=stop_loss,
                       trail_atr=mgmt.get("trail_atr"),
                       breakeven_at_r=mgmt.get("breakeven_at_r"),
                       max_bars=mgmt.get("max_bars"),
                       high_water=fill, low_water=fill)
        self.positions[pos.id] = pos
        return pos

    def close(self, position, price, ts=None, reason=""):
        ts = ts or utcnow()
        stop_out = reason in ("stop loss", "trailing stop", "break-even stop")
        fill = self.fill_price(position.symbol, position.side, price, closing=True,
                               stop_out=stop_out * self.execution.gap_fills)
        gross = (fill - position.entry_price) * position.side.sign * position.units
        commission = self._commission(position.symbol, position.units, fill)
        swap = self._swap(position, ts)
        pnl = gross + swap - commission
        self.balance += pnl
        self.positions.pop(position.id, None)
        trade = Trade(position.symbol, position.side, position.units, position.entry_price,
                      fill, position.opened_at, ts, pnl, reason, position.strategy,
                      initial_stop=position.initial_stop,
                      bars_held=position.bars_held, mfe_r=position.mfe_r,
                      mae_r=position.mae_r, commission=commission, swap=swap)
        self.trades.append(trade)
        return trade

    def equity(self, prices: Dict[str, float]) -> float:
        eq = self.balance
        for p in self.positions.values():
            px = prices.get(p.symbol)
            if px is not None:
                eq += p.unrealized_pnl(px)
        return eq

    def open_risk(self, equity: float) -> float:
        """Sum of the risk still open, as a fraction of equity."""
        total = 0.0
        for p in self.positions.values():
            risk_per_unit = p.risk_per_unit
            if risk_per_unit is None:
                continue
            # remaining risk from *current* stop, floored at zero (a trailed
            # stop past entry means the trade can no longer lose)
            stop = p.stop_loss
            if stop is None:
                continue
            remaining = abs(p.entry_price - stop) * p.units if stop else 0.0
            total += remaining
        return total / equity if equity > 0 else 0.0

    def position_for(self, symbol: str, strategy: str | None = None) -> Optional[Position]:
        for p in self.positions.values():
            if p.symbol == symbol and (strategy is None or p.strategy == strategy):
                return p
        return None

    def mark(self, ts: datetime, prices: Dict[str, float]) -> float:
        eq = self.equity(prices)
        self.equity_curve.append((ts, eq))
        return eq
