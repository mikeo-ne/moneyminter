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
    """Realistic offline FX simulator.

    Uses geometric Brownian motion with mean reversion, intraday session
    volatility (Tokyo/London/NY), occasional news shocks and trending regimes
    so strategies see behaviour similar to a real pair.
    """

    def __init__(self, symbol: str, timeframe: str = "H1", bars: int = 3000,
                 seed: Optional[int] = 7, end: Optional[datetime] = None):
        inst = get_instrument(symbol)
        step = timeframe_seconds(timeframe)
        rng = np.random.default_rng(seed if seed is not None else random.randrange(1 << 30))
        end = end or datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        idx = pd.date_range(end=end, periods=bars, freq=pd.Timedelta(seconds=step), tz="UTC")

        bars_per_day = 86400 / step
        sigma = inst.pip * inst.daily_vol_pips / math.sqrt(bars_per_day)

        # session volatility multiplier
        hours = idx.hour.to_numpy()
        session = np.where((hours >= 7) & (hours < 16), 1.35,
                  np.where((hours >= 12) & (hours < 21), 1.25,
                  np.where((hours >= 0) & (hours < 7), 0.75, 0.6)))

        # Slowly varying trend regime (Ornstein-Uhlenbeck): phi < 1 keeps the
        # drift bounded so the series trends but never runs away.
        phi, drift_sd = 0.98, sigma * 0.10
        drift = np.zeros(bars)
        d = 0.0
        for i in range(bars):
            d = phi * d + rng.normal(0, drift_sd)
            drift[i] = d

        shocks = (rng.random(bars) < 0.004) * rng.normal(0, 1, bars) * sigma * 5
        noise = rng.normal(0, 1, bars) * sigma * session + shocks

        # Random walk with weak mean reversion toward the anchor price, so the
        # pair stays in a realistic band instead of collapsing to zero.
        anchor, kappa = inst.base_price, 0.01
        close = np.empty(bars)
        px = anchor
        for i in range(bars):
            px = px + kappa * (anchor - px) + noise[i] + drift[i]
            close[i] = px

        open_ = np.concatenate([[anchor], close[:-1]])
        wick = np.abs(rng.normal(0, 1, bars)) * sigma * 0.8 * session
        high = np.maximum(open_, close) + wick
        low = np.minimum(open_, close) - np.abs(rng.normal(0, 1, bars)) * sigma * 0.8 * session
        volume = (np.abs(noise) / (sigma + 1e-12) * 1000 * session).round()

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
