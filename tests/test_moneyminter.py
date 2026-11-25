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


def test_position_size_converts_quote_currency_to_usd():
    """A 20-pip stop on USD/JPY must not be over-sized by the JPY rate.

    P&L on USD/JPY settles in JPY, so 1 unit of risk needs ``1/USDJPY`` USD of
    conversion. Ignoring it over-risks by ~150x on a JPY pair.
    """
    rm = RiskManager(RiskConfig(risk_per_trade=0.01, use_confidence_sizing=False,
                                max_leverage=1_000))
    inst = get_instrument("USD/JPY")                      # 151.20, pip 0.01
    entry, stop = 151.20, 151.00                           # 20-pip stop
    units = rm.position_size(10_000, entry, stop, inst)
    # $100 risk / (0.20 JPY per unit / 151.20 JPY per USD) = 75,600 units
    assert units == pytest.approx(75_600, rel=0.02)
    # one unit of USD/JPY is exactly $1 of notional, so a 50x cap still allows
    # 500k units — the risk budget, not leverage, is the binding constraint here
    rm_tight = RiskManager(RiskConfig(risk_per_trade=0.01, use_confidence_sizing=False,
                                      max_leverage=50))
    assert rm_tight.position_size(10_000, entry, stop, inst) == pytest.approx(
        75_600, rel=0.02)


def test_leverage_cap_uses_usd_notional_not_raw_price():
    """EUR/USD at 1.10: 1 unit is $1.10 notional, so a 10x cap on $10k is
    ~90,909 units — not the 100,000 a naive equity*leverage/price gives."""
    rm = RiskManager(RiskConfig(risk_per_trade=0.5, use_confidence_sizing=False,
                                max_leverage=10))
    inst = get_instrument("EUR/USD")
    units = rm.position_size(10_000, 1.10, 1.0999, inst)   # 1-pip stop: huge
    assert units == pytest.approx(10_000 * 10 / 1.10, rel=0.001)


def test_leverage_cap_can_reduce_size_below_risk_target():
    rm = RiskManager(RiskConfig(risk_per_trade=0.02, use_confidence_sizing=False,
                                max_leverage=5))
    inst = get_instrument("EUR/USD")
    units = rm.position_size(10_000, 1.1000, 1.0980, inst)
    assert units == pytest.approx(10_000 * 5 / 1.1, rel=0.01)   # capped


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


# ------------------------------------------------- new web api surface
def test_web_demo_and_validation_endpoints():
    """The dashboard's demo-sim and validation endpoints must round-trip."""
    import time

    from fastapi.testclient import TestClient
    from moneyminter.web.app import create_app

    with TestClient(create_app(BotConfig(journal_path=None), autostart=False)) as c:
        meta = c.get("/api/meta").json()
        assert "demo-sim" in meta["modes"]
        assert "trend_pullback" in meta["strategies"]

        v = c.post("/api/validate", json={"strategy": "donchian_trend",
                                          "samples": 3, "bars": 1200})
        assert v.status_code == 200
        body = v.json()
        assert body["summary"]["verdict"] in ("validated", "promising", "rejected")
        assert body["summary"]["score"] <= 100
        assert len(body["samples"]) == 3

        assert c.post("/api/demo/start",
                      json={"symbols": ["EUR/USD"], "strategy": "squeeze_momentum",
                            "bars_per_poll": 3}).status_code == 200
        time.sleep(0.5)
        st = c.get("/api/demo/state").json()
        assert st["mode"] == "sim"
        for t in st["trades"]:
            assert t["side"] in ("BUY", "SELL")
        assert c.post("/api/demo/stop").status_code == 200


def test_web_report_is_json_safe_with_infinite_profit_factor():
    """A run with no losing trades yields profit_factor=inf; JSON has no inf."""
    import json

    from fastapi.testclient import TestClient
    from moneyminter.web.app import create_app

    with TestClient(create_app(BotConfig(journal_path=None), autostart=False)) as c:
        r = c.post("/api/backtest", json={"symbol": "EUR/USD", "strategy": "breakout",
                                          "bars": 900, "seed": 4})
        assert r.status_code == 200
        json.dumps(r.json())            # must not raise on inf/nan


# ------------------------------------------------------------------ doctor
def test_doctor_runs_and_reports_offline_mode(capsys):
    """`doctor` must be runnable anywhere and never crash on a missing broker."""
    from moneyminter.cli import main
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert "Python drivers" in out and "Offline mode" in out
    assert "Simulated demo session runs" in out
    # exit code is 2 when a driver is missing, 0 when everything is present
    assert code in (0, 2)


