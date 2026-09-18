"""Core domain models for Money Minter."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
import uuid


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class SignalType(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    CLOSE = "CLOSE"
    FLAT = "FLAT"


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


@dataclass(frozen=True)
class Tick:
    timestamp: datetime
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass
class Signal:
    type: SignalType
    symbol: str
    confidence: float = 1.0
    reason: str = ""
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None

    @property
    def is_entry(self) -> bool:
        return self.type in (SignalType.LONG, SignalType.SHORT)

    @property
    def side(self) -> Optional[Side]:
        if self.type is SignalType.LONG:
            return Side.BUY
        if self.type is SignalType.SHORT:
            return Side.SELL
        return None


@dataclass
class Position:
    symbol: str
    side: Side
    units: float
    entry_price: float
    opened_at: datetime
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    strategy: str = ""

    def unrealized_pnl(self, price: float, pip_value: float = 1.0) -> float:
        return (price - self.entry_price) * self.side.sign * self.units

    def hit_stop(self, high: float, low: float) -> Optional[float]:
        """Return the exit price if SL/TP was touched inside the bar."""
        if self.side is Side.BUY:
            if self.stop_loss is not None and low <= self.stop_loss:
                return self.stop_loss
            if self.take_profit is not None and high >= self.take_profit:
                return self.take_profit
        else:
            if self.stop_loss is not None and high >= self.stop_loss:
                return self.stop_loss
            if self.take_profit is not None and low <= self.take_profit:
                return self.take_profit
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["side"] = self.side.value
        d["opened_at"] = self.opened_at.isoformat()
        d["units"] = float(self.units)
        d["entry_price"] = float(self.entry_price)
        for k in ("stop_loss", "take_profit"):
            if d[k] is not None:
                d[k] = float(d[k])
        return d


@dataclass
class Trade:
    symbol: str
    side: Side
    units: float
    entry_price: float
    exit_price: float
    opened_at: datetime
    closed_at: datetime
    pnl: float
    reason: str = ""
    strategy: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def is_win(self) -> bool:
        return bool(self.pnl > 0)

    @property
    def duration_s(self) -> float:
        return (self.closed_at - self.opened_at).total_seconds()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["side"] = self.side.value
        d["opened_at"] = self.opened_at.isoformat()
        d["closed_at"] = self.closed_at.isoformat()
        d["is_win"] = self.is_win
        d["pnl"] = float(self.pnl)
        d["units"] = float(self.units)
        d["entry_price"] = float(self.entry_price)
        d["exit_price"] = float(self.exit_price)
        return d


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
