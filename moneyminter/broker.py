"""Broker abstraction + a realistic paper broker (spread, slippage, commission, swap)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from .instruments import Instrument, get_instrument
from .models import Position, Side, Trade, utcnow


@dataclass
class ExecutionConfig:
    spread_multiplier: float = 1.0      # scale instrument typical spread
    slippage_pips: float = 0.2
    commission_per_million: float = 35.0  # round-turn USD
    swap_pips_per_day: float = -0.15


class Broker(ABC):
    @abstractmethod
    def open(self, symbol: str, side: Side, units: float, price: float, ts: datetime,
             stop_loss=None, take_profit=None, strategy: str = "") -> Optional[Position]: ...

    @abstractmethod
    def close(self, position: Position, price: float, ts: datetime, reason: str = "") -> Trade: ...


class PaperBroker(Broker):
    """Simulated broker used for both backtests and paper trading."""

    def __init__(self, balance: float = 10_000.0, execution: Optional[ExecutionConfig] = None):
        self.initial_balance = float(balance)
        self.balance = float(balance)
        self.execution = execution or ExecutionConfig()
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.equity_curve: List[tuple[datetime, float]] = []

    # ---- helpers -------------------------------------------------------
    def _inst(self, symbol: str) -> Instrument:
        return get_instrument(symbol)

    def fill_price(self, symbol: str, side: Side, mid: float, closing: bool = False) -> float:
        inst = self._inst(symbol)
        half_spread = inst.pip * inst.typical_spread_pips * self.execution.spread_multiplier / 2
        slip = inst.pip * self.execution.slippage_pips
        direction = side.sign if not closing else -side.sign
        return mid + direction * (half_spread + slip)

    def _commission(self, symbol: str, units: float, price: float) -> float:
        notional = units * price
        return notional / 1_000_000 * self.execution.commission_per_million

    def _swap(self, position: Position, closed_at: datetime) -> float:
        days = max(0.0, (closed_at - position.opened_at).total_seconds() / 86400.0)
        inst = self._inst(position.symbol)
        return days * self.execution.swap_pips_per_day * inst.pip * position.units

    # ---- api -----------------------------------------------------------
    def open(self, symbol, side, units, price, ts=None, stop_loss=None, take_profit=None, strategy=""):
        if units <= 0:
            return None
        ts = ts or utcnow()
        fill = self.fill_price(symbol, side, price)
        pos = Position(symbol=symbol, side=side, units=units, entry_price=fill, opened_at=ts,
                       stop_loss=stop_loss, take_profit=take_profit, strategy=strategy)
        self.positions[pos.id] = pos
        return pos

    def close(self, position, price, ts=None, reason=""):
        ts = ts or utcnow()
        fill = self.fill_price(position.symbol, position.side, price, closing=True)
        gross = (fill - position.entry_price) * position.side.sign * position.units
        # full round-turn commission is charged on close so trade.pnl is all-in
        pnl = gross + self._swap(position, ts) - self._commission(position.symbol, position.units, fill)
        self.balance += pnl
        self.positions.pop(position.id, None)
        trade = Trade(position.symbol, position.side, position.units, position.entry_price,
                      fill, position.opened_at, ts, pnl, reason, position.strategy)
        self.trades.append(trade)
        return trade

    def equity(self, prices: Dict[str, float]) -> float:
        eq = self.balance
        for p in self.positions.values():
            px = prices.get(p.symbol)
            if px is not None:
                eq += p.unrealized_pnl(px)
        return eq

    def position_for(self, symbol: str, strategy: str | None = None) -> Optional[Position]:
        for p in self.positions.values():
            if p.symbol == symbol and (strategy is None or p.strategy == strategy):
                return p
        return None

    def mark(self, ts: datetime, prices: Dict[str, float]) -> float:
        eq = self.equity(prices)
        self.equity_curve.append((ts, eq))
        return eq