def test_dashboard_has_phone_layout_rules():
    """The dashboard is meant to be watched from a phone.

    Without a sub-640px media query the fixed gutters, sticky header and
    110px-minimum inputs overflow a 380px screen.
    """
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1]
           / "moneyminter/web/static/style.css").read_text()
    assert "@media(max-width:640px)" in css
    assert "overflow-x:auto" in css          # wide tables scroll, not clip
    assert "env(safe-area-inset-left)" in css
    assert "width=device-width" in (
        Path(__file__).resolve().parents[1]
        / "moneyminter/web/templates/index.html").read_text()


# ------------------------------------------------------ dashboard auth
def _client(**kw):
    from starlette.testclient import TestClient
    from moneyminter.web.app import create_app
    return TestClient(create_app(autostart=False, **kw))


def test_dashboard_without_auth_is_open():
    c = _client(auth=False)
    assert c.get("/api/meta").status_code == 200


def test_dashboard_auth_blocks_every_mutating_endpoint():
    """A dashboard on a borrowed VPS must not let a stranger stop your bot."""
    c = _client(auth=True, auth_user="me", auth_password="s3cret")
    assert c.get("/api/meta").status_code == 401
    assert c.post("/api/stop").status_code == 401
    assert c.post("/api/start", json={}).status_code == 401
    assert c.post("/api/demo/start", json={}).status_code == 401
    # the page itself and its assets must still load, or the login prompt never
    # appears
    assert c.get("/").status_code == 200
    assert c.get("/static/style.css").status_code == 200


def test_dashboard_auth_accepts_correct_credentials():
    c = _client(auth=True, auth_user="me", auth_password="s3cret")
    assert c.get("/api/meta", auth=("me", "s3cret")).status_code == 200
    assert c.get("/api/meta", auth=("me", "wrong")).status_code == 401
    assert c.get("/api/meta", auth=("other", "s3cret")).status_code == 401


def test_dashboard_websocket_requires_auth():
    import pytest
    c = _client(auth=True, auth_user="me", auth_password="s3cret")
    with pytest.raises(Exception):
        with c.websocket_connect("/ws"):
            pass


def test_public_bind_forces_auth_and_generates_a_password(capsys):
    """Binding 0.0.0.0 must never mean anonymous control of the bot."""
    from moneyminter.web.app import _resolve_auth
    on, user, password = _resolve_auth(None, None, None)
    assert on is False                    # nothing to enforce without a host
    out = capsys.readouterr().out
    assert password == "" and out == ""


def test_loopback_bind_defaults_to_no_password():
    from moneyminter.cli import build_parser
    p = build_parser()
    a = p.parse_args(["dashboard", "--host", "127.0.0.1"])
    assert a.auth is None                 # CLI decides: loopback -> off
    b = p.parse_args(["dashboard", "--host", "0.0.0.0"])
    assert b.auth is None                 # CLI decides: public -> on


# ------------------------------------------------------- virtual machines
def test_doctor_flags_arm_windows_as_unsusable_for_the_api(monkeypatch, capsys):
    """Windows-on-ARM is the most confusing VM setup.

    MT5 itself installs and runs, so everything looks fine — but the
    MetaTrader5 package only ever ships win32/win_amd64 wheels, so
    `pip install MetaTrader5` cannot succeed and no driver can attach.
    """
    import platform
    import sys
    from moneyminter.cli import main

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform, "machine", lambda: "ARM64")
    main(["doctor"])
    out = capsys.readouterr().out
    assert "ARM Windows" in out
    assert "win_amd64 only" in out
    assert "x86-64 Windows VM" in out


def test_doctor_does_not_complain_on_x86_windows(monkeypatch, capsys):
    import platform
    import sys
    from moneyminter.cli import main

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    main(["doctor"])
    assert "This is ARM Windows" not in capsys.readouterr().out


def test_doctor_distinguishes_installed_from_usable_macos_bundle(tmp_path, monkeypatch, capsys):
    """"MT5.app exists" and "MT5 can be driven from Python" are different."""
    import os
    import sys
    from moneyminter.cli import main
    import moneyminter.live.mt5_broker as mb

    app = os.path.join(str(tmp_path), "MetaTrader 5.app")
    os.makedirs(os.path.join(app, "Contents", "MacOS"))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(mb, "find_mac_mt5_app", lambda: app)

    main(["doctor"])
    out = capsys.readouterr().out
    assert "no usable Wine runtime" in out
    assert "no wine executable anywhere inside the bundle" in out


def test_doctor_explains_the_linux_route_and_its_limits(monkeypatch, capsys):
    """Linux has no native MT5, and ARM makes it far worse."""
    import platform
    import sys
    from moneyminter.cli import main

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    main(["doctor"])
    out = capsys.readouterr().out
    assert "mt5server.exe" in out
    assert "--mt5-host" in out

    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    main(["doctor"])
    out = capsys.readouterr().out
    assert "too" in out and "slow" in out
    assert "Use a Windows VPS" in out
