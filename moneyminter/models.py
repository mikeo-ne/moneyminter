"""Core domain models for Money Minter."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
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
    """A trading decision for one symbol.

    Beyond direction, a signal may request position *management*: an initial
    stop, a target, an ATR trailing distance and a maximum holding time. The
    trade manager (``moneyminter.management``) applies these uniformly in the
    backtester, the paper engine and the live/demo runner so a strategy behaves
    identically in all three.
    """

    type: SignalType
    symbol: str
    confidence: float = 1.0
    reason: str = ""
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    #: trail the stop this many ATRs behind the best price seen (None = off)
    trail_atr: Optional[float] = None
    #: move the stop to break-even once the trade is this many R in profit
    breakeven_at_r: Optional[float] = None
    #: close the trade after this many bars regardless of P&L
    max_bars: Optional[int] = None

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
    # ---- management state (used by the trailing-stop engine) -------------
    initial_stop: Optional[float] = None
    trail_atr: Optional[float] = None
    breakeven_at_r: Optional[float] = None
    max_bars: Optional[int] = None
    bars_held: int = 0
    high_water: float = 0.0
    low_water: float = 0.0
    #: highest favourable excursion in R (for reporting)
    mfe_r: float = 0.0
    #: adverse excursion in R
    mae_r: float = 0.0
    #: exit price for server-side closes, recorded during reconciliation
    closed_exit_price: Optional[float] = None
    closed_pnl: Optional[float] = None

    def __post_init__(self) -> None:
        if self.initial_stop is None:
            self.initial_stop = self.stop_loss
        if not self.high_water:
            self.high_water = self.entry_price
        if not self.low_water:
            self.low_water = self.entry_price

    # ---- risk geometry ---------------------------------------------------
    @property
    def risk_per_unit(self) -> Optional[float]:
        """Distance from entry to the original stop (always positive)."""
        if self.initial_stop is None:
            return None
        return abs(self.entry_price - self.initial_stop)

    def r_multiple(self, price: float) -> Optional[float]:
        """Profit at ``price`` expressed in units of initial risk."""
        base = self.risk_per_unit
        if not base:
            return None
        return (price - self.entry_price) * self.side.sign / base

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.entry_price) * self.side.sign * self.units

    # ---- excursion tracking ---------------------------------------------
    def observe(self, high: float, low: float) -> None:
        """Update the water marks with a new bar/tick range."""
        best = high if self.side is Side.BUY else low
        worst = low if self.side is Side.BUY else high
        if self.side is Side.BUY:
            self.high_water = max(self.high_water, best)
            self.low_water = min(self.low_water, worst)
        else:
            self.low_water = min(self.low_water, best)
            self.high_water = max(self.high_water, worst)
        r = self.r_multiple(best)
        if r is not None:
            self.mfe_r = max(self.mfe_r, r)
        rw = self.r_multiple(worst)
        if rw is not None:
            self.mae_r = min(self.mae_r, rw)

    def hit_stop(self, high: float, low: float) -> Optional[float]:
        """Return the exit price if SL/TP was touched inside the bar.

        When a bar straddles both levels we assume the stop is hit first — the
        pessimistic assumption that keeps backtests honest.
        """
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
        for k in ("stop_loss", "take_profit", "initial_stop", "closed_exit_price", "closed_pnl"):
            if d.get(k) is not None:
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
    initial_stop: Optional[float] = None
    bars_held: int = 0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    commission: float = 0.0
    swap: float = 0.0

    @property
    def is_win(self) -> bool:
        return bool(self.pnl > 0)

    @property
    def is_long(self) -> Optional[bool]:
        return None if self.side is None else self.side is Side.BUY

    @property
    def duration_s(self) -> float:
        return (self.closed_at - self.opened_at).total_seconds()

    @property
    def r_multiple(self) -> Optional[float]:
        if self.initial_stop is None:
            return None
        risk = abs(self.entry_price - self.initial_stop) * self.units
        if risk <= 0:
            return None
        return self.pnl / risk

    def to_dict(self) -> dict:
        d = asdict(self)
        d["side"] = self.side.value if self.side is not None else "?"
        d["opened_at"] = self.opened_at.isoformat()
        d["closed_at"] = self.closed_at.isoformat()
        d["is_win"] = self.is_win
        d["r_multiple"] = round(self.r_multiple, 3) if self.r_multiple is not None else None
        for k in ("pnl", "units", "entry_price", "exit_price"):
            d[k] = float(d[k])
        return d


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
