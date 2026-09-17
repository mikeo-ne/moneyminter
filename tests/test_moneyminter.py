import time

import pandas as pd
import pytest

from moneyminter import (BotConfig, PaperBroker, RiskConfig, RiskManager, Side,
                         TradingBot, available_strategies, get_instrument,
                         run_backtest)
from moneyminter import indicators as ta
from moneyminter.backtest import optimize, walk_forward
from moneyminter.data import SyntheticFeed, load_feed
from moneyminter.models import Position, utcnow


# ---------------------------------------------------------------- indicators
def test_indicators_shapes_and_bounds():
    df = SyntheticFeed("EUR/USD", "H1", 400, seed=1).df
    assert len(ta.sma(df.close, 10)) == len(df)
    r = ta.rsi(df.close, 14).dropna()
    assert r.between(0, 100).all()
    assert (ta.atr(df.high, df.low, df.close, 14).dropna() > 0).all()
    lo, mid, hi = ta.bollinger(df.close, 20)
    valid = mid.notna()
    assert (lo[valid] <= mid[valid]).all() and (mid[valid] <= hi[valid]).all()


def test_sma_is_correct():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    assert ta.sma(s, 2).tolist()[1:] == [1.5, 2.5, 3.5, 4.5]


# ---------------------------------------------------------------- data feeds
def test_synthetic_feed_is_deterministic_and_valid():
    a = SyntheticFeed("EUR/USD", "H1", 300, seed=42).df
    b = SyntheticFeed("EUR/USD", "H1", 300, seed=42).df
    pd.testing.assert_frame_equal(a, b)
    assert (a.high >= a.low).all()
    assert (a.high >= a[["open", "close"]].max(axis=1) - 1e-12).all()
    assert (a.low <= a[["open", "close"]].min(axis=1) + 1e-12).all()
    assert (a.close > 0).all()
    assert a.index.is_monotonic_increasing


def test_load_feed_falls_back_to_synthetic():
    assert len(load_feed("GBP/USD", "H1", "synthetic", bars=200)) == 200


# ---------------------------------------------------------------- instruments
def test_instrument_pip_maths():
    eu = get_instrument("eurusd")
    assert eu.symbol == "EUR/USD"
    assert eu.pips(0.0010) == pytest.approx(10)
    assert get_instrument("USD/JPY").pips(0.10) == pytest.approx(10)


# ---------------------------------------------------------------- risk
def test_position_size_respects_risk_budget():
    rm = RiskManager(RiskConfig(risk_per_trade=0.02, use_confidence_sizing=False))
    inst = get_instrument("EUR/USD")
    units = rm.position_size(10_000, 1.1000, 1.0980, inst)   # 20 pip stop
    assert units == pytest.approx(100_000, rel=0.01)          # $200 risk / 0.0020
    loss = abs(1.1000 - 1.0980) * units
    assert loss == pytest.approx(200, rel=0.01)


def test_risk_guardrails_block_trading():
    rm = RiskManager(RiskConfig(max_positions=2, max_daily_loss=0.05))
    today = utcnow().date()
    rm.update_equity(10_000, today)
    assert rm.can_open(10_000, 0, today)[0]
    assert not rm.can_open(10_000, 2, today)[0]         # position cap
    assert not rm.can_open(9_400, 0, today)[0]          # -6% daily loss


def test_drawdown_kill_switch():
    rm = RiskManager(RiskConfig(max_drawdown=0.20))
    today = utcnow().date()
    rm.update_equity(10_000, today)
    assert not rm.can_open(7_500, 0, today)[0]
    assert rm.halted_reason == "max drawdown"


# ---------------------------------------------------------------- broker
def test_broker_roundtrip_pnl_and_costs():
    b = PaperBroker(10_000)
    pos = b.open("EUR/USD", Side.BUY, 100_000, 1.1000, utcnow())
    assert pos.entry_price > 1.1000                 # pays spread + slippage
    trade = b.close(pos, 1.1100, utcnow(), "tp")
    assert 0 < trade.pnl < 1000                     # profitable but net of costs
    assert b.balance == pytest.approx(10_000 + trade.pnl, abs=1.0)
    assert not b.positions and len(b.trades) == 1


