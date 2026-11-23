"""A simulated MetaTrader 5 terminal.

Why this exists
---------------
Money Minter's live/demo path is the part that matters and the part that is
hardest to test: you need Windows, a broker, a funded demo account and hours of
waiting to see a single order. That means the code that actually places orders
is the least exercised code in most trading projects — exactly backwards.

``SimTerminal`` implements the subset of the ``MetaTrader5`` Python API that
:class:`moneyminter.live.mt5_broker.MT5Broker` uses, backed by the synthetic
price generator instead of a real broker. As a result:

* ``MT5Broker``, ``LiveTrader`` and every line of order, stop-loss, reconciliation
  and reconnect logic run **unmodified** against it,
* a full day of M5 trading takes seconds instead of a day,
* it is deterministic, so tests can assert on real fills.

It is not a market and it cannot tell you whether a strategy is profitable. It
tells you whether your *plumbing* works — which is what a demo account is for.

Usage::

    from moneyminter.live.sim_terminal import SimTerminal, install
    install()                       # sys.modules["MetaTrader5"] = SimTerminal(...)
    # ... then use MT5Broker()/LiveTrader() exactly as on Windows
"""
from __future__ import annotations

import math
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..data.feeds import SyntheticFeed, timeframe_seconds
from ..instruments import get_instrument, normalize

# ----------------------------------------------------------------- constants
TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15 = 1, 5, 15
TIMEFRAME_M30, TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1 = 30, 16385, 16388, 16408

ACCOUNT_TRADE_MODE_DEMO = 0
ACCOUNT_TRADE_MODE_CONTEST = 1
ACCOUNT_TRADE_MODE_REAL = 2

TRADE_ACTION_DEAL = 1
TRADE_ACTION_SLTP = 6
ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
ORDER_TIME_GTC = 0
ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2

TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_REQUOTE = 10004
TRADE_RETCODE_INVALID_STOPS = 10016
TRADE_RETCODE_NO_MONEY = 10019
TRADE_RETCODE_PRICE_CHANGED = 10020

_TF_SECONDS = {TIMEFRAME_M1: 60, TIMEFRAME_M5: 300, TIMEFRAME_M15: 900,
               TIMEFRAME_M30: 1800, TIMEFRAME_H1: 3600, TIMEFRAME_H4: 14400,
               TIMEFRAME_D1: 86400}
_TF_LABEL = {v: k for k, v in {"M1": TIMEFRAME_M1, "M5": TIMEFRAME_M5,
                               "M15": TIMEFRAME_M15, "M30": TIMEFRAME_M30,
                               "H1": TIMEFRAME_H1, "H4": TIMEFRAME_H4,
                               "D1": TIMEFRAME_D1}.items()}


@dataclass
class _Symbol:
    name: str
    digits: int = 5
    point: float = 0.00001
    trade_contract_size: float = 100_000
    volume_min: float = 0.01
    volume_max: float = 200.0
    volume_step: float = 0.01
    spread: int = 6
    visible: bool = True
    filling_mode: int = 1


@dataclass
class _Account:
    login: int = 78_234_001
    server: str = "MoneyMinter-Sim"
    name: str = "Simulated Demo"
    currency: str = "USD"
    balance: float = 10_000.0
    equity: float = 10_000.0
    margin: float = 0.0
    margin_free: float = 10_000.0
    leverage: int = 500
    trade_mode: int = ACCOUNT_TRADE_MODE_DEMO


@dataclass
class _Position:
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
    time: int = 0
    identifier: int = 0


@dataclass
class _OrderResult:
    retcode: int = TRADE_RETCODE_DONE
    order: int = 0
    deal: int = 0
    price: float = 0.0
    volume: float = 0.0
    comment: str = "Done"
    request: dict = field(default_factory=dict)


