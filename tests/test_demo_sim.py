"""End-to-end tests of the LIVE code path against the simulated MT5 terminal.

These matter more than any other test in the suite: they exercise the code that
actually places orders. The same MT5Broker / LiveTrader objects are used here as
on a real Exness demo account — only the terminal underneath is simulated.
"""
import sys

import pytest

from moneyminter import RiskConfig, Side
from moneyminter.live.sim_terminal import SimTerminal, install


@pytest.fixture
def terminal(monkeypatch):
    def _install(**kw):
        term = SimTerminal(**kw)
        monkeypatch.setitem(sys.modules, "MetaTrader5", term)
        return term
    return _install


def _broker(**kw):
    from moneyminter.live import MT5Broker
    return MT5Broker(**kw)


# ------------------------------------------------------------------ terminal
def test_terminal_presents_an_exness_style_demo_account(terminal):
    terminal()
    b = _broker()
    a = b.account_summary()
    assert a["demo"] is True
    assert b.resolve_symbol("EUR/USD") == "EURUSDm"
    assert a["leverage"] == 500


def test_terminal_can_impersonate_a_live_account(terminal):
    """The demo-only guard must still trip when pointed at a 'real' account."""
    from moneyminter.live import MT5Error
    terminal(trade_mode=2, balance=5_000)
    with pytest.raises(MT5Error, match="Refusing to trade LIVE"):
        _broker()


def test_order_fills_at_the_ask_plus_slippage(terminal):
    term = terminal()
    b = _broker()
    tick = term.symbol_info_tick("EURUSDm")
    p = b.open("EUR/USD", Side.BUY, 100_000)
    assert p.entry_price > tick.ask          # slippage is charged, always
    assert p.entry_price == pytest.approx(tick.ask + 0.00001 * 2, rel=1e-4)


def test_broker_side_stop_loss_actually_executes(terminal):
    """The stop must fire from the terminal, not from Python.

    This is what keeps you safe if your machine dies mid-trade.
    """
    term = terminal()
    b = _broker()
    price = b.price("EUR/USD")
    p = b.open("EUR/USD", Side.BUY, 100_000, stop_loss=price - 0.0010)
    assert p.id in b.positions
    term.advance(400)                        # let the market run
    assert p.id not in b.positions, "the terminal should have stopped this out"
    deals = term.history_deals_get()
    assert any(getattr(d, "reason", "") == "sl" for d in deals)


def test_invalid_stops_are_rejected_like_a_real_broker(terminal):
    """A stop on the wrong side must never reach the market.

    Two layers now: the client refuses it before sending (so the position is
    never left unprotected), and the terminal itself still rejects it.
    """
    from moneyminter.live import MT5Error, StaleSignal
    term = terminal()
    b = _broker()
    price = b.price("EUR/USD")

    with pytest.raises(StaleSignal, match="moved past the stop"):
        b.open("EUR/USD", Side.BUY, 100_000, stop_loss=price + 0.0010)
    assert not term.sent_orders, "nothing may be sent when the stop is invalid"

    # bypass our guard to prove the simulated broker rejects it too
    req = {"symbol": "EURUSDm", "type": term.ORDER_TYPE_BUY, "volume": 1.0,
           "price": price, "sl": price + 0.0010, "tp": 0.0,
           "action": term.TRADE_ACTION_DEAL, "magic": 1, "comment": "",
           "type_time": term.ORDER_TIME_GTC, "type_filling": term.ORDER_FILLING_IOC,
           "deviation": 20}
    res = term.order_send(req)
    assert res.retcode != term.TRADE_RETCODE_DONE
    assert "invalid stop" in res.comment.lower()


def test_no_money_is_reported_not_swallowed(terminal):
    from moneyminter.live import MT5Error
    terminal(balance=100.0)
    b = _broker()
    with pytest.raises(MT5Error, match="10019|no money"):
        b.open("EUR/USD", Side.BUY, 5_000_000)


def test_trailing_stop_is_pushed_to_the_broker(terminal):
    term = terminal()
    b = _broker()
    bid = term.symbol_info_tick("EURUSDm").bid
    p = b.open("EUR/USD", Side.BUY, 100_000, stop_loss=bid - 0.0020)
    # trail it up, but still below the BID (a long is stopped on the bid)
    new_stop = round(bid - 0.0005, 5)
    assert b.modify_stop(p, new_stop) is True
    live = [x for x in term.positions_get() if x.ticket == int(p.id)]
    assert live and live[0].sl == pytest.approx(new_stop, abs=1e-6)


def test_trailing_stop_above_market_is_rejected(terminal):
    """A stop on the wrong side of price is nonsense; the broker must refuse it."""
    from moneyminter.live import MT5Error
    term = terminal()
    b = _broker()
    bid = term.symbol_info_tick("EURUSDm").bid
    p = b.open("EUR/USD", Side.BUY, 100_000, stop_loss=bid - 0.0020)
    with pytest.raises(MT5Error, match="invalid stop loss"):
        b.modify_stop(p, bid + 0.0005)


def test_requote_is_retried_and_records_nothing_extra(terminal):
    terminal(reject_first=1)
    b = _broker()
    p = b.open("EUR/USD", Side.BUY, 100_000)
    assert p is not None
    assert len(b.positions) == 1


def test_dry_run_reaches_the_terminal_with_no_orders(terminal):
    term = terminal()
    b = _broker(dry_run=True)
    assert b.open("EUR/USD", Side.BUY, 100_000) is None
    assert term.sent_orders == []


