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

import glob
import logging
import os
import platform
import sys
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


class StaleSignal(MT5Error):
    """The setup is no longer valid at the current price — skip this one."""


class OrderRejected(MT5Error):
    """The broker refused one order. The account and connection are fine.

    Kept distinct from :class:`MT5Error` because a rejected order must never
    escalate to a reconnect, and must never abort the poll for every symbol.
    """


# ---------------------------------------------------------------- macOS app
#: where MT5 for macOS usually ends up, across installer versions
_MAC_APP_CANDIDATES = (
    "/Applications/MetaTrader 5.app",
    "/Applications/MetaTrader 5 (Exness).app",
    "/Applications/MetaTrader 5 (Exness Technologies Ltd).app",
    "/Applications/Exness MetaTrader 5.app",
    "/Applications/MetaTrader5.app",
)
_MAC_APP_SEARCH_DIRS = ("/Applications", os.path.expanduser("~/Applications"))


def find_mac_mt5_app() -> Optional[str]:
    """Return the path of MetaTrader 5.app on macOS, or None.

    ``mt5_mac`` hard-codes ``/Applications/MetaTrader 5.app``. Broker-branded
    installers (Exness, XM, IC Markets ...) write a differently-named bundle,
    and the app also lands in ``~/Applications`` when dropped there instead of
    dragged onto /Applications. Both cases fail with a misleading
    "MetaTrader 5.app not found" even though it is plainly open on screen.
    """
    env = os.getenv("MT5_APP") or os.getenv("MT5_MAC_APP")
    if env:
        return env if os.path.isdir(env) else None
    for cand in _MAC_APP_CANDIDATES:
        if os.path.isdir(cand) and _is_mt5_bundle(cand):
            return cand
    found = []
    for root in _MAC_APP_SEARCH_DIRS:
        try:
            for name in os.listdir(root):
                if not name.endswith(".app"):
                    continue
                low = name.lower()
                if "metatrader" in low or low.startswith("mt5"):
                    full = os.path.join(root, name)
                    if _is_mt5_bundle(full):
                        found.append(full)
        except OSError:
            continue
    if not found:
        return None
    # prefer the plain, unbranded bundle, then the shortest name
    found.sort(key=lambda p: (os.path.basename(p) != "MetaTrader 5.app",
                              len(os.path.basename(p))))
    return found[0]


#: the Wine binary has moved between MT5 builds; try all of them
_WINE_BIN_RELATIVE = (
    "Contents/SharedSupport/wine/bin/wine64",        # MT5 build 4000+
    "Contents/SharedSupport/wine/bin/wine",
    "Contents/SharedSupport/metatrader5/MetaTrader 5/wine64",   # older bundles
    "Contents/Frameworks/wine/bin/wine64",
    "Contents/Resources/wine/bin/wine64",
)


def find_wine_binary(app_path: str) -> Optional[str]:
    """Return the Wine executable inside an MT5 bundle, or None.

    This is the *real* check. ``mt5_mac`` tests only for
    ``Contents/SharedSupport/wine/bin/wine64``, so on a bundle whose Wine
    runtime lives elsewhere it raises "MetaTrader 5.app not found" while the
    app sits in /Applications exactly where the installer put it.
    """
    for rel in _WINE_BIN_RELATIVE:
        path = os.path.join(app_path, rel)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    # last resort: a bounded search inside the bundle
    for root, dirs, files in os.walk(app_path):
        depth = root[len(app_path):].count(os.sep)
        if depth > 5:
            dirs[:] = []
            continue
        for name in ("wine64", "wine"):
            if name in files:
                full = os.path.join(root, name)
                if os.access(full, os.X_OK):
                    return full
    return None


def _is_mt5_bundle(path: str) -> bool:
    """True when this .app is an MT5 install with a usable Wine runtime."""
    return find_wine_binary(path) is not None


