"""Tests that the validation harness can tell an edge from noise.

A validator is only useful if it is hard to fool. These tests assert both
directions:

* a strategy with **no edge**, run on data with **no exploitable structure**,
  must NOT be validated;
* the same class of strategy on data with genuine serial correlation MUST score
  materially higher, proving the battery is sensitive to real edge and not just
  a constant pessimist.

They also pin down the required behaviour of the Monte Carlo and cost checks.
"""
import math

import numpy as np
import pytest

from moneyminter.data import SyntheticFeed
from moneyminter.models import Side, Trade, utcnow
from moneyminter.validation import (THRESHOLDS, monte_carlo, validate)


def _noise(sym, seed):
    return SyntheticFeed(sym, "H1", bars=1800, seed=seed, autocorr=0.0)


def _edge(sym, seed):
    # 0.5 is a strong, unambiguous trend persistence injected on purpose. A
    # weaker edge (0.3) also lifts the score, but not by enough to be
    # distinguishable from noise at this sample size — which is itself the
    # lesson: small edges need a lot of independent evidence to detect.
    return SyntheticFeed(sym, "H1", bars=1800, seed=seed, autocorr=0.50)


# ------------------------------------------------------------------ data
def test_simulated_data_has_no_exploitable_autocorrelation():
    """The default feed must be an honest null: no momentum to farm."""
    for seed in (7, 99, 1234):
        r = np.log(SyntheticFeed("EUR/USD", "H1", bars=4000, seed=seed).df.close).diff().dropna()
        assert abs(r.autocorr(1)) < 0.08, "returns must not be predictable at lag 1"
        # variance ratio ~1 => random-walk-like, no systematic trend or reversion
        vr = r.rolling(20).sum().var() / (20 * r.var())
        assert 0.75 < vr < 1.35, f"variance ratio {vr:.2f} implies a tradeable pattern"


def test_simulated_data_has_fat_tails():
    """Gaussian noise would make stop-outs too well behaved; FX does not."""
    r = np.log(SyntheticFeed("EUR/USD", "H1", bars=4000, seed=7).df.close).diff().dropna()
    assert r.kurt() > 3.0


# --------------------------------------------------------------- monte carlo
def _trades(pnls):
    from datetime import timedelta
    t0 = utcnow()
    return [Trade("EUR/USD", Side.BUY, 100_000, 1.10, 1.10, t0,
                  t0 + timedelta(hours=1), p, "test") for p in pnls]


def test_monte_carlo_needs_a_minimum_sample():
    assert monte_carlo(_trades([10, -5, 3]), 10_000) is None


def test_monte_carlo_preserves_total_pnl():
    trades = _trades([50, -30, 80, -20, 10, -40, 60, -10, 25, -15, 35, -25])
    mc = monte_carlo(trades, 10_000, runs=300)
    total = sum(t.pnl for t in trades)
    # every shuffle ends at the same equity
    assert math.isclose(mc.p05_equity, mc.p95_equity, rel_tol=1e-9)
    assert math.isclose(mc.p50_equity, 10_000 + total, rel_tol=1e-9)


def test_monte_carlo_reports_path_risk_for_a_losing_distribution():
    trades = _trades([-500] * 10 + [200] * 5)
    mc = monte_carlo(trades, 10_000, runs=300)
    assert mc.p50_return_pct < 0
    assert mc.p95_max_dd_pct > mc.median_max_dd_pct


def test_monte_carlo_ruin_probability():
    # a 6-start-losing distribution on a small account is genuinely risky
    trades = _trades([-900] * 6 + [300] * 4)
    mc = monte_carlo(trades, 5_000, runs=500, ruin_pct=50)
    assert mc.risk_of_ruin_pct > 0


# ---------------------------------------------------------------- validator
def test_null_strategy_is_rejected_on_noise():
    """A trend follower on random data must not pass. If this fails, the
    validator is handing out false confidence."""
    r = validate("donchian_trend", samples=5, bars=1600, mc_runs=200,
                 feed_factory=_noise)
    assert r.verdict != "validated"
    assert r.score < THRESHOLDS["min_profitable_pct"] * 100


def test_validator_detects_a_real_edge():
    """With genuine serial correlation the same strategy must score clearly
    higher — proving the battery responds to edge rather than always saying no."""
    noise = validate("donchian_trend", samples=5, bars=1600, mc_runs=200,
                     feed_factory=_noise)
    edge = validate("donchian_trend", samples=5, bars=1600, mc_runs=200,
                    feed_factory=_edge)
    assert edge.score > noise.score + 25, \
        f"edge {edge.score} should beat noise {noise.score} by a clear margin"
    assert edge.median_pf > noise.median_pf
    assert edge.profitable_pct > noise.profitable_pct


