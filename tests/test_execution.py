"""Tests for the trade manager, broker realism and risk guardrails."""
import numpy as np
import pytest

from moneyminter import (ExecutionConfig, PaperBroker, Position, RiskConfig,
                         RiskManager, Side, get_instrument, utcnow)
from moneyminter.management import (BREAKEVEN, STOP, TARGET, TIME, TRAIL,
                                    hard_breach, manage_bar)


def _pos(side=Side.BUY, entry=1.1000, stop=1.0980, tp=1.1060, **kw):
    kw.setdefault("take_profit", tp)
    return Position("EUR/USD", side, 100_000, entry, utcnow(), stop_loss=stop,
                    initial_stop=stop, **kw)


# ------------------------------------------------------------- trade manager
def test_stop_is_hit_before_target_when_bar_straddles_both():
    """Pessimistic assumption: a bar that touches both resolves to the stop."""
    p = _pos()
    d = manage_bar(p, high=1.1070, low=1.0975, close=1.1050)
    assert d is not None and d.reason == STOP and d.price == pytest.approx(1.0980)


def test_target_fills_when_only_target_is_touched():
    p = _pos()
    d = manage_bar(p, high=1.1065, low=1.0990, close=1.1060)
    assert d is not None and d.reason == TARGET and d.price == pytest.approx(1.1060)


def test_bar_that_touches_nothing_keeps_the_trade_open():
    p = _pos()
    assert manage_bar(p, high=1.1020, low=1.0990, close=1.1010) is None


def test_trailing_stop_only_ratchets_upwards():
    p = _pos(trail_atr=2.0, take_profit=None)
    # price rallies: the trail should rise
    manage_bar(p, high=1.1040, low=1.0995, close=1.1035, atr_value=0.0010)
    first = p.stop_loss
    assert first > 1.0980
    # price falls back but stays above the trail: the stop must NOT loosen
    manage_bar(p, high=1.1030, low=1.1005, close=1.1010, atr_value=0.0010)
    assert p.stop_loss == first


def test_trailing_stop_does_not_apply_inside_the_bar_that_sets_it():
    """A trail set by bar N can only be hit on bar N+1 — no look-ahead."""
    p = _pos(trail_atr=1.0, take_profit=None)
    d = manage_bar(p, high=1.1060, low=1.0995, close=1.1050, atr_value=0.0005)
    assert d is None                      # the wide bar cannot stop itself out
    assert p.stop_loss > 1.1000           # but the stop has moved up


def test_short_trailing_stop_ratchets_downwards():
    p = _pos(Side.SELL, entry=1.1000, stop=1.1020, tp=1.0920, trail_atr=2.0)
    manage_bar(p, high=1.1005, low=1.0960, close=1.0965, atr_value=0.0010)
    first = p.stop_loss
    assert first < 1.1020
    manage_bar(p, high=1.0990, low=1.0970, close=1.0980, atr_value=0.0010)
    assert p.stop_loss == first


def test_break_even_moves_stop_to_entry_after_one_r():
    p = _pos(breakeven_at_r=1.0, take_profit=None)
    # 1R = the 20 pip stop distance, so a 20+ pip rally triggers break-even
    manage_bar(p, high=1.1025, low=1.0995, close=1.1020, atr_value=0.0010)
    assert p.stop_loss == pytest.approx(1.1000)


def test_time_exit_closes_at_the_close():
    p = _pos(max_bars=3, take_profit=None)
    manage_bar(p, 1.1010, 1.0990, 1.1005)
    manage_bar(p, 1.1010, 1.0990, 1.1005)
    d = manage_bar(p, 1.1010, 1.0990, 1.1005)
    assert d is not None and d.reason == TIME and d.price == pytest.approx(1.1005)


def test_mfe_mae_tracking():
    p = _pos()
    manage_bar(p, high=1.1040, low=1.0985, close=1.1030, atr_value=0.001)
    assert p.mfe_r == pytest.approx(2.0, abs=0.05)      # 40 pips = 2R
    assert p.mae_r == pytest.approx(-0.75, abs=0.05)    # 15 pips against


def test_hard_breach_uses_the_right_side_of_the_spread():
    buy = _pos(stop=1.0980)
    assert hard_breach(buy, bid=1.0985, ask=1.0987) is None
    assert hard_breach(buy, bid=1.0979, ask=1.0981) is not None
    sell = _pos(Side.SELL, stop=1.1020)
    assert hard_breach(sell, bid=1.1018, ask=1.1021) is not None
    assert hard_breach(sell, bid=1.1010, ask=1.1013) is None


# ---------------------------------------------------------------- paper broker
def test_broker_grid_rejects_sizes_below_the_minimum_lot():
    """A size the demo account cannot trade must not silently succeed."""
    b = PaperBroker(10_000)
    inst = get_instrument("EUR/USD")
    tiny = inst.contract_size * (inst.min_lot / 2)
    assert b.open("EUR/USD", Side.BUY, tiny, 1.10) is None
    assert b.rejected and "minimum lot" in b.rejected[0]["reason"]
    ok = b.open("EUR/USD", Side.BUY, inst.contract_size * inst.min_lot, 1.10)
    assert ok is not None


