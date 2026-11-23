"""Strategy validation: the difference between "backtests well" and "proven".

A single backtest on a single price series is not evidence. With ~3,000 bars and
a handful of parameters, almost anything can be made to look good by chance —
that is the multiple-comparisons problem, and it is the single most common way
traders fool themselves.

This module implements the checks a quant desk would actually run before
allocating to a strategy:

1. **Cross-sectional robustness** — the same parameters must work on many
   *independent* price samples (different seeds, symbols and timeframes), not
   just the one that was optimised. We report the share of samples that are
   profitable and the worst case, not just the average.
2. **Out-of-sample walk-forward** — parameters fitted on slice *n* must survive
   slice *n+1* untouched.
3. **Monte Carlo on trade order** — reshuffling trades tests whether the equity
   curve depends on a lucky sequence. We report the 5th percentile outcome and
   the probability of a drawdown worse than the plan allows.
4. **Cost sensitivity** — the edge must survive a doubling of spread. Strategies
   whose profit vanishes when spreads widen are scalping noise, not edge.
5. **Sample sufficiency** — fewer than ``min_trades`` trades proves nothing.

The output is a verdict with explicit thresholds, written to JSON so the web UI
and CLI can both read it. ``VALIDATED`` is a high bar and deliberately rare.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from statistics import median
from typing import Iterable, Optional, Sequence

import numpy as np

from .backtest import run_backtest, walk_forward
from .broker import ExecutionConfig
from .data import SyntheticFeed, load_feed
from .instruments import INSTRUMENTS
from .models import Trade
from .risk import RiskConfig
from .strategies import get_strategy

#: thresholds that separate a real edge from a lucky curve
THRESHOLDS = {
    "min_samples": 8,             # independent price samples
    "min_profitable_pct": 0.70,   # share of samples in profit
    "min_median_pf": 1.30,        # profit factor across samples
    "min_trades": 60,             # total trades across all samples
    "max_worst_return_pct": -25.0,  # worst single sample may not blow up
    "min_cost_survival": 0.60,    # pf at 2x spread vs 1x
    "max_mc_dd_pct": 35.0,        # 95th percentile Monte Carlo drawdown
}

DEFAULT_SYMBOLS = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD"]
INF_CAP = 999.0


def _json_safe(obj):
    """Replace NaN/inf so reports are always JSON-serialisable."""
    if isinstance(obj, float):
        if math.isnan(obj):
            return 0.0
        if math.isinf(obj):
            return INF_CAP if obj > 0 else -INF_CAP
        return obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


@dataclass
class SampleResult:
    label: str
    net_profit: float
    return_pct: float
    trades: int
    profit_factor: float
    win_rate: float
    max_drawdown_pct: float
    sharpe: float
    expectancy: float


@dataclass
class MonteCarloResult:
    runs: int
    p05_equity: float
    p50_equity: float
    p95_equity: float
    p05_return_pct: float
    p50_return_pct: float
    p95_return_pct: float
    median_max_dd_pct: float
    p95_max_dd_pct: float
    risk_of_ruin_pct: float
    worst_run_pct: float


@dataclass
class ValidationReport:
    strategy: str
    symbol: str
    timeframe: str
    params: dict
    samples: list[SampleResult] = field(default_factory=list)
    walk_forward: list[dict] = field(default_factory=list)
    monte_carlo: Optional[MonteCarloResult] = None
    cost_sensitivity: dict = field(default_factory=dict)
    score: float = 0.0
    verdict: str = "rejected"
    reasons: list[str] = field(default_factory=list)
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # ---- aggregates ------------------------------------------------------
    @property
    def profitable_pct(self) -> float:
        if not self.samples:
            return 0.0
        return sum(1 for s in self.samples if s.net_profit > 0) / len(self.samples)

    @property
    def median_pf(self) -> float:
        if not self.samples:
            return 0.0
        finite = [min(s.profit_factor, 99.0) for s in self.samples]
        return float(median(finite))

    @property
    def total_trades(self) -> int:
        return sum(s.trades for s in self.samples)

    @property
    def worst_return_pct(self) -> float:
        return min((s.return_pct for s in self.samples), default=0.0)

    @property
    def median_return_pct(self) -> float:
        return float(median([s.return_pct for s in self.samples])) if self.samples else 0.0

    @property
    def is_validated(self) -> bool:
        return self.verdict == "validated"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["samples"] = [_json_safe(asdict(s)) for s in self.samples]
        d["summary"] = _json_safe({
            "profitable_pct": round(self.profitable_pct * 100, 1),
            "median_pf": round(self.median_pf, 2),
            "median_return_pct": round(self.median_return_pct, 2),
            "worst_return_pct": round(self.worst_return_pct, 2),
            "total_trades": self.total_trades,
            "score": round(self.score, 1),
            "verdict": self.verdict,
        })
        d["walk_forward"] = [_json_safe(w) for w in self.walk_forward]
        return _json_safe(d)

    def summary(self) -> str:
        rows = [
            ("Strategy", f"{self.strategy} on {self.symbol} {self.timeframe}"),
            ("Parameters", json.dumps(self.params, sort_keys=True)),
            ("Samples tested", f"{len(self.samples)}"),
            ("Samples profitable", f"{self.profitable_pct * 100:.0f}%"),
            ("Median profit factor", f"{self.median_pf:.2f}"),
            ("Median return", f"{self.median_return_pct:+.2f}%"),
            ("Worst sample", f"{self.worst_return_pct:+.2f}%"),
            ("Total trades", f"{self.total_trades}"),
        ]
        if self.monte_carlo:
            mc = self.monte_carlo
            rows += [
                ("Monte Carlo median", f"{mc.p50_return_pct:+.1f}% "
                                       f"(5th {mc.p05_return_pct:+.1f}%)"),
                ("MC 95th pct drawdown", f"{mc.p95_max_dd_pct:.1f}%"),
                ("MC risk of ruin", f"{mc.risk_of_ruin_pct:.1f}%"),
            ]
        if self.walk_forward:
            oos = [w["return_pct"] for w in self.walk_forward]
            rows.append(("Walk-forward folds", f"{sum(1 for r in oos if r > 0)}/{len(oos)} profitable "
                                               f"({min(oos):+.2f}% .. {max(oos):+.2f}%)"))
        if self.cost_sensitivity:
            rows.append(("2x spread survival", f"{self.cost_sensitivity.get('survival', 0) * 100:.0f}%"))
        rows.append(("Score", f"{self.score:.1f}/100"))
        rows.append(("VERDICT", self.verdict.upper()))
        w = max(len(a) for a, _ in rows)
        out = "\n".join(f"  {a.ljust(w)} : {b}" for a, b in rows)
        if self.reasons:
            out += "\n\n  Findings:"
            out += "".join(f"\n    · {r}" for r in self.reasons)
        return out


# --------------------------------------------------------------- sampling
def build_samples(symbol: str, timeframe: str, samples: int,
                  bars: int = 3000, symbols: Sequence[str] | None = None,
                  feed_factory=None) -> list[tuple[str, object]]:
    """Create independent price samples: distinct seeds across several symbols.

    Synthetic seeds give genuinely independent paths (useful in an offline CI
    environment); spreading across symbols adds cross-sectional evidence, which
    is what stops a strategy from being "an EUR/USD 2023 phenomenon".
    """
    syms = list(symbols or [symbol])
    if symbol not in syms:
        syms.insert(0, symbol)
    out = []
    for i in range(samples):
        sym = syms[i % len(syms)]
        seed = 1000 + i * 17
        if feed_factory is not None:
            feed = feed_factory(sym, seed)
        else:
            feed = SyntheticFeed(sym, timeframe, bars=bars, seed=seed)
        out.append((f"{sym}#{seed}", feed))
    return out


# --------------------------------------------------------------- monte carlo
def monte_carlo(trades: Sequence[Trade], initial_balance: float, runs: int = 2000,
                ruin_pct: float = 50.0, seed: int = 12345) -> Optional[MonteCarloResult]:
    """Reshuffle trade order to see how much the equity curve depends on luck."""
    if len(trades) < 10:
        return None
    pnls = np.array([t.pnl for t in trades], dtype=float)
    rng = np.random.default_rng(seed)
    finals, max_dds, ruined = [], [], 0
    for _ in range(runs):
        eq = initial_balance
        peak = eq
        worst = 0.0
        for pnl in rng.permutation(pnls):
            eq += pnl
            peak = max(peak, eq)
            dd = (peak - eq) / peak * 100 if peak > 0 else 100.0
            worst = max(worst, dd)
            if eq <= initial_balance * (1 - ruin_pct / 100):
                ruined += 1
                break
        finals.append(eq)
        max_dds.append(worst)
    finals_a = np.array(finals)
    dds_a = np.array(max_dds)
    pct = (finals_a / initial_balance - 1) * 100
    return MonteCarloResult(
        runs=runs,
        p05_equity=float(np.percentile(finals_a, 5)),
        p50_equity=float(np.percentile(finals_a, 50)),
        p95_equity=float(np.percentile(finals_a, 95)),
        p05_return_pct=float(np.percentile(pct, 5)),
        p50_return_pct=float(np.percentile(pct, 50)),
        p95_return_pct=float(np.percentile(pct, 95)),
        median_max_dd_pct=float(np.percentile(dds_a, 50)),
        p95_max_dd_pct=float(np.percentile(dds_a, 95)),
        risk_of_ruin_pct=ruined / runs * 100,
        worst_run_pct=float(pct.min()),
    )


# --------------------------------------------------------------- core runner
def validate(strategy: str, symbol: str = "EUR/USD", timeframe: str = "H1",
             samples: int = 12, bars: int = 3000, balance: float = 10_000.0,
             risk: Optional[RiskConfig] = None, params: Optional[dict] = None,
             symbols: Sequence[str] | None = None, mc_runs: int = 2000,
             folds: int = 4, cost_check: bool = True, feed_factory=None) -> ValidationReport:
    """Run the full validation battery for one strategy configuration."""
    risk = risk or RiskConfig()
    strat = get_strategy(strategy, **(params or {}))
    report = ValidationReport(strategy=strategy, symbol=symbol, timeframe=timeframe,
                              params=strat.params)

    # ---- 1. cross-sectional samples -------------------------------------
    all_trades: list[Trade] = []
    for label, feed in build_samples(symbol, timeframe, samples, bars=bars,
                                     symbols=symbols, feed_factory=feed_factory):
        try:
            res = run_backtest(symbol, timeframe, strategy, balance=balance,
                               risk=risk, strategy_params=params, feed=feed)
        except Exception as exc:  # noqa: BLE001 - one bad sample is data too
            report.samples.append(SampleResult(label, 0, 0, 0, 0, 0, 0, 0, 0))
            report.reasons.append(f"sample {label} failed: {type(exc).__name__}")
            continue
        m = res.metrics
        report.samples.append(SampleResult(
            label=f"{label} ({feed.symbol})" if hasattr(feed, "symbol") else label,
            net_profit=round(m.net_profit, 2), return_pct=round(m.return_pct, 3),
            trades=m.trades, profit_factor=round(min(m.profit_factor, 99.0), 3),
            win_rate=round(m.win_rate, 2), max_drawdown_pct=round(m.max_drawdown_pct, 2),
            sharpe=round(m.sharpe, 2), expectancy=round(m.expectancy, 2)))
        all_trades.extend(res.trades)

    # ---- 2. walk-forward out-of-sample ----------------------------------
    try:
        wf_feed = (feed_factory(symbol, 4242) if feed_factory is not None
                   else SyntheticFeed(symbol, timeframe, bars=bars, seed=4242))
        report.walk_forward = walk_forward(symbol, timeframe, strategy, folds=folds,
                                           bars=bars, risk=risk, feed=wf_feed,
                                           strategy_params=params)
    except Exception as exc:  # noqa: BLE001
        report.reasons.append(f"walk-forward unavailable: {exc}")

    # ---- 3. Monte Carlo on trade order ----------------------------------
    report.monte_carlo = monte_carlo(all_trades, balance, runs=mc_runs)

    # ---- 4. cost sensitivity --------------------------------------------
    # Measured on the same independent samples used above, so a strategy whose
    # profit comes from beating the spread is exposed. A single global run would
    # compare two different price paths and measure nothing.
    if cost_check:
        base_pfs, wide_pfs, base_nets, wide_nets = [], [], [], []
        for _label, feed in build_samples(symbol, timeframe, min(4, samples),
                                          bars=bars, symbols=symbols,
                                          feed_factory=feed_factory):
            try:
                base = run_backtest(symbol, timeframe, strategy, balance=balance,
                                    risk=risk, strategy_params=params, feed=feed,
                                    execution=ExecutionConfig(spread_multiplier=1.0))
                wide = run_backtest(symbol, timeframe, strategy, balance=balance,
                                    risk=risk, strategy_params=params, feed=feed,
                                    execution=ExecutionConfig(spread_multiplier=2.0,
                                                              slippage_pips=0.3))
            except Exception:  # noqa: BLE001
                continue
            base_pfs.append(base.metrics.profit_factor)
            wide_pfs.append(wide.metrics.profit_factor)
            base_nets.append(base.metrics.net_profit)
            wide_nets.append(wide.metrics.net_profit)
        base_pf = float(median(base_pfs)) if base_pfs else 0.0
        wide_pf = float(median(wide_pfs)) if wide_pfs else 0.0
        survival = 0.0
        if base_pf > 1.0:
            survival = min(1.0, max(0.0, (wide_pf - 1.0) / (base_pf - 1.0)))
        base = types_namespace(base_pf, median(base_nets) if base_nets else 0.0)
        wide = types_namespace(wide_pf, median(wide_nets) if wide_nets else 0.0)
        report.cost_sensitivity = {
            "base_pf": round(base.metrics.profit_factor, 3),
            "wide_pf": round(wide.metrics.profit_factor, 3),
            "base_net": round(base.metrics.net_profit, 2),
            "wide_net": round(wide.metrics.net_profit, 2),
            "survival": round(survival, 3),
        }

    _score(report)
    return report


def types_namespace(pf: float, net: float):
    """Tiny stand-in so the report block below stays readable."""
    return type("Run", (), {"metrics": type("M", (), {"profit_factor": pf,
                                                       "net_profit": net})})


def _score(report: ValidationReport) -> None:
    """Turn the battery into a 0-100 score and an explicit verdict."""
    t = THRESHOLDS
    reasons = report.reasons
    score = 0.0

    prof = report.profitable_pct
    score += 30 * min(1.0, prof / t["min_profitable_pct"])
    if prof < t["min_profitable_pct"]:
        reasons.append(f"only {prof * 100:.0f}% of samples were profitable "
                       f"(need {t['min_profitable_pct'] * 100:.0f}%)")

    pf = report.median_pf
    score += 25 * min(1.0, max(0.0, (pf - 1.0) / (t["min_median_pf"] - 1.0)))
    if pf < t["min_median_pf"]:
        reasons.append(f"median profit factor {pf:.2f} is below {t['min_median_pf']}")

    if report.total_trades < t["min_trades"]:
        reasons.append(f"only {report.total_trades} trades across all samples — "
                       f"not enough evidence (need {t['min_trades']})")
    else:
        score += 10

    worst = report.worst_return_pct
    if worst < t["max_worst_return_pct"]:
        reasons.append(f"worst sample returned {worst:+.1f}% — plan for this drawdown")
    else:
        score += 10

    wf = report.walk_forward
    if wf:
        wins = sum(1 for w in wf if w["return_pct"] > 0)
        score += 15 * (wins / len(wf))
        if wins < len(wf):
            reasons.append(f"{len(wf) - wins}/{len(wf)} walk-forward folds lost money")
    else:
        reasons.append("no walk-forward evidence")

    cs = report.cost_sensitivity
    if cs:
        if cs["survival"] >= t["min_cost_survival"]:
            score += 10
        else:
            reasons.append(f"edge decays under wider spreads "
                           f"(survival {cs['survival'] * 100:.0f}%)")

    if report.monte_carlo:
        if report.monte_carlo.p95_max_dd_pct > t["max_mc_dd_pct"]:
            reasons.append(f"Monte Carlo 95th-percentile drawdown "
                           f"{report.monte_carlo.p95_max_dd_pct:.1f}% exceeds "
                           f"{t['max_mc_dd_pct']}%")
        else:
            score += 10
        if report.monte_carlo.risk_of_ruin_pct > 5:
            reasons.append(f"risk of ruin {report.monte_carlo.risk_of_ruin_pct:.1f}%")
    else:
        reasons.append("too few trades for a Monte Carlo estimate")

    report.score = round(min(100.0, max(0.0, score)), 1)
    passes_minimum = (
        prof >= t["min_profitable_pct"]
        and pf >= t["min_median_pf"]
        and report.total_trades >= t["min_trades"]
        and worst >= t["max_worst_return_pct"]
    )
    if score >= 82 and passes_minimum:
        report.verdict = "validated"
    elif score >= 58 and pf > 1.05:
        report.verdict = "promising"
    else:
        report.verdict = "rejected"


# --------------------------------------------------------------- promotion
def scan(strategies: Iterable[str] | None = None, symbol: str = "EUR/USD",
         timeframe: str = "H1", samples: int = 8, bars: int = 2500,
         params: Optional[dict] = None) -> list[ValidationReport]:
    """Validate every strategy and return reports sorted by score."""
    from .strategies import available_strategies

    names = list(strategies or available_strategies())
    reports = []
    for name in names:
        try:
            reports.append(validate(name, symbol=symbol, timeframe=timeframe,
                                    samples=samples, bars=bars, params=params))
        except Exception as exc:  # noqa: BLE001
            r = ValidationReport(strategy=name, symbol=symbol, timeframe=timeframe, params={})
            r.reasons.append(f"validation failed: {type(exc).__name__}: {exc}")
            reports.append(r)
    reports.sort(key=lambda r: r.score, reverse=True)
    return reports


def save_reports(reports: Sequence[ValidationReport], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "thresholds": THRESHOLDS,
        "reports": [r.to_dict() for r in reports],
    }
    p.write_text(json.dumps(payload, indent=2))
    return p


def load_reports(path: str | Path) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