def test_validator_rejects_when_out_of_sample_evidence_is_absent():
    """A high in-sample score must not buy a pass.

    Regression: with this exact setup the validator returned 98.8/100 and
    'validated' while all four walk-forward folds lost money. Walk-forward was
    worth 15 of 100 points and was not a gate, so overfitting scored well.
    """
    scores = []
    for a in (0.0, 0.3, 0.5):
        r = validate("donchian_trend", samples=4, bars=1500, mc_runs=150,
                     feed_factory=lambda s, sd, a=a: SyntheticFeed(
                         s, "H1", bars=1800, seed=sd, autocorr=a))
        scores.append(r.score)
        assert r.verdict == "rejected", (
            f"autocorr={a} scored {r.score} -> {r.verdict} but the pooled "
            "out-of-sample result is negative; it must not be validated")
        assert any("out-of-sample" in reason or "out of sample" in reason
                   for reason in r.reasons), r.reasons
    # noise must never out-score a real edge
    assert scores[0] <= scores[-1]


def test_folds_too_thin_to_judge_are_rejected_as_unproven():
    """2-4 trades per fold is a coin flip, not evidence."""
    r = validate("donchian_trend", samples=4, bars=1500, mc_runs=150)
    oos = sum(w["trades"] for w in r.walk_forward)
    if oos < 12:                      # this short series cannot prove anything
        assert r.verdict == "rejected"
        assert any("out-of-sample trades" in reason or "only" in reason
                   for reason in r.reasons)


def test_validator_says_yes_when_a_real_edge_has_enough_evidence():
    """The gate must be capable of passing, or it is just a 'no' machine."""
    r = validate("donchian_trend", samples=6, bars=4000, mc_runs=200,
                 feed_factory=lambda s, sd: SyntheticFeed(
                     s, "H1", bars=4600, seed=sd, autocorr=0.7))
    oos = sum(w["trades"] for w in r.walk_forward)
    assert oos >= 12, "test setup should produce enough out-of-sample trades"
    assert r.verdict == "validated", f"scored {r.score} with oos net " \
        f"{sum(w['net_profit'] for w in r.walk_forward):+.1f}"


def test_report_exposes_the_numbers_behind_the_verdict():
    r = validate("trend_pullback", samples=4, bars=1500, mc_runs=100,
                 feed_factory=_noise)
    d = r.to_dict()
    assert d["summary"]["verdict"] in ("validated", "promising", "rejected")
    assert len(d["samples"]) == 4
    for s in d["samples"]:
        assert {"net_profit", "profit_factor", "max_drawdown_pct"} <= set(s)
    assert d["monte_carlo"] is not None
    assert set(d["cost_sensitivity"]) >= {"base_pf", "wide_pf", "survival"}


def test_sample_labels_identify_symbol_and_seed():
    r = validate("trend_pullback", samples=3, bars=1200, mc_runs=50,
                 feed_factory=_noise)
    labels = [s.label for s in r.samples]
    assert len(set(labels)) == 3
    assert all("#" in lbl for lbl in labels)


def test_verdict_thresholds_are_explicit():
    """The bar for 'validated' must be visible and non-trivial."""
    assert THRESHOLDS["min_profitable_pct"] >= 0.6
    assert THRESHOLDS["min_median_pf"] >= 1.2
    assert THRESHOLDS["min_trades"] >= 40
    assert THRESHOLDS["min_cost_survival"] >= 0.5


# -------------------------------------------------------------- backtester
def test_backtest_is_deterministic_for_a_given_seed():
    from moneyminter import run_backtest
    a = run_backtest("EUR/USD", "H1", "trend_pullback", bars=1500, seed=11)
    b = run_backtest("EUR/USD", "H1", "trend_pullback", bars=1500, seed=11)
    assert a.metrics.net_profit == b.metrics.net_profit
    assert len(a.trades) == len(b.trades)


def test_wider_spreads_never_improve_a_backtest():
    """Cost sensitivity must be monotonic, or the harness is measuring noise."""
    from moneyminter import ExecutionConfig, run_backtest
    tight = run_backtest("EUR/USD", "H1", "trend_pullback", bars=2000, seed=5,
                         execution=ExecutionConfig(spread_multiplier=1.0))
    wide = run_backtest("EUR/USD", "H1", "trend_pullback", bars=2000, seed=5,
                        execution=ExecutionConfig(spread_multiplier=3.0))
    assert wide.metrics.net_profit < tight.metrics.net_profit


def test_backtest_never_risks_more_than_configured():
    from moneyminter import RiskConfig, run_backtest
    res = run_backtest("EUR/USD", "H1", "trend_pullback", bars=2500, seed=3,
                       risk=RiskConfig(risk_per_trade=0.01,
                                       use_confidence_sizing=False))
    for t in res.trades:
        assert t.pnl > -0.05 * 10_000, f"lost ${-t.pnl:.0f} on a 1% risk budget"
