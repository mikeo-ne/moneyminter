"""Position sizing and risk guardrails.

Sizing rule (unchanged in spirit, corrected in units)::

    risk_cash   = equity * risk_per_trade
    units       = risk_cash / (|entry - stop| * pip_value_per_unit_in_USD)

The ``pip_value_per_unit`` term is what makes a JPY pair or gold size correctly.
The result is then snapped down to the broker's lot grid; if the snapped size is
below the minimum lot the trade is **skipped** rather than rounded up, because
rounding up can multiply the intended risk several-fold on a small account.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Iterable, Optional

from .instruments import Instrument

#: groups of pairs whose USD legs make them move together — used to avoid
#: stacking what is effectively one bet, three times over
CORRELATION_GROUPS = {
    "usd_short": {"EUR/USD", "GBP/USD", "AUD/USD", "NZD/USD"},   # long = short USD
    "usd_long": {"USD/JPY", "USD/CHF", "USD/CAD"},               # long = long USD
    "eur": {"EUR/USD", "EUR/JPY", "EUR/GBP"},
    "gbp": {"GBP/USD", "GBP/JPY", "EUR/GBP"},
    "jpy": {"USD/JPY", "EUR/JPY", "GBP/JPY"},
}


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.005       # 0.5% of equity per trade
    max_positions: int = 3
    max_daily_loss: float = 0.03        # stop trading after -3% on the day
    max_weekly_loss: float = 0.07
    max_drawdown: float = 0.15          # kill switch from the equity peak
    max_leverage: float = 30.0
    min_units: float = 1.0
    max_units: float = 20_000_000.0
    use_confidence_sizing: bool = True
    #: refuse two positions from the same currency bloc
    max_per_currency_group: int = 1
    #: cap on total open risk (sum of per-trade risk) as a share of equity
    max_portfolio_risk: float = 0.03
    #: pause after this many consecutive losses on a symbol
    loss_streak_pause: int = 4
    #: minutes to stay flat after the pause triggers
    pause_minutes: int = 240


@dataclass
class RiskManager:
    config: RiskConfig = field(default_factory=RiskConfig)
    _day: Optional[date] = None
    _week: Optional[tuple] = None
    _day_start_equity: float = 0.0
    _week_start_equity: float = 0.0
    peak_equity: float = 0.0
    halted_reason: Optional[str] = None
    loss_streaks: Dict[str, int] = field(default_factory=dict)
    paused_until: Dict[str, object] = field(default_factory=dict)

    # ---- sizing ---------------------------------------------------------
    def risk_cash(self, equity: float, confidence: float = 1.0) -> float:
        cash = equity * self.config.risk_per_trade
        if self.config.use_confidence_sizing:
            cash *= max(0.25, min(confidence, 1.5))
        return cash

    def position_size(self, equity: float, entry: float, stop: Optional[float],
                      instrument: Instrument, confidence: float = 1.0) -> float:
        """Units of base currency sized so a stop-out loses ``risk_per_trade``."""
        cash = self.risk_cash(equity, confidence)
        if stop is None or abs(entry - stop) < instrument.pip / 10:
            stop_dist = instrument.pip * 20
        else:
            stop_dist = abs(entry - stop)
        # value of a 1.0 price move on one unit of base currency, in USD
        usd_per_unit = instrument.usd_per_quote()
        if usd_per_unit <= 0 or entry <= 0:
            return 0.0
        units = cash / (stop_dist * usd_per_unit)
        # Never exceed the leverage ceiling on *notional in USD*.
        #   units x price     -> exposure in the quote currency
        #   x usd_per_quote   -> exposure in USD
        # Getting this wrong over-sizes USD-base pairs by the FX rate.
        usd_notional_per_unit = entry * usd_per_unit
        max_by_leverage = (equity * self.config.max_leverage
                           / max(usd_notional_per_unit, 1e-9))
        units = min(units, max_by_leverage, self.config.max_units)
        return float(max(0.0, units)) if units >= self.config.min_units else 0.0

    def position_lots(self, equity: float, entry: float, stop: Optional[float],
                      instrument: Instrument, confidence: float = 1.0) -> float:
        """Risk-based size expressed in broker lots (already step-rounded down)."""
        units = self.position_size(equity, entry, stop, instrument, confidence)
        return instrument.round_lots(units / instrument.contract_size, mode="down")

    # ---- guardrails -----------------------------------------------------
    def update_equity(self, equity: float, today: date) -> None:
        if self._day != today:
            self._day = today
            self._day_start_equity = equity
            week = today.isocalendar()[:2]
            if self._week != week:
                self._week = week
                self._week_start_equity = equity
            if self.halted_reason == "daily loss limit":
                self.halted_reason = None
        self.peak_equity = max(self.peak_equity, equity)

    def daily_pnl_pct(self, equity: float) -> float:
        if not self._day_start_equity:
            return 0.0
        return (equity - self._day_start_equity) / self._day_start_equity

    def weekly_pnl_pct(self, equity: float) -> float:
        if not self._week_start_equity:
            return 0.0
        return (equity - self._week_start_equity) / self._week_start_equity

    def drawdown_pct(self, equity: float) -> float:
        if not self.peak_equity:
            return 0.0
        return (self.peak_equity - equity) / self.peak_equity

    # ---- trade outcomes -------------------------------------------------
    def record_result(self, symbol: str, pnl: float, now=None) -> None:
        """Feed a closed trade back so the loss-streak brake can work."""
        if pnl < 0:
            self.loss_streaks[symbol] = self.loss_streaks.get(symbol, 0) + 1
            if (self.config.loss_streak_pause
                    and self.loss_streaks[symbol] >= self.config.loss_streak_pause
                    and now is not None):
                from datetime import timedelta
                self.paused_until[symbol] = now + timedelta(minutes=self.config.pause_minutes)
                self.loss_streaks[symbol] = 0
        else:
            self.loss_streaks[symbol] = 0

    def is_paused(self, symbol: str, now=None) -> bool:
        until = self.paused_until.get(symbol)
        if until is None or now is None:
            return False
        return now < until

    # ---- gate -----------------------------------------------------------
    def can_open(self, equity: float, open_positions: int, today: date,
                 symbol: Optional[str] = None,
                 open_symbols: Iterable[str] = (),
                 portfolio_risk: float = 0.0,
                 now=None) -> tuple[bool, str]:
        self.update_equity(equity, today)
        if self.drawdown_pct(equity) >= self.config.max_drawdown:
            self.halted_reason = "max drawdown"
            return False, "max drawdown reached — trading halted"
        if self.daily_pnl_pct(equity) <= -self.config.max_daily_loss:
            self.halted_reason = "daily loss limit"
            return False, "daily loss limit reached"
        if self.weekly_pnl_pct(equity) <= -self.config.max_weekly_loss:
            self.halted_reason = "weekly loss limit"
            return False, "weekly loss limit reached"
        if self.halted_reason:
            return False, self.halted_reason
        if open_positions >= self.config.max_positions:
            return False, "max concurrent positions"
        if portfolio_risk >= self.config.max_portfolio_risk:
            return False, "portfolio risk cap"
        if symbol:
            if self.is_paused(symbol, now):
                return False, f"{symbol} paused after a losing streak"
            if symbol in set(open_symbols):
                return False, "already in a position on this symbol"
            if self.config.max_per_currency_group:
                for group, members in CORRELATION_GROUPS.items():
                    if symbol in members:
                        same = [s for s in open_symbols if s in members and s != symbol]
                        if len(same) >= self.config.max_per_currency_group:
                            return False, f"correlated exposure ({group})"
        return True, "ok"

    def summary(self, equity: float) -> dict:
        return {
            "risk_per_trade": self.config.risk_per_trade,
            "drawdown_pct": round(self.drawdown_pct(equity) * 100, 2),
            "daily_pnl_pct": round(self.daily_pnl_pct(equity) * 100, 2),
            "weekly_pnl_pct": round(self.weekly_pnl_pct(equity) * 100, 2),
            "halted": self.halted_reason,
            "loss_streaks": dict(self.loss_streaks),
        }
