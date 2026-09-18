"""Mock MetaTrader5 module so the MT5 adapter can be tested off-Windows.

Emulates an Exness-style account: symbols carry the 'm' suffix, contract size
100k, 0.01 lot steps.
"""
from __future__ import annotations

import sys
import time
import types
from dataclasses import dataclass, field


@dataclass
class _Sym:
    name: str
    digits: int = 5
    point: float = 0.00001
    trade_contract_size: float = 100_000
    volume_min: float = 0.01
    volume_max: float = 200.0
    volume_step: float = 0.01
    spread: int = 7
    visible: bool = True
    filling_mode: int = 1


@dataclass
class _Acct:
    login: int = 123456
    server: str = "Exness-MT5Trial"
    name: str = "Test User"
    currency: str = "USD"
    balance: float = 10_000.0
    equity: float = 10_000.0
    margin: float = 0.0
    margin_free: float = 10_000.0
    leverage: int = 2000
    trade_mode: int = 0  # DEMO


@dataclass
class _Pos:
    ticket: int
    symbol: str
    type: int
    volume: float
    price_open: float
    sl: float = 0.0
    tp: float = 0.0
    profit: float = 0.0
    swap: float = 0.0
    magic: int = 0
    comment: str = ""
    time: int = field(default_factory=lambda: int(time.time()))
    identifier: int = 0


@dataclass
class _Result:
    retcode: int
    order: int = 0
    price: float = 0.0
    comment: str = "ok"


def build(trade_mode=0, balance=10_000.0, suffix="m", fail_first=False):
    m = types.ModuleType("MetaTrader5")

    m.TIMEFRAME_M1, m.TIMEFRAME_M5, m.TIMEFRAME_M15 = 1, 5, 15
    m.TIMEFRAME_M30, m.TIMEFRAME_H1, m.TIMEFRAME_H4, m.TIMEFRAME_D1 = 30, 16385, 16388, 16408
    m.ACCOUNT_TRADE_MODE_DEMO = 0
    m.TRADE_ACTION_DEAL = 1
    m.ORDER_TYPE_BUY, m.ORDER_TYPE_SELL = 0, 1
    m.POSITION_TYPE_BUY, m.POSITION_TYPE_SELL = 0, 1
    m.ORDER_TIME_GTC = 0
    m.ORDER_FILLING_FOK, m.ORDER_FILLING_IOC, m.ORDER_FILLING_RETURN = 0, 1, 2
    m.TRADE_RETCODE_DONE = 10009
    m.TRADE_RETCODE_REQUOTE = 10004
    m.TRADE_RETCODE_PRICE_CHANGED = 10020

    state = {"positions": {}, "ticket": 5000, "price": 1.10000,
             "sent": [], "calls": {"send": 0}}
    m.state = state
    acct = _Acct(trade_mode=trade_mode, balance=balance, equity=balance)
    symbols = {f"EURUSD{suffix}": _Sym(f"EURUSD{suffix}"),
               f"GBPUSD{suffix}": _Sym(f"GBPUSD{suffix}"),
               f"USDJPY{suffix}": _Sym(f"USDJPY{suffix}", digits=3, point=0.001)}

    m.initialize = lambda **kw: True
    m.shutdown = lambda: None
    m.last_error = lambda: (0, "ok")
    m.account_info = lambda: acct
    m.terminal_info = lambda: types.SimpleNamespace(trade_allowed=True)
    m.symbol_info = lambda name: symbols.get(name)
    m.symbols_get = lambda: list(symbols.values())
    m.symbol_select = lambda name, on=True: name in symbols

    def tick(name):
        p = state["price"]
        return types.SimpleNamespace(bid=p - 0.00005, ask=p + 0.00005, time=int(time.time()))
    m.symbol_info_tick = tick

    def rates(name, tf, start, count):
        import numpy as np
        n = count
        base = np.linspace(1.09, 1.11, n) + np.random.default_rng(0).normal(0, 0.0004, n)
        now = int(time.time()) // 60 * 60
        return np.array(
            [(now - (n - i) * 60, base[i], base[i] + 0.0006, base[i] - 0.0006, base[i], 500, 2, 0)
             for i in range(n)],
            dtype=[("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"),
                   ("close", "<f8"), ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")])
    m.copy_rates_from_pos = rates

    def order_send(req):
        state["calls"]["send"] += 1
        state["sent"].append(dict(req))
        if fail_first and state["calls"]["send"] == 1:
            return _Result(m.TRADE_RETCODE_REQUOTE, comment="requote")
        if req.get("position"):                      # closing
            pos = state["positions"].pop(req["position"], None)
            return _Result(m.TRADE_RETCODE_DONE, order=req["position"], price=req["price"])
        state["ticket"] += 1
        t = state["ticket"]
        state["positions"][t] = _Pos(
            ticket=t, symbol=req["symbol"], type=req["type"], volume=req["volume"],
            price_open=req["price"], sl=req.get("sl", 0.0), tp=req.get("tp", 0.0),
            magic=req.get("magic", 0), comment=req.get("comment", ""), profit=12.5, identifier=t)
        return _Result(m.TRADE_RETCODE_DONE, order=t, price=req["price"])
    m.order_send = order_send

    m.positions_get = lambda symbol=None: [
        p for p in state["positions"].values() if symbol is None or p.symbol == symbol]
    return m


def install(**kw):
    m = build(**kw)
    sys.modules["MetaTrader5"] = m
    return m
