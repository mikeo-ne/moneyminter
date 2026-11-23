"""Position management: trailing stops, break-even, time exits, partials.

This module is deliberately broker-agnostic and side-effect free. Both the
backtester and the live/paper engines call it with the same inputs, so a
strategy's exit behaviour is identical whether it is being tested or traded.

The order of operations inside a bar matters and is fixed here to be
*conservative* (worst case for the trader):

1. observe the bar's excursion (updates water marks / MFE / MAE),
2. check the stop that was in force when the bar opened,
3. check the target,
4. check the time exit,
5. only then update the stop for the next bar (trail / break-even).

Updating the trail before checking the stop would let a bar tighten the stop and
then claim it was hit inside the same bar — a classic backtest mirage.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import Position, Side

#: reasons that are reported on the resulting Trade
STOP = "stop loss"
TRAIL = "trailing stop"
TARGET = "take profit"
TIME = "time exit"
BREAKEVEN = "break-even stop"


@dataclass
class ExitDecision:
    price: float
    reason: str


def _to_stop_side(position: Position, price: float) -> float:
    """Never let a trailing stop move backwards."""
    cur = position.stop_loss
    if cur is None:
        return price
    return max(cur, price) if position.side is Side.BUY else min(cur, price)


def stop_price_from_atr(position: Position, atr_value: float) -> Optional[float]:
    """Trail the stop to ``trail_atr`` ATRs behind the best price seen."""
    if not position.trail_atr or not atr_value or atr_value <= 0:
        return None
    if position.side is Side.BUY:
        return _to_stop_side(position, position.high_water - position.trail_atr * atr_value)
    return _to_stop_side(position, position.low_water + position.trail_atr * atr_value)


def manage_bar(position: Position, high: float, low: float, close: float,
               atr_value: Optional[float] = None) -> Optional[ExitDecision]:
    """Advance a position through one closed bar.

    Returns an ``ExitDecision`` when the bar closed the trade, otherwise
    ``None`` after updating the position's management state in place.
    """
    # 1. excursion bookkeeping (uses this bar's range)
    position.observe(high, low)
    position.bars_held += 1

    # 2. stop that was live at the open of the bar
    if position.stop_loss is not None:
        if position.side is Side.BUY and low <= position.stop_loss:
            reason = TRAIL if position.trail_atr else (
                BREAKEVEN if position.initial_stop and
                position.stop_loss != position.initial_stop else STOP)
            return ExitDecision(position.stop_loss, reason)
        if position.side is Side.SELL and high >= position.stop_loss:
            reason = TRAIL if position.trail_atr else (
                BREAKEVEN if position.initial_stop and
                position.stop_loss != position.initial_stop else STOP)
            return ExitDecision(position.stop_loss, reason)

    # 3. target
    if position.take_profit is not None:
        if position.side is Side.BUY and high >= position.take_profit:
            return ExitDecision(position.take_profit, TARGET)
        if position.side is Side.SELL and low <= position.take_profit:
            return ExitDecision(position.take_profit, TARGET)

    # 4. time stop
    if position.max_bars and position.bars_held >= position.max_bars:
        return ExitDecision(close, TIME)

    # 5. move the stop for the *next* bar
    r = position.r_multiple(close)
    if (position.breakeven_at_r is not None and r is not None
            and r >= position.breakeven_at_r):
        position.stop_loss = _to_stop_side(position, position.entry_price)
    if atr_value and position.trail_atr:
        new_stop = stop_price_from_atr(position, atr_value)
        if new_stop is not None:
            position.stop_loss = new_stop
    return None


def hard_breach(position: Position, bid: float, ask: float) -> Optional[ExitDecision]:
    """Tick-level stop check, used by the live runners between bars.

    The broker holds the real stop-loss order, so this is a backstop for the
    case where the terminal is briefly unable to execute it.
    """
    px = bid if position.side is Side.BUY else ask
    if position.stop_loss is None:
        return None
    if position.side is Side.BUY and px <= position.stop_loss:
        return ExitDecision(position.stop_loss, STOP)
    if position.side is Side.SELL and px >= position.stop_loss:
        return ExitDecision(position.stop_loss, STOP)
    return None
