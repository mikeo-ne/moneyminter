"""Event-driven backtester with intrabar stop/target/trail handling.

The critical property of this backtester is that it uses the *same*
``moneyminter.management`` code that the paper and live engines use. If a
trailing stop behaves one way in a backtest and another on the demo account,
every backtest number here is a lie — so there is exactly one implementation.

Fills are conservative:

* the stop in force at the *open* of a bar is the one that can be hit by it,
* a bar that straddles both stop and target resolves to the stop,
* spread, slippage (extra on stops), commission and swap are all charged.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from . import metrics as mx
from .broker import ExecutionConfig, PaperBroker
from .data import CandleFeed, load_feed
from .data.feeds import timeframe_seconds
from .instruments import get_instrument
from .management import manage_bar
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
    params: dict = field(default_factory=dict)

    def to_dict(self, max_points: int = 1200) -> dict:
        step = max(1, len(self.equity_curve) // max_points)
        return {
            "symbol": self.symbol, "timeframe": self.timeframe, "strategy": self.strategy,
            "params": self.params,
            "metrics": self.metrics.to_dict(),
            "trades": [t.to_dict() for t in self.trades][-500:],
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


def _simulate(feed: CandleFeed, strat: Strategy, balance: float,
              risk: Optional[RiskConfig], execution: Optional[ExecutionConfig],
              symbol_override: Optional[str] = None) -> tuple[PaperBroker, int, int]:
    """Run one symbol through a strategy. Returns (broker, bars_in_market, bars)."""
    broker = PaperBroker(balance, execution)
    rm = RiskManager(risk or RiskConfig())
    symbol = symbol_override or feed.symbol
    inst = get_instrument(symbol)

    df = strat.prepare(feed.df)
    df.attrs["symbol"] = symbol
    df.symbol_name = symbol
    bars_in_market = 0
    warm = min(strat.warmup, max(0, len(df) - 2))

    for i in range(warm, len(df)):
        window = df.iloc[: i + 1]
        window.attrs["symbol"] = symbol
        window.symbol_name = symbol
        row = window.iloc[-1]
        ts = window.index[-1].to_pydatetime()
        price = float(row.close)
        atr_val = float(row["atr"]) if "atr" in window.columns and pd.notna(row.get("atr")) else None
        equity = broker.equity({symbol: price})

        pos = broker.position_for(symbol)
        closed_this_bar = False

        # 1. manage the open position through this bar (stop -> target -> time)
        if pos is not None:
            decision = manage_bar(pos, float(row.high), float(row.low), price, atr_val)
            if decision is not None:
                trade = broker.close(pos, decision.price, ts, decision.reason)
                rm.record_result(symbol, trade.pnl, ts)
                pos = None
                closed_this_bar = True

        # 2. strategy decision on the closed bar
        signal = strat.on_bar(window, pos)
        if pos is not None and signal.type is SignalType.CLOSE:
            trade = broker.close(pos, price, ts, signal.reason or "signal")
            rm.record_result(symbol, trade.pnl, ts)
            pos = None
        elif pos is None and signal.is_entry and not closed_this_bar:
            equity = broker.equity({symbol: price})
            ok, _why = rm.can_open(equity, len(broker.positions), ts.date(),
                                   symbol=symbol,
                                   open_symbols=[p.symbol for p in broker.positions.values()],
                                   portfolio_risk=broker.open_risk(equity), now=ts)
            if ok:
                units = rm.position_size(equity, price, signal.stop_loss, inst,
                                         signal.confidence)
                if units > 0:
                    broker.open(symbol, signal.side, units, price, ts,
                                signal.stop_loss, signal.take_profit, strat.name,
                                trail_atr=signal.trail_atr,
                                breakeven_at_r=signal.breakeven_at_r,
                                max_bars=signal.max_bars)

        if broker.positions:
            bars_in_market += 1
        broker.mark(ts, {symbol: price})

    if broker.positions:
        last_ts = df.index[-1].to_pydatetime()
        last_px = float(df.close.iloc[-1])
        for p in list(broker.positions.values()):
            broker.close(p, last_px, last_ts, "end of data")
        broker.mark(last_ts, {symbol: last_px})

    return broker, bars_in_market, max(1, len(df) - warm)


def run_backtest(symbol: str = "EUR/USD", timeframe: str = "H1",
                 strategy: str = "trend_pullback", balance: float = 10_000.0,
                 source: str = "synthetic", bars: int = 3000, path: str | None = None,
                 seed: int | None = 7, risk: Optional[RiskConfig] = None,
                 execution: Optional[ExecutionConfig] = None,
                 strategy_params: Optional[dict] = None,
                 feed: Optional[CandleFeed] = None) -> BacktestResult:
    feed = feed or load_feed(symbol, timeframe, source, bars=bars, path=path, seed=seed)
    strat: Strategy = get_strategy(strategy, **(strategy_params or {}))
    broker, bars_in_market, total_bars = _simulate(feed, strat, balance, risk, execution)

    seconds = timeframe_seconds(feed.timeframe)
    bars_per_year = 365 * 24 * 3600 / seconds * (5 / 7)
    m = mx.compute(broker.trades, broker.equity_curve, balance, bars_per_year,
                   bars_in_market, total_bars)
    return BacktestResult(feed.symbol, feed.timeframe, strat.name, m, broker.trades,
                          broker.equity_curve, feed.df, params=strat.params)


def optimize(symbol: str, timeframe: str, strategy: str, grid: dict,
             metric: str = "sharpe", **kw) -> list[dict]:
    """Exhaustive grid search; results sorted by ``metric`` descending."""
    import itertools

    feed = kw.pop("feed", None) or load_feed(
        symbol, timeframe, kw.pop("source", "synthetic"),
        bars=kw.pop("bars", 3000), seed=kw.pop("seed", 7))
    keys = list(grid)
    out = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        params = dict(zip(keys, combo))
        try:
            res = run_backtest(symbol, timeframe, strategy, strategy_params=params,
                               feed=feed, **kw)
        except Exception:  # noqa: BLE001 - a bad parameter set is not fatal
            continue
        out.append({"params": params, **res.metrics.to_dict()})
    out.sort(key=lambda r: (r.get(metric) if r.get(metric) is not None else -1e9),
             reverse=True)
    return out


def walk_forward(symbol: str, timeframe: str, strategy: str, folds: int = 4,
                 optimize_params: Optional[dict] = None,
                 feed: Optional[CandleFeed] = None, **kw) -> list[dict]:
    """Anchored walk-forward with optional in-sample optimisation per fold.

    Without ``optimize_params`` this is a plain out-of-sample split. With it,
    each fold optimises on the preceding data and reports the result of trading
    those parameters on the *next*, unseen slice — which is the only number that
    means anything.
    """
    source = kw.pop("source", "synthetic")
    bars = kw.pop("bars", 3000)
    seed = kw.pop("seed", 7)
    feed = feed or load_feed(symbol, timeframe, source, bars=bars, seed=seed)
    n = len(feed.df)
    fold_size = n // (folds + 1)
    results = []
    for f in range(folds):
        train_end = fold_size * (f + 1)
        test_end = min(n, fold_size * (f + 2))
        train_df = feed.df.iloc[:train_end]
        test_df = feed.df.iloc[train_end:test_end]
        if len(test_df) < 60:
            continue
        # pull strategy_params out of kw so it cannot be passed twice below
        params = kw.pop("strategy_params", None) or {}
        if optimize_params:
            train_feed = CandleFeed(feed.symbol, feed.timeframe, train_df)
            ranked = optimize(symbol, timeframe, strategy, optimize_params,
                              feed=train_feed, **kw)
            if ranked:
                params = ranked[0]["params"]
        sub = CandleFeed(feed.symbol, feed.timeframe, test_df)
        res = run_backtest(symbol, timeframe, strategy, feed=sub,
                           strategy_params=params, **kw)
        results.append({"fold": f + 1, "from": str(sub.df.index[0]), "to": str(sub.df.index[-1]),
                        "params": params, **res.metrics.to_dict()})
    return results
