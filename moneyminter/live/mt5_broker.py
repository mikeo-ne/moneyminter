"""MetaTrader 5 broker adapter — trades a real Exness (or any MT5 broker) account.

Requires the ``MetaTrader5`` package, which is **Windows-only**, with the MT5
terminal installed, logged in, and "Algo Trading" enabled.

    pip install MetaTrader5

Safety model
------------
Every instance is bound to one account and refuses to place orders unless:

* the terminal reports ``trade_allowed`` (Algo Trading toggle is on), and
* the account is a DEMO account, **or** ``allow_live=True`` was passed
  explicitly *and* the account balance is under ``max_live_balance``.

That makes it impossible to point this at a funded live account by accident.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd

from ..broker import Broker
from ..models import Position, Side, Trade, utcnow

log = logging.getLogger("moneyminter.mt5")

#: MoneyMinter timeframe -> MT5 timeframe constant name
_TF = {"M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15",
       "M30": "TIMEFRAME_M30", "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4",
       "D1": "TIMEFRAME_D1"}

#: Symbol suffixes used by MT5 brokers. Exness standard/cent accounts use "m",
#: Exness Pro/Zero/Raw often use plain names; others use "." / "_" / "micro".
_SUFFIXES = ["", "m", "z", "c", ".", "_", "micro", ".a", "-ECN", "ecn", "pro", "#"]


class MT5Error(RuntimeError):
    pass


def _import_mt5(host: Optional[str] = None, port: int = 18812, backend: str = "auto"):
    """Return a MetaTrader5-compatible module.

    Backends, in the order ``auto`` tries them:

    ``local``   the official ``MetaTrader5`` package (Windows only).
    ``mac``     ``mt5_mac`` — drives the Wine runtime bundled inside
                MetaTrader 5.app on macOS (Intel and Apple Silicon).
    ``rpyc``    a remote bridge exposing the MT5 API over RPyC. Works with
                ``mt5linux``/``mt5-server`` (Linux+Wine, port 18812) and
                ``siliconmetatrader5`` (Docker on Apple Silicon, port 8001),
                or a Windows box on your LAN running the same server.
    """
    if backend in ("auto", "rpyc") and host:
        return _connect_rpyc(host, port)

    if backend in ("auto", "local"):
        try:
            import MetaTrader5 as mt5  # type: ignore
            return mt5
        except ImportError:
            if backend == "local":
                raise MT5Error(_WINDOWS_ONLY_HELP) from None

    if backend in ("auto", "mac"):
        try:
            import mt5_mac  # type: ignore
            log.info("Using the mt5_mac backend (MetaTrader 5.app bundled Wine)")
            return mt5_mac
        except ImportError:
            if backend == "mac":
                raise MT5Error(
                    "The 'mt5_mac' package is required for the macOS backend.\n"
                    "  pip install mt5_mac\n"
                    "It also needs MetaTrader 5 installed from metatrader5.com "
                    "at /Applications/MetaTrader 5.app"
                ) from None

    raise MT5Error(_WINDOWS_ONLY_HELP)


def _connect_rpyc(host: str, port: int):
    try:
        import rpyc  # type: ignore
    except ImportError:
        raise MT5Error("Remote MT5 needs rpyc:  pip install rpyc") from None
    try:
        conn = rpyc.classic.connect(host, port)
    except Exception as exc:  # noqa: BLE001
        raise MT5Error(
            f"Could not reach the MT5 bridge at {host}:{port} ({exc}).\n"
            "Start the bridge on the machine running MetaTrader 5:\n"
            "  Linux/Wine : python -m mt5linux <path-to-windows-python.exe>\n"
            "  Windows    : python -m mt5linux\n"
            "  Apple M-series: docker compose up  (siliconmetatrader5, port 8001)"
        ) from exc
    log.info("Connected to remote MT5 bridge at %s:%s", host, port)
    mt5 = conn.modules.MetaTrader5
    mt5._mm_rpyc_conn = conn   # keep a reference so the socket stays open
    return mt5


_WINDOWS_ONLY_HELP = (
    "No MetaTrader 5 backend available.\n"
    "The official 'MetaTrader5' package only runs on Windows. Options:\n"
    "  Windows      : pip install MetaTrader5\n"
    "  macOS        : pip install mt5_mac        (then --mt5-backend mac)\n"
    "  Linux/macOS  : run MT5 under Wine or on another machine and start a\n"
    "                 bridge, then pass --mt5-host <ip> [--mt5-port 18812]\n"
    "Until then, everything else works offline: `python -m moneyminter trade`."
)



class MT5Broker(Broker):
    """Live/demo broker backed by a MetaTrader 5 terminal."""

    def __init__(self, login: Optional[int] = None, password: Optional[str] = None,
                 server: Optional[str] = None, path: Optional[str] = None,
                 allow_live: bool = False, max_live_balance: float = 500.0,
                 magic: int = 990707, deviation: int = 20, symbol_suffix: Optional[str] = None,
                 dry_run: bool = False, host: Optional[str] = None, port: int = 18812,
                 backend: str = "auto"):
        self.mt5 = _import_mt5(host=host, port=port, backend=backend)
        self.remote = bool(host)
        self.allow_live = allow_live
        self.max_live_balance = max_live_balance
        self.magic = magic
        self.deviation = deviation
        self.dry_run = dry_run
        self._suffix = symbol_suffix
        self._symbol_cache: Dict[str, str] = {}
        self.trades: List[Trade] = []
        self.equity_curve: List[tuple] = []

        kwargs = {}
        if path:
            kwargs["path"] = path
        if login:
            kwargs.update(login=int(login), password=password, server=server)
        if not self.mt5.initialize(**kwargs):
            raise MT5Error(f"MT5 initialize() failed: {self.mt5.last_error()}. "
                           "Is the terminal running and logged in?")

        info = self.mt5.account_info()
        if info is None:
            raise MT5Error(f"Could not read account info: {self.mt5.last_error()}")
        self.account = info
        self.is_demo = info.trade_mode == self.mt5.ACCOUNT_TRADE_MODE_DEMO
        self.initial_balance = float(info.balance)
        self.currency = info.currency

        self._guard_account()
        log.info("Connected to %s account #%s (%s) balance %.2f %s",
                 "DEMO" if self.is_demo else "LIVE", info.login, info.server,
                 info.balance, info.currency)

    # ------------------------------------------------------------ safety
    def _guard_account(self) -> None:
        term = self.mt5.terminal_info()
        if term is not None and not term.trade_allowed:
            raise MT5Error(
                "Algo Trading is disabled in the MT5 terminal. Enable it: "
                "Tools > Options > Expert Advisors > 'Allow algorithmic trading', "
                "and click the 'Algo Trading' toolbar button."
            )
        if not self.is_demo and not self.allow_live:
            raise MT5Error(
                f"Refusing to trade LIVE account #{self.account.login} "
                f"(balance {self.account.balance:.2f} {self.currency}).\n"
                "Money Minter defaults to demo-only. To override you must pass "
                "allow_live=True (CLI: --i-understand-live-risk)."
            )
        if not self.is_demo and self.account.balance > self.max_live_balance:
            raise MT5Error(
                f"Live account balance {self.account.balance:.2f} {self.currency} exceeds the "
                f"{self.max_live_balance:.2f} safety cap. Raise it deliberately with "
                "--max-live-balance if you really mean to risk this much."
            )

    # ------------------------------------------------------------ symbols
    def resolve_symbol(self, symbol: str) -> str:
        """Map 'EUR/USD' to the broker's actual symbol, e.g. Exness 'EURUSDm'."""
        if symbol in self._symbol_cache:
            return self._symbol_cache[symbol]
        base = symbol.replace("/", "").upper()
        candidates = [base + self._suffix] if self._suffix is not None else \
            [base + s for s in _SUFFIXES]
        for cand in candidates:
            info = self.mt5.symbol_info(cand)
            if info is not None:
                if not info.visible and not self.mt5.symbol_select(cand, True):
                    continue
                self._symbol_cache[symbol] = cand
                if cand != base:
                    log.info("Resolved %s -> %s", symbol, cand)
                return cand
        # last resort: scan everything the broker offers
        for info in (self.mt5.symbols_get() or []):
            if info.name.upper().startswith(base):
                self.mt5.symbol_select(info.name, True)
                self._symbol_cache[symbol] = info.name
                return info.name
        raise MT5Error(f"Symbol {symbol} not found on this account (tried {candidates}). "
                       "Check Market Watch, or pass --symbol-suffix.")

    def spec(self, symbol: str):
        return self.mt5.symbol_info(self.resolve_symbol(symbol))

    # ------------------------------------------------------------ data
    def price(self, symbol: str) -> float:
        t = self.mt5.symbol_info_tick(self.resolve_symbol(symbol))
        if t is None:
            raise MT5Error(f"No tick for {symbol}: {self.mt5.last_error()}")
        return (t.bid + t.ask) / 2

    def candles(self, symbol: str, timeframe: str = "M1", bars: int = 500) -> pd.DataFrame:
        tf = getattr(self.mt5, _TF[timeframe.upper()])
        rates = self.mt5.copy_rates_from_pos(self.resolve_symbol(symbol), tf, 0, bars)
        if rates is None or len(rates) == 0:
            raise MT5Error(f"No history for {symbol} {timeframe}: {self.mt5.last_error()}")
        df = pd.DataFrame(rates)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("timestamp").rename(columns={"tick_volume": "volume"})
        return df[["open", "high", "low", "close", "volume"]].astype(float)

    # ------------------------------------------------------------ sizing
    def units_to_lots(self, symbol: str, units: float) -> float:
        """Convert Money Minter 'units' of base currency into broker lots.

        Returns 0.0 when the risk-based size is smaller than the broker's
        minimum lot. Rounding *up* to the minimum would silently trade far more
        than the risk manager authorised (on a small account 0.01 lots can be
        many multiples of the intended risk), so we skip the trade instead.
        """
        s = self.spec(symbol)
        lots = units / (s.trade_contract_size or 100_000)
        step = s.volume_step or 0.01
        lots = math.floor(lots / step) * step
        if lots < s.volume_min:
            log.warning(
                "%s: risk-based size %.4f lots is below the broker minimum %.2f — "
                "skipping trade. Taking it would risk %.1fx your limit. "
                "Use a smaller timeframe/wider stop, or a cent account.",
                symbol, lots, s.volume_min,
                (s.volume_min * s.trade_contract_size / units) if units else 0)
            return 0.0
        return round(min(lots, s.volume_max), 2)

    # ------------------------------------------------------------ trading
    def _send(self, request: dict) -> "object":
        if self.dry_run:
            log.warning("DRY RUN, not sending: %s", request)
            return None
        for attempt in range(3):
            result = self.mt5.order_send(request)
            if result is None:
                raise MT5Error(f"order_send returned None: {self.mt5.last_error()}")
            if result.retcode == self.mt5.TRADE_RETCODE_DONE:
                return result
            # requote / price-changed are worth retrying with a fresh price
            if result.retcode in (self.mt5.TRADE_RETCODE_REQUOTE,
                                  self.mt5.TRADE_RETCODE_PRICE_CHANGED) and attempt < 2:
                time.sleep(0.4)
                tick = self.mt5.symbol_info_tick(request["symbol"])
                if request["type"] == self.mt5.ORDER_TYPE_BUY:
                    request["price"] = tick.ask
                else:
                    request["price"] = tick.bid
                continue
            raise MT5Error(f"Order rejected: retcode={result.retcode} {result.comment}")
        raise MT5Error("Order failed after retries")

    def open(self, symbol, side: Side, units, price=None, ts=None,
             stop_loss=None, take_profit=None, strategy=""):
        broker_sym = self.resolve_symbol(symbol)
        lots = self.units_to_lots(symbol, units)
        if lots <= 0:
            log.warning("Computed lot size 0 for %s (%s units) — skipping", symbol, units)
            return None
        tick = self.mt5.symbol_info_tick(broker_sym)
        s = self.spec(symbol)
        is_buy = side is Side.BUY
        entry = tick.ask if is_buy else tick.bid

        req = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": broker_sym,
            "volume": lots,
            "type": self.mt5.ORDER_TYPE_BUY if is_buy else self.mt5.ORDER_TYPE_SELL,
            "price": entry,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": f"MM:{strategy}"[:31],
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(s),
        }
        if stop_loss:
            req["sl"] = round(float(stop_loss), s.digits)
        if take_profit:
            req["tp"] = round(float(take_profit), s.digits)

        result = self._send(req)
        if result is None:
            return None
        pos = Position(symbol=symbol, side=side, units=lots * s.trade_contract_size,
                       entry_price=float(result.price or entry), opened_at=ts or utcnow(),
                       stop_loss=stop_loss, take_profit=take_profit, strategy=strategy)
        pos.id = str(result.order)
        log.info("OPEN %s %s %.2f lots @ %.5f (ticket %s)", broker_sym, side.value,
                 lots, pos.entry_price, result.order)
        return pos

    def _filling_mode(self, spec):
        """Pick a fill policy the symbol actually supports (brokers differ)."""
        mode = getattr(spec, "filling_mode", 0)
        if mode & 1:
            return self.mt5.ORDER_FILLING_FOK
        if mode & 2:
            return self.mt5.ORDER_FILLING_IOC
        return self.mt5.ORDER_FILLING_RETURN

    def close(self, position: Position, price=None, ts=None, reason=""):
        broker_sym = self.resolve_symbol(position.symbol)
        live = self._find_mt5_position(position)
        if live is None:
            raise MT5Error(f"Position {position.id} not found on the terminal")
        s = self.spec(position.symbol)
        tick = self.mt5.symbol_info_tick(broker_sym)
        is_buy = live.type == self.mt5.POSITION_TYPE_BUY
        req = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": broker_sym,
            "volume": live.volume,
            "type": self.mt5.ORDER_TYPE_SELL if is_buy else self.mt5.ORDER_TYPE_BUY,
            "position": live.ticket,
            "price": tick.bid if is_buy else tick.ask,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": f"MM close:{reason}"[:31],
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(s),
        }
        result = self._send(req)
        exit_px = float(result.price) if result else float(tick.bid if is_buy else tick.ask)
        # realised profit as the broker computed it (includes swap + commission)
        pnl = float(live.profit + live.swap)
        trade = Trade(position.symbol, position.side, position.units, position.entry_price,
                      exit_px, position.opened_at, ts or utcnow(), pnl, reason, position.strategy)
        self.trades.append(trade)
        log.info("CLOSE %s @ %.5f  P&L %.2f %s (%s)", broker_sym, exit_px, pnl,
                 self.currency, reason)
        return trade

    def _find_mt5_position(self, position: Position):
        for p in (self.mt5.positions_get(symbol=self.resolve_symbol(position.symbol)) or []):
            if str(p.ticket) == str(position.id) or str(p.identifier) == str(position.id):
                return p
        return None

    # ------------------------------------------------------------ state
    @property
    def positions(self) -> Dict[str, Position]:
        """Live positions opened by this robot, keyed by ticket."""
        out: Dict[str, Position] = {}
        for p in (self.mt5.positions_get() or []):
            if p.magic != self.magic:
                continue  # never touch trades we did not open
            s = self.mt5.symbol_info(p.symbol)
            pos = Position(
                symbol=p.symbol, side=Side.BUY if p.type == self.mt5.POSITION_TYPE_BUY else Side.SELL,
                units=p.volume * (s.trade_contract_size if s else 100_000),
                entry_price=p.price_open,
                opened_at=datetime.fromtimestamp(p.time, tz=timezone.utc),
                stop_loss=p.sl or None, take_profit=p.tp or None,
                strategy=(p.comment or "").replace("MM:", ""))
            pos.id = str(p.ticket)
            out[pos.id] = pos
        return out

    def equity(self, prices=None) -> float:
        info = self.mt5.account_info()
        return float(info.equity) if info else 0.0

    @property
    def balance(self) -> float:
        info = self.mt5.account_info()
        return float(info.balance) if info else 0.0

    def position_for(self, symbol: str, strategy: str | None = None) -> Optional[Position]:
        want = self.resolve_symbol(symbol)
        for p in self.positions.values():
            if p.symbol == want:
                return p
        return None

    def mark(self, ts: datetime, prices=None) -> float:
        eq = self.equity()
        self.equity_curve.append((ts, eq))
        return eq

    def account_summary(self) -> dict:
        i = self.mt5.account_info()
        return {"login": i.login, "server": i.server, "name": i.name,
                "currency": i.currency, "balance": i.balance, "equity": i.equity,
                "margin": i.margin, "free_margin": i.margin_free,
                "leverage": i.leverage, "demo": self.is_demo}

    def shutdown(self) -> None:
        try:
            self.mt5.shutdown()
        except Exception:  # noqa: BLE001
            pass