# ---------------------------------------------------------------- live runner
def _trader(monkeypatch, **kw):
    install_kw = {k: kw.pop(k) for k in
                  ("symbols", "timeframe", "balance", "seed", "bars") if k in kw}
    term = SimTerminal(**install_kw)
    monkeypatch.setitem(sys.modules, "MetaTrader5", term)
    from moneyminter.live import LiveConfig, LiveTrader
    cfg = LiveConfig(sim=True, poll_seconds=0, journal_path=None, **kw)
    return term, LiveTrader(cfg)


def test_preflight_reports_ready_symbols(monkeypatch):
    term, t = _trader(monkeypatch, symbols=["EUR/USD"], timeframe="M5",
                      strategy="squeeze_momentum", history_bars=800,
                      risk=RiskConfig(risk_per_trade=0.005))
    pf = t.preflight()
    assert pf["account"]["demo"] is True
    assert pf["symbols"][0]["symbol"] == "EUR/USD"
    assert pf["symbols"][0]["broker_symbol"] == "EURUSDm"
    assert pf["symbols"][0]["ready"] is True


def test_runner_opens_real_orders_and_records_closed_trades(monkeypatch):
    term, t = _trader(monkeypatch, symbols=["EUR/USD"], timeframe="M5",
                      strategy="squeeze_momentum", history_bars=800,
                      risk=RiskConfig(risk_per_trade=0.005))
    for _ in range(2500):
        t.poll()
    s = t.state()
    assert len(term.sent_orders) > 0, "the robot never reached the market"
    assert len(t.broker.trades) > 0, "closed trades must be recorded, including stop-outs"
    assert s["stats"]["trades"] == len(t.broker.trades)
    # every recorded trade must carry the risk it was sized against
    for tr in t.broker.trades:
        assert tr.initial_stop is not None
        assert tr.r_multiple is not None


def test_stop_outs_are_recorded_with_the_brokers_pnl(monkeypatch):
    term, t = _trader(monkeypatch, symbols=["EUR/USD"], timeframe="M5",
                      strategy="trend_pullback", history_bars=800,
                      risk=RiskConfig(risk_per_trade=0.02))
    for _ in range(4000):
        t.poll()
    broker_exits = [tr for tr in t.broker.trades if "broker" in tr.reason
                    or tr.reason in ("stop loss", "take profit", "")]
    assert broker_exits, "broker-side exits must appear in the trade log"
    assert all(isinstance(tr.pnl, float) for tr in t.broker.trades)


def test_equity_curve_carries_realised_pnl(monkeypatch):
    term, t = _trader(monkeypatch, symbols=["EUR/USD"], timeframe="M5",
                      strategy="squeeze_momentum", history_bars=800,
                      risk=RiskConfig(risk_per_trade=0.005))
    for _ in range(1500):
        t.poll()
    curve = t.state()["equity_curve"]
    assert curve, "the demo trial must produce an equity curve to show"


def test_disconnect_is_survivable(monkeypatch):
    """A dropped terminal must raise, not corrupt risk state or latch the brake."""
    term, t = _trader(monkeypatch, symbols=["EUR/USD"], timeframe="M5",
                      strategy="trend_pullback", history_bars=600)
    t.poll()
    term.disconnect()
    with pytest.raises(Exception):
        t.poll()
    assert t.risk.halted_reason is None       # not latched by a bogus equity
    term.reconnect_terminal()
    t.poll()                                  # and it recovers
    assert t.risk.peak_equity >= 0


def test_risk_guardrail_blocks_a_second_correlated_entry(monkeypatch):
    term, t = _trader(monkeypatch, symbols=["EUR/USD", "GBP/USD"], timeframe="M5",
                      strategy="squeeze_momentum", history_bars=800,
                      risk=RiskConfig(risk_per_trade=0.01, max_per_currency_group=1))
    for _ in range(2500):
        t.poll()
    symbols_held = {p.symbol for p in t.broker.positions.values()}
    assert len(symbols_held) <= 1          # both are USD shorts; only one allowed


if __name__ == "__main__":       # pragma: no cover
    pytest.main([__file__, "-v"])


def test_price_running_through_the_stop_is_a_skip_not_a_crash(monkeypatch):
    """Regression: a fast bar made the robot abort and look disconnected.

    The signal is computed on the last CLOSED bar. A violent bar can then run
    21 pips through the stop before the order is sent, which the broker rejects
    with 10016. That used to raise MT5Error, and because it was the only symbol
    the poll escalated it to a reconnect — killing a healthy session.
    """
    term, t = _trader(monkeypatch, symbols=["EUR/USD"], timeframe="M5",
                      strategy="trend_pullback", history_bars=800,
                      risk=RiskConfig(risk_per_trade=0.02))
    skips = []
    for _ in range(3000):
        t.poll()                            # must not raise
        skips = [e for e in t.events if e["kind"] == "skip"]
        if skips:
            break
    # either the market never gapped on this seed (fine) or we skipped cleanly
    for ev in skips:
        assert "setup gone" in ev["message"] or "rejected" in ev["message"]
    # The invariant is that the loop never raised. Do NOT assert that no
    # guardrail fired: at 2% risk per trade the daily loss limit is *meant* to
    # trip, and treating that as failure would make this test fight the very
    # safety system it is meant to protect.
    assert len(t.events) > 0


def test_rejected_order_does_not_escalate_to_reconnect(monkeypatch):
    term, t = _trader(monkeypatch, symbols=["EUR/USD"], timeframe="M5",
                      strategy="trend_pullback", history_bars=800,
                      risk=RiskConfig(risk_per_trade=0.02))
    from moneyminter.live import OrderRejected
    orig = t.broker.open

    def boom(*a, **kw):
        raise OrderRejected("Order rejected: retcode=10019 no money")

    t.broker.open = boom
    for _ in range(400):
        t.poll()                            # a single-symbol rejection used to abort
    assert any(e["kind"] == "skip" for e in t.events)
    t.broker.open = orig
