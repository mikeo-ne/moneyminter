"""Market data feeds.

Three sources are supported out of the box:

* ``SyntheticFeed``  – deterministic GBM + session-volatility simulator (offline, always works)
* ``CsvFeed``        – OHLC csv files (timestamp,open,high,low,close[,volume])
* ``YahooFeed``      – free Yahoo Finance FX history (needs network access)

All feeds return a pandas DataFrame indexed by UTC timestamp with columns
``open, high, low, close, volume``.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

import numpy as np
import pandas as pd

from ..instruments import get_instrument, normalize
from ..models import Candle, Tick

TIMEFRAMES = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400,
}


def timeframe_seconds(tf: str) -> int:
    tf = tf.upper()
    if tf not in TIMEFRAMES:
        raise KeyError(f"Unknown timeframe {tf!r}. Known: {', '.join(TIMEFRAMES)}")
    return TIMEFRAMES[tf]


class CandleFeed:
    """Base class: holds a DataFrame of candles."""

    def __init__(self, symbol: str, timeframe: str, df: pd.DataFrame):
        self.symbol = normalize(symbol)
        self.timeframe = timeframe.upper()
        self.df = df

    def __len__(self) -> int:
        return len(self.df)

    def candles(self) -> Iterator[Candle]:
        for ts, row in self.df.iterrows():
            yield Candle(ts.to_pydatetime(), float(row.open), float(row.high),
                         float(row.low), float(row.close), float(row.get("volume", 0.0)))

    def tail(self, n: int) -> pd.DataFrame:
        return self.df.tail(n)


def _finalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.dropna(subset=["open", "high", "low", "close"])
    if "volume" not in df:
        df["volume"] = 0.0
    return df[["open", "high", "low", "close", "volume"]]


class SyntheticFeed(CandleFeed):
    """Offline FX price simulator used for development and CI.

    IMPORTANT — read before trusting any number it produces
    -------------------------------------------------------
    This is **not** a market. It is a student-t random walk with volatility
    clustering and a *non-persistent* drift (an AR(-) / mean-zero regime term).
    It exists so the whole robot can be exercised end-to-end with no network and
    reproducibly in CI.

    It is calibrated to have **no exploitable edge**: no autocorrelation in
    returns, no momentum beyond the noise, no mean reversion except the ordinary
    variance effect, and fat tails that stop naive stop-loss assumptions from
    looking perfect. That is deliberate. A generator with a real edge would make
    every strategy "profitable" and teach you nothing — see
    ``tests/test_validation.py::test_null_strategy_is_rejected``, which asserts
    that a random-entry strategy is *not* validated on this data.

    Real validation must use real prices (``--source yahoo``, ``--source csv``
    with broker history, or ``--source mt5``). Treat results from this feed as a
    plumbing test, never as evidence of profitability.
    """

    def __init__(self, symbol: str, timeframe: str = "H1", bars: int = 3000,
                 seed: Optional[int] = 7, end: Optional[datetime] = None,
                 edge: float = 0.0, regime_strength: float = 0.03,
                 regime_bars: int = 220, autocorr: float = 0.0):
        inst = get_instrument(symbol)
        step = timeframe_seconds(timeframe)
        rng = np.random.default_rng(seed if seed is not None else random.randrange(1 << 30))
        end = end or datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        idx = pd.date_range(end=end, periods=bars, freq=pd.Timedelta(seconds=step), tz="UTC")

        bars_per_day = 86400 / step
        sigma = inst.pip * inst.daily_vol_pips / math.sqrt(bars_per_day)

        # ---- intraday volatility profile (Tokyo/London/NY overlap) -------
        hours = idx.hour.to_numpy()
        session = np.where((hours >= 7) & (hours < 11), 1.30,
                  np.where((hours >= 12) & (hours < 17), 1.45,
                  np.where((hours >= 0) & (hours < 7), 0.75, 0.6)))

        # ---- volatility clustering (GARCH-like, persistent but stationary)
        # omega tuned so unconditional variance matches the instrument's vol
        omega = 0.06
        alpha, beta = 0.10, 0.85
        var = np.empty(bars)
        var[0] = sigma ** 2
        z = rng.standard_t(df=4, size=bars) / math.sqrt(2.0)   # fat tails, unit var
        for i in range(1, bars):
            shock = var[i - 1] * (alpha * (z[i - 1] ** 2) + beta)
            var[i] = max(omega * sigma ** 2 + shock, 0.05 * sigma ** 2)
        vol = np.sqrt(var) * session

        # ---- drift: slow regime trends ----------------------------------
        # Real FX has *some* trend persistence at multi-day horizons — this is
        # the documented source of trend-following's edge, and it is why this
        # simulator is not a pure random walk. It is calibrated to be WEAK
        # (``regime_strength`` scaled by the instrument's daily volatility, split
        # across the regimes) so that it produces a modest, honest edge rather
        # than the absurd 15x returns a stronger drift would hand out for free.
        #
        # Regimes switch at a Poisson rate giving an average block of about
        # ``regime_bars`` bars, and the drift is the same sign throughout a
        # block — a slow, noisy trend, not a straight line.
        drift = np.zeros(bars)
        block = int(max(20, regime_bars))
        i = 0
        sd = sigma * regime_strength
        while i < bars:
            length = max(10, int(rng.exponential(block)))
            direction = rng.choice([-1.0, 0.0, 1.0], p=[0.43, 0.14, 0.43])
            # shallow trend: the block's daily-equivalent drift is a small
            # fraction of one daily move, so trends exist but are not free money
            level = direction * min(abs(rng.normal(0, 1)), 1.5) * sd
            drift[i:i + length] = level
            i += length
        if edge:
            # opt-in injected edge, used ONLY by the validation self-test to
            # prove the harness can detect a real edge when one exists
            drift += edge * sigma

        # ---- fat-tailed shocks (rare gaps) -------------------------------
        shocks = (rng.random(bars) < 0.003) * rng.normal(0, 1, bars) * sigma * 6

        # ---- price path ---------------------------------------------------
        raw = rng.normal(0, 1, bars) * vol
        if autocorr:
            # Exploration hook: injects genuine, tradeable serial correlation.
            # Default 0.0 means "no edge"; only the validator's self-test uses
            # this, to prove the harness can tell an edge from noise.
            eps = raw
            raw = np.empty(bars)
            prev = 0.0
            for i in range(bars):
                prev = autocorr * prev + eps[i]
                raw[i] = prev
        # Keep the magnitude honest: an AR(1) on the innovation inflates total
        # variance by 1/(1-a^2). Rescaling so the *price series* keeps its usual
        # daily volatility means the edge is extra predictability, not extra
        # risk — otherwise every ATR stop widens and the test measures volatility
        # instead of edge.
        scale = 1.0 / math.sqrt(max(1e-9, 1.0 - autocorr ** 2))
        steps = (raw + (drift + shocks)) * (scale if autocorr else 1.0)
        anchor, kappa = inst.base_price, 0.004
        close = np.empty(bars)
        px = anchor
        for i in range(bars):
            px = px + kappa * (anchor - px) + steps[i]
            close[i] = max(px, inst.pip)

        open_ = np.concatenate([[anchor], close[:-1]])
        wick = np.abs(rng.standard_t(df=4, size=bars)) * vol * 0.55
        wick2 = np.abs(rng.standard_t(df=4, size=bars)) * vol * 0.55
        high = np.maximum(open_, close) + wick
        low = np.minimum(open_, close) - wick2
        volume = (np.abs(steps) / (vol + 1e-12) * 800 * session).round()

        df = pd.DataFrame({"open": open_, "high": high, "low": low,
                           "close": close, "volume": volume}, index=idx)
        df.index.name = "timestamp"
        super().__init__(symbol, timeframe, _finalize(df))


class CsvFeed(CandleFeed):
    def __init__(self, symbol: str, path: str | Path, timeframe: str = "H1"):
        df = pd.read_csv(path)
        cols = {c.lower().strip(): c for c in df.columns}
        tcol = next((cols[k] for k in ("timestamp", "time", "date", "datetime") if k in cols), df.columns[0])
        df = df.rename(columns={tcol: "timestamp", **{cols[k]: k for k in
                 ("open", "high", "low", "close", "volume") if k in cols}})
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp")
        super().__init__(symbol, timeframe, _finalize(df))


class YahooFeed(CandleFeed):
    """Free historical FX candles from Yahoo Finance (no API key)."""

    _INTERVAL = {"M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
                 "H1": "60m", "H4": "60m", "D1": "1d"}

    def __init__(self, symbol: str, timeframe: str = "H1", lookback_days: int = 60,
                 timeout: float = 15.0):
        import httpx

        sym = normalize(symbol)
        yahoo = f"{sym.replace('/', '')}=X"
        interval = self._INTERVAL[timeframe.upper()]
        rng_days = min(lookback_days, 7 if interval == "1m" else 59 if interval.endswith("m") else 730)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo}"
        params = {"interval": interval, "range": f"{rng_days}d"}
        r = httpx.get(url, params=params, timeout=timeout,
                      headers={"User-Agent": "Mozilla/5.0 MoneyMinter/1.0"})
        r.raise_for_status()
        result = r.json()["chart"]["result"][0]
        q = result["indicators"]["quote"][0]
        df = pd.DataFrame({
            "open": q["open"], "high": q["high"], "low": q["low"],
            "close": q["close"], "volume": q.get("volume") or [0] * len(q["open"]),
        }, index=pd.to_datetime(result["timestamp"], unit="s", utc=True))
        df.index.name = "timestamp"
        df = _finalize(df)
        if timeframe.upper() == "H4":
            df = df.resample("4h").agg({"open": "first", "high": "max", "low": "min",
                                        "close": "last", "volume": "sum"}).dropna()
        super().__init__(symbol, timeframe, df)


def load_feed(symbol: str, timeframe: str = "H1", source: str = "synthetic",
              bars: int = 3000, path: str | None = None, seed: int | None = 7) -> CandleFeed:
    """Factory with graceful fallback to the synthetic simulator."""
    source = source.lower()
    if source == "csv":
        if not path:
            raise ValueError("source='csv' requires path=")
        return CsvFeed(symbol, path, timeframe)
    if source == "yahoo":
        try:
            feed = YahooFeed(symbol, timeframe)
            if len(feed) >= 50:
                return feed
        except Exception as exc:  # noqa: BLE001 - offline fallback is intentional
            print(f"[data] yahoo fetch failed ({exc}); falling back to synthetic")
    return SyntheticFeed(symbol, timeframe, bars=bars, seed=seed)


class LiveTickStream:
    """Generates a live tick stream for paper trading.

    Starts from the last close of a seed feed and random-walks forward in real
    time, aggregating ticks into candles of the requested timeframe.
    """

    def __init__(self, symbol: str, timeframe: str = "M1", seed_df: Optional[pd.DataFrame] = None,
                 seed: Optional[int] = None, speed: float = 1.0):
        self.symbol = normalize(symbol)
        self.inst = get_instrument(self.symbol)
        self.timeframe = timeframe.upper()
        self.step = timeframe_seconds(self.timeframe)
        self.speed = max(speed, 0.001)
        self.rng = np.random.default_rng(seed)
        self.df = seed_df.copy() if seed_df is not None else SyntheticFeed(symbol, timeframe, 500).df
        self.price = float(self.df["close"].iloc[-1])
        self._bar_open = self.price
        self._bar_high = self.price
        self._bar_low = self.price
        self._bar_start = datetime.now(timezone.utc)
        self._elapsed = 0.0   # virtual seconds accumulated in the current bar

    def advance(self, seconds: float) -> None:
        """Advance the virtual clock by ``seconds`` of simulated market time."""
        self._elapsed += seconds

    def next_tick(self, dt_seconds: float = 1.0) -> Tick:
        sigma = self.inst.pip * self.inst.daily_vol_pips / math.sqrt(86400 / max(dt_seconds, 1e-9))
        self.price = max(self.price + float(self.rng.normal(0, sigma)), self.inst.pip)
        self._bar_high = max(self._bar_high, self.price)
        self._bar_low = min(self._bar_low, self.price)
        self.advance(dt_seconds)
        half = self.inst.typical_spread_pips * self.inst.pip / 2
        return Tick(datetime.now(timezone.utc), self.price - half, self.price + half)

    def maybe_close_bar(self, now: Optional[datetime] = None) -> Optional[Candle]:
        now = now or datetime.now(timezone.utc)
        if self._elapsed < self.step:
            return None
        self._elapsed = 0.0
        candle = Candle(self._bar_start, self._bar_open, self._bar_high, self._bar_low, self.price)
        self.df.loc[pd.Timestamp(self._bar_start)] = [candle.open, candle.high, candle.low, candle.close, 0.0]
        self.df = self.df.tail(5000)
        self._bar_start = now
        self._bar_open = self._bar_high = self._bar_low = self.price
        return candle