def test_broker_grid_snaps_down_to_the_lot_step():
    b = PaperBroker(10_000)
    inst = get_instrument("EUR/USD")
    # 1.037 lots must become 1.03, never 1.04
    p = b.open("EUR/USD", Side.BUY, inst.contract_size * 1.037, 1.10)
    assert p.units == pytest.approx(inst.contract_size * 1.03)


def test_stops_pay_more_slippage_than_targets():
    b = PaperBroker(10_000, ExecutionConfig(stop_slippage_pips=0.5))
    stop_fill = b.fill_price("EUR/USD", Side.BUY, 1.1000, closing=True, stop_out=True)
    target_fill = b.fill_price("EUR/USD", Side.BUY, 1.1000, closing=True)
    assert stop_fill < target_fill


def test_commission_and_swap_are_charged():
    b = PaperBroker(10_000, ExecutionConfig(commission_per_million=35.0,
                                            swap_pips_per_day=-0.5))
    p = b.open("EUR/USD", Side.BUY, 100_000, 1.1000, utcnow())
    from datetime import timedelta
    trade = b.close(p, 1.1000, utcnow() + timedelta(days=2), "flat")
    assert trade.commission > 0 and trade.swap < 0
    assert trade.pnl < 0                      # round trip at flat price loses


def test_trade_records_the_stop_it_was_risking():
    b = PaperBroker(10_000)
    p = b.open("EUR/USD", Side.BUY, 100_000, 1.1000, stop_loss=1.0980)
    t = b.close(p, 1.1020, reason="tp")
    assert t.initial_stop == pytest.approx(1.0980)
    assert t.r_multiple == pytest.approx(1.0, abs=0.15)


def test_open_risk_shrinks_when_the_stop_trails_past_entry():
    b = PaperBroker(10_000)
    p = b.open("EUR/USD", Side.BUY, 100_000, 1.1000, stop_loss=1.0980)
    before = b.open_risk(10_000)
    p.stop_loss = 1.1010                       # trailed into profit
    after = b.open_risk(10_000)
    assert after < before


# ----------------------------------------------------------------------- risk
def test_correlation_gate_blocks_a_second_usd_short():
    rm = RiskManager(RiskConfig(max_per_currency_group=1))
    today = utcnow().date()
    ok, why = rm.can_open(10_000, 1, today, symbol="GBP/USD",
                          open_symbols=["EUR/USD"])
    assert not ok and "correlated" in why


def test_correlation_gate_allows_uncorrelated_pairs():
    rm = RiskManager(RiskConfig(max_per_currency_group=1))
    ok, _ = rm.can_open(10_000, 1, utcnow().date(), symbol="USD/JPY",
                        open_symbols=["EUR/USD"])
    assert ok


def test_same_symbol_cannot_be_opened_twice():
    rm = RiskManager(RiskConfig())
    ok, why = rm.can_open(10_000, 1, utcnow().date(), symbol="EUR/USD",
                          open_symbols=["EUR/USD"])
    assert not ok and "already in a position" in why


def test_portfolio_risk_cap_blocks_stacking():
    rm = RiskManager(RiskConfig(max_portfolio_risk=0.03))
    ok, why = rm.can_open(10_000, 2, utcnow().date(), symbol="AUD/USD",
                          open_symbols=["EUR/USD"], portfolio_risk=0.031)
    assert not ok and "portfolio risk" in why


def test_losing_streak_pauses_a_symbol():
    rm = RiskManager(RiskConfig(loss_streak_pause=3, pause_minutes=60))
    now = utcnow()
    for _ in range(3):
        rm.record_result("EUR/USD", -50.0, now)
    assert rm.is_paused("EUR/USD", now)
    ok, why = rm.can_open(10_000, 0, now.date(), symbol="EUR/USD", now=now)
    assert not ok and "paused" in why


def test_weekly_loss_limit_halts_trading():
    rm = RiskManager(RiskConfig(max_weekly_loss=0.05, max_daily_loss=0.5))
    today = utcnow().date()
    rm.update_equity(10_000, today)
    ok, why = rm.can_open(9_400, 0, today)     # -6% on the week
    assert not ok and "weekly" in why


# ------------------------------------------------- consistency across engines
def test_backtest_paper_and_live_share_one_trade_manager():
    """The exit geometry must not depend on which engine is running.

    A strategy that trails in a backtest but not on the demo account makes every
    backtest number meaningless, so all three engines call ``manage_bar``.
    """
    import inspect

    from moneyminter import backtest, engine
    from moneyminter.live import runner

    for mod in (backtest, engine, runner):
        src = inspect.getsource(mod)
        assert "manage_bar" in src, f"{mod.__name__} bypasses the shared manager"
