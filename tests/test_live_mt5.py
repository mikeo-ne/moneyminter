"""Tests for the MT5/Exness live adapter, using a mock terminal."""
import sys
import types

import pytest

from moneyminter.models import Side

from . import mock_mt5


@pytest.fixture
def mt5(monkeypatch):
    def _install(**kw):
        m = mock_mt5.build(**kw)
        monkeypatch.setitem(sys.modules, "MetaTrader5", m)
        return m
    return _install


def test_resolves_exness_symbol_suffix(mt5):
    mt5(suffix="m")
    from moneyminter.live import MT5Broker
    b = MT5Broker()
    assert b.resolve_symbol("EUR/USD") == "EURUSDm"
    assert b.resolve_symbol("USD/JPY") == "USDJPYm"


def test_resolves_plain_symbols(mt5):
    mt5(suffix="")
    from moneyminter.live import MT5Broker
    assert MT5Broker().resolve_symbol("EUR/USD") == "EURUSD"


def test_units_to_lots_rounding(mt5):
    mt5()
    from moneyminter.live import MT5Broker
    b = MT5Broker()
    assert b.units_to_lots("EUR/USD", 100_000) == 1.0
    assert b.units_to_lots("EUR/USD", 150_000) == 1.5
    assert b.units_to_lots("EUR/USD", 1_500) == 0.01


def test_undersized_trade_is_skipped_not_rounded_up(mt5):
    """Rounding up to the min lot would risk ~200x the intended amount."""
    mt5()
    from moneyminter.live import MT5Broker
    b = MT5Broker()
    assert b.units_to_lots("EUR/USD", 5) == 0.0
    assert b.open("EUR/USD", Side.BUY, 5) is None


def test_live_account_blocked_by_default(mt5):
    mt5(trade_mode=1, balance=5000)
    from moneyminter.live import MT5Broker, MT5Error
    with pytest.raises(MT5Error, match="Refusing to trade LIVE"):
        MT5Broker()


def test_live_balance_cap_enforced(mt5):
    mt5(trade_mode=1, balance=5000)
    from moneyminter.live import MT5Broker, MT5Error
    with pytest.raises(MT5Error, match="safety cap"):
        MT5Broker(allow_live=True, max_live_balance=500)


def test_live_allowed_when_explicit_and_small(mt5):
    mt5(trade_mode=1, balance=100)
    from moneyminter.live import MT5Broker
    assert MT5Broker(allow_live=True, max_live_balance=500).is_demo is False


def test_algo_trading_disabled_is_detected(mt5):
    m = mt5()
    m.terminal_info = lambda: types.SimpleNamespace(trade_allowed=False)
    from moneyminter.live import MT5Broker, MT5Error
    with pytest.raises(MT5Error, match="Algo Trading is disabled"):
        MT5Broker()


def test_open_sends_sl_tp_and_magic(mt5):
    m = mt5()
    from moneyminter.live import MT5Broker
    b = MT5Broker()
    p = b.open("EUR/USD", Side.BUY, 50_000, stop_loss=1.0980, take_profit=1.1040, strategy="s")
    req = m.state["sent"][-1]
    assert req["symbol"] == "EURUSDm" and req["volume"] == 0.5
    assert req["sl"] == 1.098 and req["tp"] == 1.104
    assert req["magic"] == b.magic
    assert p.id in b.positions


def test_close_roundtrip(mt5):
    mt5()
    from moneyminter.live import MT5Broker
    b = MT5Broker()
    p = b.open("EUR/USD", Side.BUY, 100_000)
    t = b.close(p, reason="tp")
    assert t.pnl == 12.5 and not b.positions and len(b.trades) == 1


def test_requote_is_retried(mt5):
    m = mt5(fail_first=True)
    from moneyminter.live import MT5Broker
    assert MT5Broker().open("EUR/USD", Side.BUY, 100_000) is not None
    assert m.state["calls"]["send"] == 2


def test_dry_run_sends_no_orders(mt5):
    m = mt5()
    from moneyminter.live import MT5Broker
    MT5Broker(dry_run=True).open("EUR/USD", Side.BUY, 100_000)
    assert m.state["sent"] == []


def test_robot_ignores_positions_it_did_not_open(mt5):
    m = mt5()
    from moneyminter.live import MT5Broker
    b = MT5Broker()
    b.open("EUR/USD", Side.BUY, 100_000)
    m.state["ticket"] += 1
    tk = m.state["ticket"]
    m.state["positions"][tk] = mock_mt5._Pos(ticket=tk, symbol="EURUSDm", type=0,
                                             volume=1.0, price_open=1.1, magic=0)
    assert len(m.state["positions"]) == 2
    assert len(b.positions) == 1        # only the magic-tagged one


def test_candles_are_utc_ohlcv(mt5):
    mt5()
    from moneyminter.live import MT5Broker
    df = MT5Broker().candles("EUR/USD", "M5", 100)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert str(df.index.tz) == "UTC" and len(df) == 100


def test_preflight_and_poll(mt5):
    mt5()
    from moneyminter.live import LiveConfig, LiveTrader
    t = LiveTrader(LiveConfig(symbols=["EUR/USD", "GBP/USD"], timeframe="M5"))
    pf = t.preflight()
    assert pf["account"]["demo"] is True
    assert {r["broker_symbol"] for r in pf["symbols"]} == {"EURUSDm", "GBPUSDm"}
    t.poll()
    before = len(t.events)
    t.poll()                              # same bar -> no new action
    assert len(t.events) == before
    assert t.state()["equity"] == 10_000