class SimTerminal:
    """An in-process fake MT5 terminal with server-side SL/TP."""

    # expose MT5 constants as attributes, exactly like the real module
    TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15 = TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15
    TIMEFRAME_M30, TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1 = (
        TIMEFRAME_M30, TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1)
    ACCOUNT_TRADE_MODE_DEMO = ACCOUNT_TRADE_MODE_DEMO
    ACCOUNT_TRADE_MODE_CONTEST = ACCOUNT_TRADE_MODE_CONTEST
    ACCOUNT_TRADE_MODE_REAL = ACCOUNT_TRADE_MODE_REAL
    TRADE_ACTION_DEAL = TRADE_ACTION_DEAL
    TRADE_ACTION_SLTP = TRADE_ACTION_SLTP
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = ORDER_TYPE_BUY, ORDER_TYPE_SELL
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = POSITION_TYPE_BUY, POSITION_TYPE_SELL
    ORDER_TIME_GTC = ORDER_TIME_GTC
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = (
        ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN)
    TRADE_RETCODE_DONE = TRADE_RETCODE_DONE
    TRADE_RETCODE_REQUOTE = TRADE_RETCODE_REQUOTE
    TRADE_RETCODE_INVALID_STOPS = TRADE_RETCODE_INVALID_STOPS
    TRADE_RETCODE_NO_MONEY = TRADE_RETCODE_NO_MONEY
    TRADE_RETCODE_PRICE_CHANGED = TRADE_RETCODE_PRICE_CHANGED

    def __init__(self, symbols: Optional[List[str]] = None, timeframe: str = "M5",
                 bars: int = 4000, seed: int = 42, balance: float = 10_000.0,
                 suffix: str = "m", trade_mode: int = ACCOUNT_TRADE_MODE_DEMO,
                 trade_allowed: bool = True, commission_per_lot: float = 3.5,
                 reject_first: int = 0, slip_points: int = 2):
        self.symbols_req = [normalize(s) for s in (symbols or ["EUR/USD", "GBP/USD", "USD/JPY"])]
        self.timeframe = timeframe.upper()
        self.seed = seed
        self.balance = balance
        self.suffix = suffix
        self.trade_allowed = trade_allowed
        self.commission_per_lot = commission_per_lot
        self.reject_first = reject_first
        self.slip_points = slip_points

        self._acct = _Account(balance=balance, equity=balance,
                              trade_mode=trade_mode,
                              login=78234001 if trade_mode == 0 else 99110042)
        self._positions: Dict[int, _Position] = {}
        self._closed: List[dict] = []
        self._closed_by_server: set[int] = set()
        self._ticket = 100_000
        self._sent: List[dict] = []
        self._calls = {"send": 0, "init": 0}
        self._last_error = (0, "ok")
        self._connected = True
        self._initialized = False

        # one price path per symbol; ``cursor`` is the index of the last closed bar
        self._data: Dict[str, pd.DataFrame] = {}
        self._cursor: Dict[str, int] = {}
        self._symbols: Dict[str, _Symbol] = {}
        step = timeframe_seconds(self.timeframe)
        for i, sym in enumerate(self.symbols_req):
            inst = get_instrument(sym)
            feed = SyntheticFeed(sym, self.timeframe, bars=bars, seed=seed + i * 13)
            self._data[sym] = feed.df
            self._cursor[sym] = len(feed.df) // 2        # start "live" mid-history
            self._symbols[self._broker_name(sym)] = _Symbol(
                name=self._broker_name(sym),
                digits=3 if inst.quote == "JPY" else 5,
                point=0.001 if inst.quote == "JPY" else 0.00001,
                trade_contract_size=inst.contract_size,
                volume_min=inst.min_lot, volume_step=inst.lot_step,
                volume_max=inst.max_lot,
                spread=int(round(inst.typical_spread_pips * 10)))
        self._step = step

    # ---------------------------------------------------------------- helpers
    def _broker_name(self, symbol: str) -> str:
        return symbol.replace("/", "").upper() + self.suffix

    def _internal(self, broker_name: str) -> Optional[str]:
        for sym in self._data:
            if self._broker_name(sym) == broker_name:
                return sym
        if broker_name in self._data:
            return broker_name
        return None

    def _bar(self, sym: str) -> pd.Series:
        return self._data[sym].iloc[self._cursor[sym]]

    def _price(self, broker_name: str) -> float:
        sym = self._internal(broker_name)
        if sym is None:
            return 0.0
        return float(self._bar(sym)["close"])

    def _spread(self, broker_name: str) -> float:
        s = self._symbols.get(broker_name)
        return (s.point * s.spread) if s else 0.0001

    def _now_ts(self, broker_name: str) -> int:
        sym = self._internal(broker_name)
        if sym is None:
            return int(datetime.now(timezone.utc).timestamp())
        return int(self._data[sym].index[self._cursor[sym]].timestamp())

    # ---------------------------------------------------------------- MT5 API
    def initialize(self, **kwargs) -> bool:
        self._calls["init"] += 1
        self._initialized = True
        self._connected = True
        return True

    def shutdown(self) -> None:
        self._initialized = False

    def last_error(self):
        return self._last_error

    def account_info(self):
        if not self._connected:
            return None
        self._revalue()
        a = self._acct
        return types.SimpleNamespace(
            login=a.login, server=a.server, name=a.name, currency=a.currency,
            balance=self.balance, equity=a.equity, margin=a.margin,
            margin_free=a.margin_free, leverage=a.leverage, trade_mode=a.trade_mode)

    def terminal_info(self):
        return types.SimpleNamespace(trade_allowed=self.trade_allowed,
                                     connected=self._connected,
                                     company="MoneyMinter Sim", path="/sim")

    def symbol_info(self, name: str):
        return self._symbols.get(name)

    def symbols_get(self):
        return list(self._symbols.values())

    def symbol_select(self, name: str, enable: bool = True) -> bool:
        s = self._symbols.get(name)
        if s is None:
            return False
        s.visible = enable
        return True

    def symbol_info_tick(self, name: str):
        sym = self._internal(name)
        if sym is None:
            return None
        # price moves intrabar: jitter around the current bar's close
        bar = self._bar(sym)
        inst = get_instrument(sym)
        rng = np.random.default_rng(hash((name, self._cursor[sym])) % (2 ** 32))
        jitter = rng.normal(0, inst.pip * 0.8)
        mid = float(np.clip(bar["close"] + jitter, bar["low"], bar["high"]))
        half = self._spread(name) / 2
        return types.SimpleNamespace(bid=round(mid - half, 8), ask=round(mid + half, 8),
                                     time=self._now_ts(name),
                                     volume=float(bar.get("volume", 0) or 0))

    def copy_rates_from_pos(self, name: str, timeframe, start: int, count: int):
        sym = self._internal(name)
        if sym is None:
            self._last_error = (-1, f"unknown symbol {name}")
            return None
        end = self._cursor[sym] + 1 - start
        begin = max(0, end - count)
        df = self._data[sym].iloc[begin:end]
        if df.empty:
            return None
        out = np.empty(len(df), dtype=[
            ("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"),
            ("close", "<f8"), ("tick_volume", "<u8"), ("spread", "<i4"),
            ("real_volume", "<u8")])
        out["time"] = df.index.astype("int64") // 10 ** 9
        out["open"], out["high"] = df["open"].to_numpy(), df["high"].to_numpy()
        out["low"], out["close"] = df["low"].to_numpy(), df["close"].to_numpy()
        out["tick_volume"] = np.nan_to_num(df["volume"].to_numpy()).astype("u8")
        out["spread"] = self._symbols[name].spread
        return out

    def positions_get(self, symbol: Optional[str] = None):
        self._revalue()
        rows = [p for p in self._positions.values()
                if symbol is None or p.symbol == symbol]
        return [types.SimpleNamespace(**p.__dict__) for p in rows]

    def orders_get(self, symbol: Optional[str] = None):
        return []

    def history_deals_get(self, *args, **kwargs):
        return [types.SimpleNamespace(**d) for d in self._closed[-500:]]

    def order_send(self, request: dict):
        self._calls["send"] += 1
        self._sent.append(dict(request))
        if not self._connected:
            self._last_error = (-10004, "no connection")
            return _OrderResult(retcode=TRADE_RETCODE_REQUOTE, comment="no connection")
        if self.reject_first and self._calls["send"] <= self.reject_first:
            return _OrderResult(retcode=TRADE_RETCODE_REQUOTE, comment="requote")

        sym_name = request["symbol"]
        sym = self._symbols.get(sym_name)
        if sym is None:
            return _OrderResult(retcode=TRADE_RETCODE_INVALID_STOPS,
                                comment=f"unknown symbol {sym_name}")
        tick = self.symbol_info_tick(sym_name)

        # SLTP must be checked BEFORE the "has a position key" close branch:
        # a modify-stop request also carries "position", and routing it to
        # _close would flatten the trade instead of moving the stop.
        if request.get("action") == TRADE_ACTION_SLTP:
            ticket = int(request.get("position") or 0)
            p = self._positions.get(ticket)
            if p is None:
                return _OrderResult(retcode=TRADE_RETCODE_INVALID_STOPS,
                                    comment="position not found")
            # a long's stop is triggered by the BID; a short's by the ASK
            cur = (tick.bid if p.type == POSITION_TYPE_BUY else tick.ask)
            sl = float(request.get("sl") or 0.0)
            if sl and ((p.type == POSITION_TYPE_BUY and sl >= cur) or
                       (p.type == POSITION_TYPE_SELL and sl <= cur)):
                return _OrderResult(retcode=TRADE_RETCODE_INVALID_STOPS,
                                    comment="invalid stop loss")
            p.sl = sl
            p.tp = float(request.get("tp") or 0.0)
            return _OrderResult(retcode=TRADE_RETCODE_DONE, order=ticket,
                                price=cur, comment="sltp updated")

        if request.get("position"):
            return self._close(request, tick, sym)

        # ---- opening deal ------------------------------------------------
        is_buy = request["type"] == ORDER_TYPE_BUY
        slip = sym.point * self.slip_points
        fill = (tick.ask + slip) if is_buy else (tick.bid - slip)
        lots = float(request["volume"])
        sl, tp = float(request.get("sl") or 0.0), float(request.get("tp") or 0.0)
        # broker rejects stops on the wrong side (real MT5 behaviour)
        # stops are validated against the price that will trigger them:
        # a long is stopped on the bid, a short on the ask
        trigger = tick.bid if is_buy else tick.ask
        if sl:
            if (is_buy and sl >= trigger) or (not is_buy and sl <= trigger):
                return _OrderResult(retcode=TRADE_RETCODE_INVALID_STOPS,
                                    comment="invalid stop loss")
        if tp:
            if (is_buy and tp <= trigger) or (not is_buy and tp >= trigger):
                return _OrderResult(retcode=TRADE_RETCODE_INVALID_STOPS,
                                    comment="invalid take profit")
        inst = get_instrument(self._internal(sym_name))
        notional = lots * sym.trade_contract_size * fill
        margin = notional / max(self._acct.leverage, 1)
        if margin > self._acct.margin_free:
            return _OrderResult(retcode=TRADE_RETCODE_NO_MONEY, comment="no money")

        self._ticket += 1
        p = _Position(ticket=self._ticket, symbol=sym_name,
                      type=POSITION_TYPE_BUY if is_buy else POSITION_TYPE_SELL,
                      volume=lots, price_open=round(fill, sym.digits),
                      sl=sl, tp=tp, magic=int(request.get("magic", 0)),
                      comment=request.get("comment", ""),
                      time=self._now_ts(sym_name), identifier=self._ticket)
        self._positions[p.ticket] = p
        self._revalue()
        return _OrderResult(retcode=TRADE_RETCODE_DONE, order=p.ticket, deal=p.ticket,
                            price=p.price_open, volume=lots, comment="Done",
                            request=request)

    def _close(self, request: dict, tick, sym: _Symbol) -> _OrderResult:
        ticket = int(request["position"])
        p = self._positions.get(ticket)
        if p is None:
            return _OrderResult(retcode=TRADE_RETCODE_INVALID_STOPS,
                                comment="position not found")
        if ticket in self._closed_by_server:
            # the terminal's own SL/TP already flattened it before our close
            # order landed — a real race, and one the runner must cope with
            return _OrderResult(retcode=TRADE_RETCODE_INVALID_STOPS,
                                comment="position already closed")
        is_buy = p.type == POSITION_TYPE_BUY
        slip = sym.point * self.slip_points
        fill = (tick.bid - slip) if is_buy else (tick.ask + slip)
        profit = self._position_profit(p, fill)
        commission = self.commission_per_lot * p.volume
        self.balance += profit - commission
        self._closed.append({"ticket": ticket, "symbol": p.symbol, "volume": p.volume,
                             "price": fill, "profit": profit - commission,
                             "time": self._now_ts(p.symbol), "magic": p.magic,
                             "comment": p.comment})
        del self._positions[ticket]
        self._revalue()
        return _OrderResult(retcode=TRADE_RETCODE_DONE, order=ticket, deal=ticket,
                            price=fill, volume=p.volume, comment="Closed")

    # ---------------------------------------------------------------- engine
    def _position_profit(self, p: _Position, price: float) -> float:
        sym = self._internal(p.symbol)
        if sym is None:
            return 0.0
        inst = get_instrument(sym)
        sign = 1 if p.type == POSITION_TYPE_BUY else -1
        # convert quote-currency P&L into the account currency (USD)
        return (price - p.price_open) * sign * p.volume * inst.contract_size * inst.usd_per_quote()

    def closed_deal(self, ticket: str) -> Optional[dict]:
        for d in reversed(self._closed):
            if str(d.get("ticket")) == str(ticket):
                return d
        return None

    def _revalue(self) -> None:
        eq = self.balance
        margin = 0.0
        for p in self._positions.values():
            tick = self.symbol_info_tick(p.symbol)
            if tick is None:
                continue
            px = tick.bid if p.type == POSITION_TYPE_BUY else tick.ask
            p.profit = self._position_profit(p, px)
            eq += p.profit + p.swap
            inst_sym = self._internal(p.symbol)
            notional = p.volume * self._symbols[p.symbol].trade_contract_size * px
            margin += notional / max(self._acct.leverage, 1)
        self._acct.equity = eq
        self._acct.margin = margin
        self._acct.margin_free = eq - margin

    def advance(self, bars: int = 1) -> int:
        """Move the virtual clock forward by ``bars`` closed bars.

        This is what makes a demo test feasible: a 6-hour M5 session is 72 bars,
        which takes milliseconds instead of six hours of wall time.
        """
        moved = 0
        start = dict(self._cursor)
        for sym in self._data:
            new = min(self._cursor[sym] + bars, len(self._data[sym]) - 2)
            if new > self._cursor[sym]:
                self._cursor[sym] = new
                moved += 1
        # Walk every bar between the old and new cursor: a stop that was
        # breached on an intermediate bar must still fire, exactly as it would
        # have done in real time. Checking only the final bar would let a
        # position survive a stop it genuinely hit.
        for sym, old in start.items():
            for i in range(old + 1, self._cursor[sym] + 1):
                self._enforce_server_stops(bar_index=i)
        self._enforce_server_stops()
        self._revalue()
        return moved

    def _enforce_server_stops(self, bar_index: Optional[int] = None) -> None:
        """Execute the SL/TP the broker is holding, like a real terminal.

        This is the behaviour that keeps a trader safe if their PC dies, so the
        simulator has to model it — a strategy whose stops only exist in Python
        would silently pass its demo test here and fail in production.
        """
        for ticket, p in list(self._positions.items()):
            sym = self._internal(p.symbol)
            if sym is None:
                continue
            bar = (self._data[sym].iloc[bar_index] if bar_index is not None
                   else self._bar(sym))
            is_buy = p.type == POSITION_TYPE_BUY
            exit_px = None
            reason = ""
            if is_buy:
                if p.sl and float(bar["low"]) <= p.sl:
                    exit_px, reason = p.sl, "sl"
                elif p.tp and float(bar["high"]) >= p.tp:
                    exit_px, reason = p.tp, "tp"
            else:
                if p.sl and float(bar["high"]) >= p.sl:
                    exit_px, reason = p.sl, "sl"
                elif p.tp and float(bar["low"]) <= p.tp:
                    exit_px, reason = p.tp, "tp"
            if exit_px is None:
                continue
            profit = self._position_profit(p, exit_px)
            commission = self.commission_per_lot * p.volume
            self.balance += profit - commission
            self._closed.append({"ticket": ticket, "symbol": p.symbol, "volume": p.volume,
                                 "price": exit_px, "profit": profit - commission,
                                 "time": self._now_ts(p.symbol), "reason": reason,
                                 "magic": p.magic, "comment": p.comment})
            self._closed_by_server.add(ticket)
            del self._positions[ticket]
        self._revalue()

    # ---------------------------------------------------------------- control
    def disconnect(self) -> None:
        """Simulate the terminal going away, for reconnect tests."""
        self._connected = False

    def reconnect_terminal(self) -> None:
        self._connected = True

    def snapshot(self) -> dict:
        return {"balance": self.balance, "equity": self._acct.equity,
                "positions": len(self._positions), "orders_sent": len(self._sent),
                "closed": len(self._closed),
                "cursor": dict(self._cursor)}

    @property
    def sent_orders(self) -> List[dict]:
        return list(self._sent)


def install(terminal: Optional[SimTerminal] = None, **kwargs) -> SimTerminal:
    """Install a simulated terminal as the ``MetaTrader5`` module.

    After this call, ``MT5Broker``/``LiveTrader`` behave exactly as they would
    on Windows with a real demo account, but with no terminal, no network and no
    waiting.
    """
    terminal = terminal or SimTerminal(**kwargs)
    sys.modules["MetaTrader5"] = terminal  # type: ignore[assignment]
    return terminal