def diagnose_mac_app(app_path: str) -> str:
    """Explain what is actually inside an .app when no Wine binary is found.

    Recursive, because the interesting answer is always *somewhere* deeper than
    the two directories a first version checked.
    """
    lines = [f"Inspecting {app_path}"]
    if not os.path.isdir(app_path):
        return f"{app_path} is not a directory"
    for rel in ("Contents", "Contents/SharedSupport", "Contents/MacOS",
                "Contents/SharedSupport/wine"):
        lines.append(f"  {'ok     ' if os.path.exists(os.path.join(app_path, rel)) else 'MISSING'} "
                     f"{rel}")
    found = []
    for proj in ("lib/wine/*/wine64", "lib/wine/*/wine",
                 "**/wine64", "**/wine", "**/wineserver"):
        pattern = os.path.join(app_path, "Contents", proj)
        found.extend(f for f in glob.glob(pattern, recursive=True)
                     if os.path.isfile(f))
        if found:
            break
    if found:
        lines.append("  executables found:")
        for f in sorted(set(found))[:6]:
            lines.append(f"    {'+' if os.access(f, os.X_OK) else '-'} "
                         f"{os.path.relpath(f, app_path)}")
        winners = [f for f in found if os.path.basename(f) == "wine64"
                   and os.access(f, os.X_OK)]
        if not winners:
            lines.append("    (none named 'wine64' and executable — the bridge")
            lines.append("     cannot use these; a launcher script is not enough)")
    else:
        lines.append("  no wine executable anywhere inside the bundle")
    try:
        tree = os.path.join(app_path, "Contents", "SharedSupport")
        if os.path.isdir(tree):
            lines.append("  SharedSupport/: " + ", ".join(sorted(os.listdir(tree))[:12]))
    except OSError:
        pass
    return "\n".join(lines)

def diagnose_mac_bundle(app_path: str) -> tuple[bool, str]:
    """Is this bundle usable by a Python driver? Returns (ok, explanation).

    Matters because "MT5.app exists" and "MT5 can be driven from Python" are
    different questions, and ``doctor`` used to report the first as if it were
    the second.
    """
    wine = find_wine_binary(app_path)
    if wine is not None:
        return True, f"wine: {os.path.relpath(wine, app_path)}"
    report = diagnose_mac_app(app_path)
    return False, (
        "The bundle is installed but contains no Wine runtime, so no Python "
        "driver (mt5_mac) can attach to it.\n"
        "For a Windows VM this is not needed — MT5 there is native. On the Mac\n"
        "itself the options are a Windows VPS, or reinstall MT5.\n\n"
        + report
    )


def _patch_mt5_mac_app(app_path: str) -> None:
    """Point the ``mt5_mac`` module at ``app_path``.

    Rewrites the module-level constants it reads at call time, in ``mt5_mac``,
    ``mt5_mac.backend`` and ``mt5_mac.bootstrap`` (``ensure_environment`` takes
    ``wine_bin`` as a default argument, so the bootstrap copy matters too).
    Silent if a future version stops exposing these names — its own error is
    better than a crash from here.
    """
    try:
        import importlib
        pkg = importlib.import_module("mt5_mac")
    except Exception:  # noqa: BLE001 - not installed
        return
    wine = find_wine_binary(app_path)
    if wine is None:
        return
    mods = [pkg]
    for sub in ("backend", "bootstrap"):
        try:
            mods.append(importlib.import_module(f"mt5_mac.{sub}"))
        except Exception:  # noqa: BLE001
            continue
    for mod in mods:
        if hasattr(mod, "MT5_APP"):
            setattr(mod, "MT5_APP", app_path)
        if hasattr(mod, "WINE_BIN"):
            setattr(mod, "WINE_BIN", wine)
    log.info("Using MetaTrader 5 bundle at %s (wine: %s)", app_path, wine)


