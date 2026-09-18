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


# --------------------------------------------------------------- backends
def test_backend_error_lists_all_platform_options(monkeypatch):
    monkeypatch.delitem(sys.modules, "MetaTrader5", raising=False)
    monkeypatch.delitem(sys.modules, "mt5_mac", raising=False)
    monkeypatch.setattr("builtins.__import__", _blocking_import({"MetaTrader5", "mt5_mac"}))
    from moneyminter.live.mt5_broker import MT5Error, _import_mt5
    with pytest.raises(MT5Error) as e:
        _import_mt5()
    msg = str(e.value)
    assert "pip install MetaTrader5" in msg          # Windows
    assert "mt5_mac" in msg                          # macOS
    assert "--mt5-host" in msg                       # remote bridge


def _blocking_import(blocked):
    real = __import__

    def fake(name, *a, **kw):
        if name in blocked:
            raise ImportError(f"blocked {name}")
        return real(name, *a, **kw)
    return fake


def test_mac_backend_is_used_when_available(monkeypatch):
    m = mock_mt5.build()
    monkeypatch.setitem(sys.modules, "mt5_mac", m)
    monkeypatch.delitem(sys.modules, "MetaTrader5", raising=False)
    monkeypatch.setattr("builtins.__import__", _blocking_import({"MetaTrader5"}))
    from moneyminter.live.mt5_broker import _import_mt5
    assert _import_mt5(backend="mac") is m


def test_unreachable_bridge_explains_how_to_start_it():
    from moneyminter.live.mt5_broker import MT5Error, _import_mt5
    with pytest.raises(MT5Error) as e:
        _import_mt5(host="127.0.0.1", port=19998)
    assert "Could not reach the MT5 bridge" in str(e.value)
    assert "mt5linux" in str(e.value)


@pytest.mark.parametrize("backend", ["local", "mac"])
def test_explicit_backend_does_not_silently_fall_back(monkeypatch, backend):
    """Asking for a specific backend must fail loudly, not pick another one."""
    monkeypatch.delitem(sys.modules, "MetaTrader5", raising=False)
    monkeypatch.delitem(sys.modules, "mt5_mac", raising=False)
    monkeypatch.setattr("builtins.__import__", _blocking_import({"MetaTrader5", "mt5_mac"}))
    from moneyminter.live.mt5_broker import MT5Error, _import_mt5
    with pytest.raises(MT5Error):
        _import_mt5(backend=backend)


def test_remote_rpyc_bridge_roundtrip(tmp_path):
    """Spin up a real RPyC server serving a mock terminal and trade through it.

    This is the macOS / Linux+Wine path: the client process has no MetaTrader5
    module at all, everything goes over the socket.
    """
    rpyc = pytest.importorskip("rpyc")
    import subprocess
    import textwrap
    import time

    script = tmp_path / "bridge.py"
    script.write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(__import__('pathlib').Path(__file__).parent)!r})
        import mock_mt5
        sys.modules["MetaTrader5"] = mock_mt5.build(suffix="m")
        from rpyc import SlaveService
        from rpyc.utils.server import ThreadedServer
        ThreadedServer(SlaveService, hostname="127.0.0.1", port=18899,
                       protocol_config={{"allow_all_attrs": True,
                                         "allow_pickle": True}}).start()
    """))
    proc = subprocess.Popen([sys.executable, str(script)])
    try:
        for _ in range(50):                       # wait for the port
            try:
                rpyc.classic.connect("127.0.0.1", 18899).close()
                break
            except Exception:
                time.sleep(0.2)
        else:
            pytest.skip("bridge did not start")

        from moneyminter.live import MT5Broker
        b = MT5Broker(host="127.0.0.1", port=18899)
        assert b.remote is True
        assert b.resolve_symbol("EUR/USD") == "EURUSDm"
        assert b.account_summary()["server"] == "Exness-MT5Trial"
        assert len(b.candles("EUR/USD", "M5", 120)) == 120
        p = b.open("EUR/USD", Side.BUY, 50_000, stop_loss=1.098, take_profit=1.104)
        assert p is not None and p.id in b.positions
        assert b.close(p, reason="tp").pnl == 12.5
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ------------------------------------------------- VPS resilience
def test_equity_raises_instead_of_reporting_zero(mt5):
    """A 0.0 equity would look like a 100% drawdown and latch the kill switch."""
    m = mt5()
    from moneyminter.live import MT5Broker, MT5Error
    b = MT5Broker()
    m.account_info = lambda: None
    with pytest.raises(MT5Error, match="Lost connection"):
        b.equity()
    with pytest.raises(MT5Error, match="Lost connection"):
        b.balance


def test_drawdown_killswitch_not_tripped_by_a_disconnect(mt5):
    """Regression: a momentary dropout must not permanently halt trading."""
    m = mt5()
    from moneyminter.live import LiveConfig, LiveTrader
    t = LiveTrader(LiveConfig(symbols=["EUR/USD"], timeframe="M5"))
    m.account_info = lambda: None          # terminal goes away
    with pytest.raises(Exception):
        t.poll()
    assert t.risk.halted_reason is None    # not latched
    assert t.risk.peak_equity == 10_000    # peak untouched by a bogus 0


def test_is_connected_and_reconnect(mt5):
    m = mt5()
    from moneyminter.live import MT5Broker
    b = MT5Broker()
    assert b.is_connected()
    acct = m.account_info
    m.account_info = lambda: None
    assert not b.is_connected()
    m.account_info = acct                  # link comes back
    assert b.reconnect() and b.is_connected()


def test_one_bad_symbol_does_not_stop_the_others(mt5):
    m = mt5()
    from moneyminter.live import LiveConfig, LiveTrader
    t = LiveTrader(LiveConfig(symbols=["EUR/USD", "GBP/USD"], timeframe="M5"))
    real = m.copy_rates_from_pos
    m.copy_rates_from_pos = lambda name, *a, **k: None if name == "EURUSDm" else real(name, *a, **k)
    t.poll()                                # must not raise
    assert any("EUR/USD" in e["message"] for e in t.events)
    assert "GBP/USD" in t._last_bar         # the healthy symbol still traded


def test_total_failure_escalates_for_reconnect(mt5):
    m = mt5()
    from moneyminter.live import LiveConfig, LiveTrader, MT5Error
    t = LiveTrader(LiveConfig(symbols=["EUR/USD", "GBP/USD"], timeframe="M5"))
    m.copy_rates_from_pos = lambda *a, **k: None
    with pytest.raises(MT5Error):
        t.poll()


def test_order_rejection_is_reported(mt5):
    m = mt5()
    from moneyminter.live import MT5Broker, MT5Error
    b = MT5Broker()
    m.order_send = lambda r: mock_mt5._Result(10019, comment="No money")
    with pytest.raises(MT5Error, match="10019"):
        b.open("EUR/USD", Side.BUY, 100_000)
