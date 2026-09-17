"""Position sizing and risk guardrails."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .instruments import Instrument


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.01        # 1% of equity per trade
    max_positions: int = 3
    max_daily_loss: float = 0.05        # stop trading after -5% on the day
    max_drawdown: float = 0.25          # kill switch
    max_leverage: float = 30.0
    min_units: float = 100.0
    max_units: float = 5_000_000.0
    use_confidence_sizing: bool = True


@dataclass
class RiskManager:
    config: RiskConfig = field(default_factory=RiskConfig)
    _day: Optional[date] = None
    _day_start_equity: float = 0.0
    peak_equity: float = 0.0
    halted_reason: Optional[str] = None

    # ---- sizing -------------------------------------------------------
    def position_size(self, equity: float, entry: float, stop: Optional[float],
                      instrument: Instrument, confidence: float = 1.0) -> float:
        """Units sized so that hitting the stop loses ``risk_per_trade`` of equity."""
        risk_cash = equity * self.config.risk_per_trade
        if self.config.use_confidence_sizing:
            risk_cash *= max(0.25, min(confidence, 1.5))
        if stop is None or abs(entry - stop) < instrument.pip / 10:
            stop_dist = instrument.pip * 20
        else:
            stop_dist = abs(entry - stop)
        units = risk_cash / stop_dist
        max_by_leverage = equity * self.config.max_leverage / max(entry, 1e-9)
        units = min(units, max_by_leverage, self.config.max_units)
        return float(max(0.0, round(units)))if units >= self.config.min_units else 0.0

    # ---- guardrails ---------------------------------------------------
    def update_equity(self, equity: float, today: date) -> None:
        if self._day != today:
            self._day = today
            self._day_start_equity = equity
            if self.halted_reason == "daily loss limit":
                self.halted_reason = None
        self.peak_equity = max(self.peak_equity, equity)

    def daily_pnl_pct(self, equity: float) -> float:
        if not self._day_start_equity:
            return 0.0
        return (equity - self._day_start_equity) / self._day_start_equity

    def drawdown_pct(self, equity: float) -> float:
        if not self.peak_equity:
            return 0.0
        return (self.peak_equity - equity) / self.peak_equity

    def can_open(self, equity: float, open_positions: int, today: date) -> tuple[bool, str]:
        self.update_equity(equity, today)
        if self.drawdown_pct(equity) >= self.config.max_drawdown:
            self.halted_reason = "max drawdown"
            return False, "max drawdown reached — trading halted"
        if self.daily_pnl_pct(equity) <= -self.config.max_daily_loss:
            self.halted_reason = "daily loss limit"
            return False, "daily loss limit reached"
        if open_positions >= self.config.max_positions:
            return False, "max concurrent positions"
        if self.halted_reason:
            return False, self.halted_reason
        return True, "ok"