def _preflight_mac_backend(backend: str) -> None:
    """Fail early, with an actionable message, when macOS MT5 is not usable."""
    if sys.platform != "darwin":
        return
    app = find_mac_mt5_app()
    if app is None:
        raise MT5Error(
            "MetaTrader 5.app was not found, or has no usable Wine runtime.\n"
            "Looked for: /Applications/*MetaTrader*.app and "
            "~/Applications/*MetaTrader*.app\n"
            "  • Installed somewhere else? Point at it:\n"
            "      export MT5_APP=\"/Applications/MetaTrader 5.app\"\n"
            "  • Not installed? Get it from https://www.metatrader5.com/en/download\n"
            "    and drag it into /Applications."
        )
    if find_wine_binary(app) is None:
        raise MT5Error(
            "MetaTrader 5.app is installed but its Wine runtime is missing, so\n"
            "Python cannot drive it.\n\n"
            + diagnose_mac_app(app) + "\n\n"
            "This usually means the bundle is damaged or was moved after install.\n"
            "Either:\n"
            "  • Reinstall MT5 from https://www.metatrader5.com/en/download\n"
            "    (it overwrites the bundle and restores Wine), or\n"
            "  • Use a remote bridge instead — run MT5 on a Windows VPS and\n"
            "      python -m moneyminter connect --mt5-host <vps-ip> --mt5-port 18812\n"
            "    which avoids the macOS Wine layer entirely. See LIVE_TRADING.md §0c."
        )
    _patch_mt5_mac_app(app)


def linux_mt5_advice() -> tuple[bool, str]:
    """Can this Linux machine drive MetaTrader 5 at all? Returns (ok, why).

    Linux has no native MT5: the terminal is a Windows binary, so the only route
    is Wine plus the Windows build of CPython plus the MetaTrader5 wheel, then
    ``mt5linux`` to expose it over RPyC (``--mt5-host``).

    That stack is x86-64. On ARM it needs full instruction emulation
    (box64/qemu), which is roughly an order of magnitude slower and far too
    fragile to leave trading unattended — so we say so instead of letting
    someone discover it three layers down.
    """
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return False, (
            f"{machine} — MT5 under Wine would need x86 emulation, which is too "
            "slow/fragile to trade with. Use a Windows VPS, or an x86-64 host.\n"
            "      Offline work (backtest / validate / demo / dashboard) runs fine here.")
    return True, (
        f"{machine} — MT5 has no Linux build, so Wine + Windows Python + "
        "mt5linux is the only route.\n"
        "      Simpler: point at a Windows machine instead —\n"
        "        python -m moneyminter connect --mt5-host <windows-ip> --mt5-port 18812")


