"""Performance analytics."""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import List, Sequence, Tuple

import numpy as np

from .models import Trade


@dataclass
class Metrics:
    initial_balance: float
    final_equity: float
    net_profit: float
    return_pct: float
    trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    expectancy: float
    avg_win: float
    avg_loss: float
    largest_win: float
    largest_loss: float
    max_drawdown: float
    max_drawdown_pct: float
    sharpe: float
    sortino: float
    calmar: float
    avg_trade_hours: float
    exposure_pct: float

    def to_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}

    def summary(self) -> str:
        d = self.to_dict()
        rows = [
            ("Initial balance", f"${d['initial_balance']:,.2f}"),
            ("Final equity", f"${d['final_equity']:,.2f}"),
            ("Net profit", f"${d['net_profit']:,.2f} ({d['return_pct']:.2f}%)"),
            ("Trades", f"{d['trades']} ({d['wins']}W / {d['losses']}L)"),
            ("Win rate", f"{d['win_rate']:.2f}%"),
            ("Profit factor", f"{d['profit_factor']:.2f}"),
            ("Expectancy", f"${d['expectancy']:,.2f}/trade"),
            ("Avg win / loss", f"${d['avg_win']:,.2f} / ${d['avg_loss']:,.2f}"),
            ("Max drawdown", f"${d['max_drawdown']:,.2f} ({d['max_drawdown_pct']:.2f}%)"),
            ("Sharpe / Sortino", f"{d['sharpe']:.2f} / {d['sortino']:.2f}"),
            ("Calmar", f"{d['calmar']:.2f}"),
            ("Avg trade length", f"{d['avg_trade_hours']:.1f} h"),
            ("Time in market", f"{d['exposure_pct']:.1f}%"),
        ]
        w = max(len(a) for a, _ in rows)
        return "\n".join(f"  {a.ljust(w)} : {b}" for a, b in rows)


def compute(trades: Sequence[Trade], equity_curve: Sequence[Tuple], initial_balance: float,
            bars_per_year: float = 6240.0, bars_in_market: int = 0, total_bars: int = 0) -> Metrics:
    eq = np.array([e for _, e in equity_curve], dtype=float) if equity_curve else np.array([initial_balance])
    final = float(eq[-1])
    pnls = np.array([t.pnl for t in trades], dtype=float) if trades else np.array([])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]

    gross_win, gross_loss = float(wins.sum()), float(-losses.sum())
    pf = gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)

    running_max = np.maximum.accumulate(eq)
    dd = running_max - eq
    max_dd = float(dd.max()) if len(dd) else 0.0
    max_dd_pct = float((dd / np.where(running_max == 0, 1, running_max)).max() * 100) if len(dd) else 0.0

    rets = np.diff(eq) / np.where(eq[:-1] == 0, 1, eq[:-1]) if len(eq) > 1 else np.array([])
    ann = math.sqrt(bars_per_year)
    sharpe = float(rets.mean() / rets.std(ddof=1) * ann) if len(rets) > 1 and rets.std(ddof=1) > 0 else 0.0
    downside = rets[rets < 0]
    sortino = float(rets.mean() / downside.std(ddof=1) * ann) if len(downside) > 1 and downside.std(ddof=1) > 0 else 0.0
    ret_pct = (final - initial_balance) / initial_balance * 100
    calmar = float(ret_pct / max_dd_pct) if max_dd_pct > 0 else 0.0

    return Metrics(
        initial_balance=initial_balance, final_equity=final,
        net_profit=final - initial_balance, return_pct=ret_pct,
        trades=len(trades), wins=int(len(wins)), losses=int(len(losses)),
        win_rate=float(len(wins) / len(pnls) * 100) if len(pnls) else 0.0,
        profit_factor=pf,
        expectancy=float(pnls.mean()) if len(pnls) else 0.0,
        avg_win=float(wins.mean()) if len(wins) else 0.0,
        avg_loss=float(losses.mean()) if len(losses) else 0.0,
        largest_win=float(pnls.max()) if len(pnls) else 0.0,
        largest_loss=float(pnls.min()) if len(pnls) else 0.0,
        max_drawdown=max_dd, max_drawdown_pct=max_dd_pct,
        sharpe=sharpe, sortino=sortino, calmar=calmar,
        avg_trade_hours=float(np.mean([t.duration_s for t in trades]) / 3600) if trades else 0.0,
        exposure_pct=float(bars_in_market / total_bars * 100) if total_bars else 0.0,
    )