def test_short_position_profits_when_price_falls():
    b = PaperBroker(10_000)
    p = b.open("EUR/USD", Side.SELL, 50_000, 1.1000, utcnow())
    assert b.close(p, 1.0900, utcnow()).pnl > 0


def test_stop_and_target_detection():
    p = Position("EUR/USD", Side.BUY, 1000, 1.1000, utcnow(), stop_loss=1.0950, take_profit=1.1100)
    assert p.hit_stop(1.1050, 1.0940) == 1.0950
    assert p.hit_stop(1.1150, 1.0990) == 1.1100
    assert p.hit_stop(1.1050, 1.0990) is None
    s = Position("EUR/USD", Side.SELL, 1000, 1.1000, utcnow(), stop_loss=1.1050, take_profit=1.0900)
    assert s.hit_stop(1.1060, 1.1000) == 1.1050
    assert s.hit_stop(1.1010, 1.0890) == 1.0900


# ---------------------------------------------------------------- strategies
@pytest.mark.parametrize("name", list(available_strategies()))
def test_every_strategy_backtests(name):
    res = run_backtest("EUR/USD", "H1", name, bars=1500, seed=3)
    assert res.metrics.initial_balance == 10_000
    assert res.metrics.trades >= 0
    for t in res.trades:
        assert t.closed_at >= t.opened_at
    assert len(res.equity_curve) > 0
    assert res.to_dict()["metrics"]["trades"] == res.metrics.trades


def test_backtest_closes_all_positions():
    res = run_backtest("GBP/USD", "H1", "breakout", bars=1200, seed=5)
    assert res.metrics.trades == len(res.trades)
    assert res.metrics.wins + res.metrics.losses == res.metrics.trades


def test_risk_limit_caps_single_trade_loss():
    res = run_backtest("EUR/USD", "H1", "ma_crossover", balance=10_000, bars=2000,
                       risk=RiskConfig(risk_per_trade=0.01, use_confidence_sizing=False))
    for t in res.trades:
        assert t.pnl > -400, f"trade lost more than expected: {t.pnl}"


# ---------------------------------------------------------------- analytics
def test_optimize_and_walkforward():
    rows = optimize("EUR/USD", "H1", "ma_crossover", {"fast": [8, 12], "slow": [26, 34]}, bars=800)
    assert len(rows) == 4 and "params" in rows[0]
    assert rows[0]["sharpe"] >= rows[-1]["sharpe"]
    folds = walk_forward("EUR/USD", "H1", "breakout", folds=3, bars=900)
    assert [f["fold"] for f in folds] == [1, 2, 3]


# ---------------------------------------------------------------- engine
def test_engine_steps_and_reports_state():
    bot = TradingBot(BotConfig(symbols=["EUR/USD", "USD/JPY"], timeframe="M1",
                               strategy="breakout", speed=5000, journal_path=None))
    for _ in range(40):
        bot.step()
    s = bot.state()
    assert set(s["prices"]) == {"EUR/USD", "USD/JPY"}
    assert s["equity"] > 0 and s["stats"]["bars"] > 0
    assert isinstance(s["open_positions"], list)


def test_engine_start_stop_thread():
    bot = TradingBot(BotConfig(symbols=["EUR/USD"], timeframe="M1", speed=4000,
                               tick_interval=0.05, journal_path=None))
    bot.start()
    assert bot.running
    time.sleep(1.0)
    bot.stop()
    assert not bot.running and not bot.broker.positions


# ---------------------------------------------------------------- web api
def test_web_api_endpoints():
    from fastapi.testclient import TestClient
    from moneyminter.web.app import create_app

    with TestClient(create_app(BotConfig(journal_path=None), autostart=False)) as c:
        assert c.get("/").status_code == 200
        meta = c.get("/api/meta").json()
        assert "ma_crossover" in meta["strategies"]
        st = c.get("/api/state").json()
        assert st["equity"] == 10_000
        r = c.post("/api/backtest", json={"symbol": "EUR/USD", "strategy": "breakout", "bars": 600})
        assert r.status_code == 200 and "metrics" in r.json()
        assert c.post("/api/start", json={"symbols": ["EUR/USD"], "speed": 2000}).json()["ok"]
        assert c.post("/api/stop").json()["ok"]