def _explain_init_failure(exc: Exception) -> str:
    text = str(exc)
    low = text.lower()
    if "metatrader 5.app not found" in low or "bootstrap" in low:
        return (
            "MetaTrader 5.app was not found where the macOS bridge expects it.\n"
            "It insists on /Applications/MetaTrader 5.app, and broker-branded\n"
            "installs are named differently.\n\n"
            "Fix it one of two ways:\n"
            "  1. Tell Money Minter where yours is (recommended):\n"
            "       ls /Applications | grep -i metatrader\n"
            "       export MT5_APP=\"/Applications/<exact name>.app\"\n"
            "  2. Or make a link with the name it wants:\n"
            "       ln -s \"/Applications/<exact name>.app\" \"/Applications/MetaTrader 5.app\"\n\n"
            f"Original error: {text}"
        )
    if "wine" in low:
        return ("The Wine runtime inside MetaTrader 5.app could not start.\n"
                "Launch MetaTrader 5 manually once, confirm it opens, then retry.\n"
                f"Original error: {text}")
    return f"Could not start the MT5 bridge: {text}"


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
            _preflight_mac_backend(backend)
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
        self._seen_tickets: set[str] = set()
        self._vanished: set[str] = set()
        self.rejected: List[dict] = []
        self._clock_offset_s: int = 0
        self.symbol_cache_probe: str = "EUR/USD"
        self.trades: List[Trade] = []
        #: broker symbol name -> requested "EUR/USD", so records are consistent
        self._display_symbols: Dict[str, str] = {}
        self.equity_curve: List[tuple] = []

        kwargs = {}
        if path:
            kwargs["path"] = path
        if login:
            kwargs.update(login=int(login), password=password, server=server)
        self._init_kwargs = dict(kwargs)
        try:
            ok = self.mt5.initialize(**kwargs)
        except Exception as exc:  # noqa: BLE001 - bridge errors vary by backend
            raise MT5Error(_explain_init_failure(exc)) from exc
        if not ok:
            raise MT5Error(
                f"MT5 initialize() failed: {self.mt5.last_error()}.\n"
                "The terminal must be OPEN and LOGGED IN before this runs.\n"
                "Check, in order:\n"
                "  1. MetaTrader 5 is running and shows your account in the title bar\n"
                "  2. Tools > Options > Experts: 'Allow algorithmic trading' ticked\n"
                "     and 'Disable algorithmic trading via external Python API' NOT ticked\n"
                "  3. The Algo Trading toolbar button is green\n"
                "  4. On macOS: export MT5_APP=<path to your MetaTrader 5.app>")

        info = self.mt5.account_info()
        if info is None:
            raise MT5Error(f"Could not read account info: {self.mt5.last_error()}")
        self.account = info
        # trade_mode 0 = DEMO, 1 = CONTEST, 2 = REAL. Anything we cannot read
        # is treated as LIVE, which fails safe.
        mode = getattr(info, "trade_mode", None)
        self.is_demo = mode in self._demo_modes() if mode is not None else False
        self.initial_balance = float(info.balance)
        self.currency = info.currency

        self._guard_account()
        self._detect_clock_offset()
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
        self._display_symbols[symbol.replace("/", "").upper() + (self._suffix or "")] = symbol
        base = symbol.replace("/", "").upper()
        candidates = [base + self._suffix] if self._suffix is not None else \
            [base + s for s in _SUFFIXES]
        for cand in candidates:
            info = self.mt5.symbol_info(cand)
            if info is not None:
                if not info.visible and not self.mt5.symbol_select(cand, True):
                    continue
                self._symbol_cache[symbol] = cand
                self._display_symbols[cand.upper()] = symbol
                if cand != base:
                    log.info("Resolved %s -> %s", symbol, cand)
                return cand
        # Last resort: scan everything the broker offers. Ordering matters —
        # a plain ``startswith`` would happily return ``EURUSDT`` (a crypto
        # pair) when the caller asked for ``EUR/USD``, and the robot would then
        # "trade EURUSD" on the wrong instrument. So rank the matches: an exact
        # name wins, then a known suffix, then the shortest remainder.
        if self._suffix is None:
            ranked = []
            for info in (self.mt5.symbols_get() or []):
                # Symbol names are case-sensitive in MT5 ('EURUSDm'), so keep
                # the broker's original spelling and only upper-case for
                # comparison.
                name = info.name or ""
                key = name.upper()
                if not key.startswith(base):
                    continue
                rest = key[len(base):]
                if rest == "":
                    rank = 0
                elif rest.lower() in _SUFFIXES:
                    rank = 1
                elif len(rest) <= 3:
                    rank = 2
                else:
                    # e.g. 'USDT' off a 'EURUSD' base: a different instrument
                    continue
                ranked.append((rank, len(name), name))
            if ranked:
                ranked.sort()
                best = ranked[0][2]
                self.mt5.symbol_select(best, True)
                self._symbol_cache[symbol] = best
                self._display_symbols[best.upper()] = symbol
                if best != base:
                    log.info("Resolved %s -> %s (by scan)", symbol, best)
                return best
        raise MT5Error(
            f"Symbol {symbol} not found on this account (tried {candidates}). "
            "Open Market Watch, right-click and choose 'Show All', then retry. "
            "If your broker suffixes symbols differently, pass --symbol-suffix.")

    def display_symbol(self, broker_symbol: str) -> str:
        """Map a broker name ('EURUSDm') back to the user-facing 'EUR/USD'."""
        return self._display_symbols.get(broker_symbol.upper(), broker_symbol)

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
        stamps = pd.to_datetime(df["time"], unit="s", utc=True)
        df["timestamp"] = self._align_bar_clock(stamps)
        df = df.set_index("timestamp").rename(columns={"tick_volume": "volume"})
        return df[["open", "high", "low", "close", "volume"]].astype(float)

    #: how far a broker clock may sit from real UTC before we correct for it
    _MAX_CLOCK_SKEW_S = 3600

    def _align_bar_clock(self, stamps: pd.DatetimeIndex) -> pd.DatetimeIndex:
        """Shift bars onto the strategy's UTC clock when the broker disagrees.

        MT5 timestamps bars with the *broker's* server clock. Some bridges
        (notably the macOS/Wine ones) hand that back labelled as UTC, so a
        UTC+2 broker produces bars two hours in the future. Strategies only
        look at relative bar order so measurement survives, but a clock-skewed
        feed breaks session logic (``session_breakout``) and makes the journal
        timestamps lie.

        We correct only when the skew is large enough to be a real offset rather
        than network latency, and we log it once so it is visible rather than
        silent.
        """
        if len(stamps) == 0 or self._clock_offset_s == 0:
            return stamps
        return stamps + pd.Timedelta(seconds=self._clock_offset_s)

    def _detect_clock_offset(self) -> None:
        """Compare the newest bar's timestamp with real time and cache the skew.

        Only *current* bars are trusted. A feed that replays history from a
        fixed start point (the simulator does this) produces bars that are days
        or weeks old, and treating that as a clock offset would corrupt a
        perfectly good timestamp sequence. So we require the newest bar to be
        within a plausible live window of now, and otherwise leave the clock
        alone.
        """
        self._clock_offset_s = 0
        try:
            import numpy as _np
            tf = getattr(self.mt5, _TF.get("M5"), None)
            sym = self.resolve_symbol(self.symbol_cache_probe)
            if tf is None or sym is None:
                return
            rates = self.mt5.copy_rates_from_pos(sym, tf, 0, 1)
            if rates is None or len(rates) == 0:
                return
            newest = float(_np.asarray(rates)[0]["time"])
            skew = newest - time.time()
            # a live bar is at most one bar-interval old; anything else means
            # this feed is not tracking wall-clock time and must not be shifted
            live_window = 3 * 3600
            if skew > live_window or skew < -live_window:
                log.debug("bar clock not live (skew %.0fs) — leaving timestamps as-is",
                          skew)
                return
            if abs(skew) > self._MAX_CLOCK_SKEW_S:
                # round to the nearest half hour: FX server offsets are UTC+2,
                # UTC+3, UTC-4 etc, not arbitrary seconds
                self._clock_offset_s = int(round(-skew / 1800.0) * 1800)
                log.warning("MT5 bars are on the broker clock (%+.1f h from UTC); "
                            "shifting them onto UTC for strategy and journal use",
                            -self._clock_offset_s / 3600.0)
        except Exception as exc:  # noqa: BLE001 - never fail on a nicety
            log.debug("clock offset detection skipped: %s", exc)

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
            self.rejected.append({"reason": "below broker minimum lot", "symbol": symbol,
                                  "wanted_lots": round(lots, 4),
                                  "min_lot": s.volume_min})
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
            if result.retcode == self._retcode_done():
                return result
            # requote / price-changed are worth retrying with a fresh price
            requote_codes = {10004, 10020}   # REQUOTE, PRICE_CHANGED
            for attr in ("TRADE_RETCODE_REQUOTE", "TRADE_RETCODE_PRICE_CHANGED"):
                val = getattr(self.mt5, attr, None)
                if isinstance(val, int):
                    requote_codes.add(val)
            if result.retcode in requote_codes and attempt < 2:
                time.sleep(0.4)
                tick = self.mt5.symbol_info_tick(request["symbol"])
                if request["type"] == self.mt5.ORDER_TYPE_BUY:
                    request["price"] = tick.ask
                else:
                    request["price"] = tick.bid
                continue
            self.rejected.append({"retcode": result.retcode, "comment": result.comment,
                                  "symbol": request.get("symbol"),
                                  "volume": request.get("volume"),
                                  "type": request.get("type")})
            # A rejection is a normal market event (price moved, no margin,
            # invalid stops), not a connection failure. Surface it on a
            # dedicated exception so the caller can skip this one idea and
            # carry on with the other symbols.
            raise OrderRejected(
                f"Order rejected: retcode={result.retcode} {result.comment}")
        raise MT5Error("Order failed after retries")

    def open(self, symbol, side: Side, units, price=None, ts=None,
             stop_loss=None, take_profit=None, strategy="", **mgmt):
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
            # Refuse a stop that is already inside the spread: the broker
            # answers 10016 'invalid stop loss' and, worse, the position would
            # have no protection at all. This happens when the market gaps
            # between the signal bar closing and the order being sent.
            if (is_buy and req["sl"] >= entry) or (not is_buy and req["sl"] <= entry):
                raise StaleSignal(
                    f"{symbol}: price moved past the stop before the order was sent "
                    f"(entry {entry:.5f}, stop {req['sl']:.5f})")
        if take_profit:
            req["tp"] = round(float(take_profit), s.digits)

        result = self._send(req)
        if result is None:
            return None
        # store exactly what the broker was told, so trailing maths and the UI
        # never drift from the live stop by sub-pip float dust
        stop_loss = round(float(stop_loss), s.digits) if stop_loss else None
        take_profit = round(float(take_profit), s.digits) if take_profit else None
        pos = Position(symbol=symbol, side=side, units=lots * s.trade_contract_size,
                       entry_price=float(result.price or entry), opened_at=ts or utcnow(),
                       stop_loss=stop_loss, take_profit=take_profit, strategy=strategy,
                       initial_stop=stop_loss,
                       trail_atr=mgmt.get("trail_atr"),
                       breakeven_at_r=mgmt.get("breakeven_at_r"),
                       max_bars=mgmt.get("max_bars"))
        pos.id = str(result.order)
        log.info("OPEN %s %s %.2f lots @ %.5f (ticket %s)", broker_sym, side.value,
                 lots, pos.entry_price, result.order)
        return pos

    def _retcode_done(self) -> int:
        """The retcode that means "filled", per backend.

        The official ``MetaTrader5`` package exposes ``TRADE_RETCODE_DONE``
        (10009). ``mt5_mac`` — the macOS wrapper — exposes ``RES_E_SUCCESS``
        instead, and some other bridges name it differently again. Without this
        fallback a *successful* fill looks like a rejection on macOS, which
        would silently stop the robot from ever trading.
        """
        for attr in ("TRADE_RETCODE_DONE", "RES_E_SUCCESS", "TRADE_RETCODE_PLACED"):
            val = getattr(self.mt5, attr, None)
            if isinstance(val, int):
                return val
        log.warning("No success retcode found on the MT5 backend; assuming 10009")
        return 10009

    def _demo_modes(self) -> tuple:
        """Account trade_mode values that mean demo/contest, per backend."""
        modes = []
        for attr in ("ACCOUNT_TRADE_MODE_DEMO", "ACCOUNT_TRADE_MODE_CONTEST"):
            val = getattr(self.mt5, attr, None)
            if isinstance(val, int):
                modes.append(val)
        return tuple(modes) if modes else (0, 1)

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
                      exit_px, position.opened_at, ts or utcnow(), pnl, reason,
                      position.strategy, initial_stop=position.initial_stop)
        self.trades.append(trade)
        log.info("CLOSE %s @ %.5f  P&L %.2f %s (%s)", broker_sym, exit_px, pnl,
                 self.currency, reason)
        return trade

    def modify_stop(self, position: Position, new_stop: float,
                    take_profit: Optional[float] = None) -> bool:
        """Re-send the broker-side stop-loss (used for trailing stops).

        A stop that only exists in Python is not a stop — if the machine dies
        the position is naked. So every trail step is pushed to the broker.
        """
        broker_sym = self.resolve_symbol(position.symbol)
        live = self._find_mt5_position(position)
        if live is None:
            raise MT5Error(f"Position {position.id} not found on the terminal")
        s = self.spec(position.symbol)
        tp = take_profit if take_profit is not None else (live.tp or 0.0)
        req = {
            "action": self.mt5.TRADE_ACTION_SLTP,
            "symbol": broker_sym,
            "position": live.ticket,
            "sl": round(float(new_stop), s.digits),
            "tp": round(float(tp), s.digits) if tp else 0.0,
        }
        result = self._send(req)
        if result is None:
            return False
        log.info("STOP %s -> %.5f (ticket %s)", broker_sym, new_stop, live.ticket)
        return True

    def close_all(self, reason: str = "flatten") -> list:
        """Close every position this robot owns."""
        out = []
        for p in list(self.positions.values()):
            try:
                out.append(self.close(p, reason=reason))
            except MT5Error as exc:
                log.error("close_all failed for %s: %s", p.symbol, exc)
        return out

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
        current = set()
        for p in (self.mt5.positions_get() or []):
            if p.magic != self.magic:
                continue  # never touch trades we did not open
            s = self.mt5.symbol_info(p.symbol)
            pos = Position(
                symbol=self.display_symbol(p.symbol),
                side=Side.BUY if p.type == self.mt5.POSITION_TYPE_BUY else Side.SELL,
                units=p.volume * (s.trade_contract_size if s else 100_000),
                entry_price=p.price_open,
                opened_at=datetime.fromtimestamp(p.time, tz=timezone.utc),
                stop_loss=p.sl or None, take_profit=p.tp or None,
                strategy=(p.comment or "").replace("MM:", ""),
                initial_stop=p.sl or None)
            pos.id = str(p.ticket)
            current.add(pos.id)
            out[pos.id] = pos
        # remember tickets that were open last time we looked and are now gone:
        # the broker closed them (SL/TP) or a human did, and reconciliation
        # needs to report that rather than silently losing the trade
        if getattr(self, "_seen_tickets", None) is not None:
            self._vanished = getattr(self, "_seen_tickets", set()) - current
        self._seen_tickets = current
        return out

    def drain_vanished(self) -> Dict[str, dict]:
        """Tickets that disappeared since the previous ``positions`` read."""
        gone = getattr(self, "_vanished", set())
        self._vanished = set()
        return {t: {"ticket": t} for t in gone}

    def equity(self, prices=None) -> float:
        """Current account equity.

        Raises rather than returning 0.0 on failure: a 0 equity would look like
        a 100% drawdown to the risk manager and latch the kill switch for good
        on what may be a momentary disconnect.
        """
        info = self.mt5.account_info()
        if info is None:
            raise MT5Error(f"Lost connection to the MT5 terminal: {self.mt5.last_error()}")
        return float(info.equity)

    @property
    def balance(self) -> float:
        info = self.mt5.account_info()
        if info is None:
            raise MT5Error(f"Lost connection to the MT5 terminal: {self.mt5.last_error()}")
        return float(info.balance)

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
        if i is None:
            raise MT5Error(f"Lost connection to the MT5 terminal: {self.mt5.last_error()}")
        return {"login": i.login, "server": i.server, "name": i.name,
                "currency": i.currency, "balance": i.balance, "equity": i.equity,
                "margin": i.margin, "free_margin": i.margin_free,
                "leverage": i.leverage, "demo": self.is_demo}

    def is_connected(self) -> bool:
        try:
            return self.mt5.account_info() is not None
        except Exception:  # noqa: BLE001
            return False

    def reconnect(self) -> bool:
        """Try to re-establish the terminal link after a dropout."""
        try:
            self.mt5.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            ok = self.mt5.initialize(**self._init_kwargs)
            if ok and self.mt5.account_info() is not None:
                log.info("Reconnected to the MT5 terminal")
                return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Reconnect failed: %s", exc)
        return False

    def shutdown(self) -> None:
        try:
            self.mt5.shutdown()
        except Exception:  # noqa: BLE001
            pass
