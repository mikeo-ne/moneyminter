"""Event-driven backtester with intrabar stop/target handling."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from . import metrics as mx
from .broker import ExecutionConfig, PaperBroker
from .data import CandleFeed, load_feed
from .data.feeds import timeframe_seconds
from .instruments import get_instrument
from .models import Side, SignalType, Trade
from .risk import RiskConfig, RiskManager
from .strategies import Strategy, get_strategy


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    strategy: str
    metrics: mx.Metrics
    trades: List[Trade]
    equity_curve: List[tuple]
    candles: pd.DataFrame = field(repr=False, default_factory=pd.DataFrame)

    def to_dict(self, max_points: int = 1200) -> dict:
        step = max(1, len(self.equity_curve) // max_points)
        return {
            "symbol": self.symbol, "timeframe": self.timeframe, "strategy": self.strategy,
            "metrics": self.metrics.to_dict(),
            "trades": [t.to_dict() for t in self.trades],
            "equity_curve": [{"t": t.isoformat(), "equity": round(e, 2)}
                             for t, e in self.equity_curve[::step]],
            "candles": [
                {"t": ts.isoformat(), "o": float(r.open), "h": float(r.high),
                 "l": float(r.low), "c": float(r.close)}
                for ts, r in self.candles.tail(600).iterrows()
            ] if len(self.candles) else [],
        }

    def print_report(self) -> None:
        print(f"\n=== Money Minter backtest — {self.strategy} on {self.symbol} {self.timeframe} ===")
        print(self.metrics.summary())


def run_backtest(symbol: str = "EUR/USD", timeframe: str = "H1", strategy: str = "ma_crossover",
                 balance: float = 10_000.0, source: str = "synthetic", bars: int = 3000,
                 path: str | None = None, seed: int | None = 7,
                 risk: Optional[RiskConfig] = None, execution: Optional[ExecutionConfig] = None,
                 strategy_params: Optional[dict] = None,
                 feed: Optional[CandleFeed] = None) -> BacktestResult:
    feed = feed or load_feed(symbol, timeframe, source, bars=bars, path=path, seed=seed)
    strat: Strategy = get_strategy(strategy, **(strategy_params or {}))
    broker = PaperBroker(balance, execution)
    rm = RiskManager(risk or RiskConfig())
    inst = get_instrument(feed.symbol)

    df = strat.prepare(feed.df)
    df.symbol_name = feed.symbol
    bars_in_market = 0

    for i in range(strat.warmup, len(df)):
        window = df.iloc[: i + 1]
        window.symbol_name = feed.symbol
        row = window.iloc[-1]
        ts = window.index[-1].to_pydatetime()
        price = float(row.close)

        pos = broker.position_for(feed.symbol)

        # 1. intrabar stop-loss / take-profit
        if pos is not None:
            exit_px = pos.hit_stop(float(row.high), float(row.low))
            if exit_px is not None:
                reason = "stop loss" if (
                    pos.stop_loss is not None and abs(exit_px - pos.stop_loss) < 1e-12) else "take profit"
                broker.close(pos, exit_px, ts, reason)
                pos = None

        # 2. strategy decision
        signal = strat.on_bar(window, pos)
        if pos is not None and signal.type is SignalType.CLOSE:
            broker.close(pos, price, ts, signal.reason or "signal")
            pos = None
        elif pos is None and signal.is_entry:
            ok, _why = rm.can_open(broker.equity({feed.symbol: price}), len(broker.positions), ts.date())
            if ok:
                units = rm.position_size(broker.equity({feed.symbol: price}), price,
                                         signal.stop_loss, inst, signal.confidence)
                if units > 0:
                    pos = broker.open(feed.symbol, signal.side, units, price, ts,
                                      signal.stop_loss, signal.take_profit, strat.name)

        if broker.positions:
            bars_in_market += 1
        broker.mark(ts, {feed.symbol: price})

    # close anything still open at the last price
    if broker.positions:
        last_ts = df.index[-1].to_pydatetime()
        last_px = float(df.close.iloc[-1])
        for p in list(broker.positions.values()):
            broker.close(p, last_px, last_ts, "end of data")
        broker.mark(last_ts, {feed.symbol: last_px})

    seconds = timeframe_seconds(feed.timeframe)
    bars_per_year = 365 * 24 * 3600 / seconds * (5 / 7)
    m = mx.compute(broker.trades, broker.equity_curve, balance, bars_per_year,
                   bars_in_market, max(1, len(df) - strat.warmup))
    return BacktestResult(feed.symbol, feed.timeframe, strat.name, m,
                          broker.trades, broker.equity_curve, feed.df)


def optimize(symbol: str, timeframe: str, strategy: str, grid: dict, metric: str = "sharpe",
             **kw) -> list[dict]:
    """Simple exhaustive grid search; returns results sorted by ``metric`` desc."""
    import itertools

    feed = kw.pop("feed", None) or load_feed(symbol, timeframe, kw.pop("source", "synthetic"),
                                             bars=kw.pop("bars", 3000), seed=kw.pop("seed", 7))
    keys = list(grid)
    out = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        params = dict(zip(keys, combo))
        try:
            res = run_backtest(symbol, timeframe, strategy, strategy_params=params, feed=feed, **kw)
        except Exception:  # noqa: BLE001
            continue
        out.append({"params": params, **res.metrics.to_dict()})
    out.sort(key=lambda r: (r.get(metric) or 0), reverse=True)
    return out


def walk_forward(symbol: str, timeframe: str, strategy: str, folds: int = 4, **kw) -> list[dict]:
    """Split the history into N sequential out-of-sample folds."""
    feed = load_feed(symbol, timeframe, kw.pop("source", "synthetic"),
                     bars=kw.pop("bars", 3000), seed=kw.pop("seed", 7))
    n = len(feed.df)
    size = n // folds
    results = []
    for f in range(folds):
        sub = CandleFeed(feed.symbol, feed.timeframe, feed.df.iloc[f * size:(f + 1) * size])
        res = run_backtest(symbol, timeframe, strategy, feed=sub, **kw)
        results.append({"fold": f + 1, "from": str(sub.df.index[0]), "to": str(sub.df.index[-1]),
                        **res.metrics.to_dict()})
    return results
