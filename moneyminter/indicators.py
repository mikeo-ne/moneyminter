"""Vectorised technical indicators (pandas/numpy, no TA-Lib dependency).

Everything here is intentional about *not* leaking future information:
rolling windows are causal and any level a strategy trades against should be
shifted by the caller (see the strategies for the pattern).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# --------------------------------------------------------------------- basics
def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def vwap_rolling(high: pd.Series, low: pd.Series, close: pd.Series,
                 volume: pd.Series | None = None, period: int = 20) -> pd.Series:
    """Volume-weighted average price over a rolling window."""
    tp = (high + low + close) / 3.0
    vol = volume if volume is not None and volume.notna().any() else pd.Series(1.0, index=close.index)
    num = (tp * vol).rolling(period, min_periods=period).sum()
    den = vol.rolling(period, min_periods=period).sum().replace(0, np.nan)
    return num / den


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(100.0).where(avg_loss.notna(), np.nan)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    return pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return true_range(high, low, close).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def atr_percentile(atr_series: pd.Series, lookback: int = 250) -> pd.Series:
    """Where current volatility sits in its own recent history (0..1).

    Strategies use this to stand down in dead markets (stops get clipped by
    noise) and in blow-off volatility (stops get jumped).
    """
    return atr_series.rolling(lookback, min_periods=max(20, lookback // 5)).rank(pct=True)


def bollinger(series: pd.Series, period: int = 20, std: float = 2.0):
    mid = sma(series, period)
    dev = series.rolling(period, min_periods=period).std(ddof=0)
    return mid - std * dev, mid, mid + std * dev


def keltner(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 20,
            mult: float = 2.0, atr_period: int = 14):
    mid = ema(close, period)
    band = atr(high, low, close, atr_period) * mult
    return mid - band, mid, mid + band


def squeeze(high, low, close, period: int = 20, bb_std: float = 2.0,
            kc_mult: float = 1.5) -> pd.Series:
    """True when Bollinger bands sit inside Keltner channels (coiled volatility).

    A squeeze is the classic precondition for a range expansion, which is what
    makes a breakout strategy worth taking rather than fading.
    """
    bb_lo, _, bb_hi = bollinger(close, period, bb_std)
    kc_lo, _, kc_hi = keltner(high, low, close, period, kc_mult)
    return (bb_lo > kc_lo) & (bb_hi < kc_hi)


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(series, fast) - ema(series, slow)
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return directional(high, low, close, period)[2]


def directional(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14):
    """Return (+DI, -DI, ADX) — Wilder's directional movement system."""
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = true_range(high, low, close)
    alpha = 1 / period
    atr_ = tr.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=high.index).ewm(alpha=alpha, adjust=False).mean() / atr_
    minus_di = 100 * pd.Series(minus_dm, index=high.index).ewm(alpha=alpha, adjust=False).mean() / atr_
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return plus_di, minus_di, dx.ewm(alpha=alpha, adjust=False).mean()


def donchian(high: pd.Series, low: pd.Series, period: int = 20):
    return low.rolling(period, min_periods=period).min(), high.rolling(period, min_periods=period).max()


def stoch_rsi(series: pd.Series, period: int = 14, k: int = 3, d: int = 3):
    r = rsi(series, period)
    lo = r.rolling(period, min_periods=period).min()
    hi = r.rolling(period, min_periods=period).max()
    kline = 100 * (r - lo) / (hi - lo).replace(0, np.nan)
    kline = kline.rolling(k, min_periods=1).mean()
    return kline, kline.rolling(d, min_periods=1).mean()


def supertrend(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 10,
               mult: float = 3.0):
    """Supertrend line and its direction (+1 up-trend, -1 down-trend).

    Implemented iteratively (it is path dependent); O(n) and fine for the bar
    counts used here.
    """
    a = atr(high, low, close, period).to_numpy(dtype=float)
    h, l, c = high.to_numpy(float), low.to_numpy(float), close.to_numpy(float)
    n = len(c)
    line = np.full(n, np.nan)
    direction = np.zeros(n, dtype=int)
    upper = lower = np.nan
    for i in range(1, n):
        if np.isnan(a[i]):
            continue
        mid = (h[i] + l[i]) / 2.0
        bu, bl = mid + mult * a[i], mid - mult * a[i]
        if np.isnan(upper):
            upper, lower = bu, bl
            direction[i] = 1
            line[i] = bl
            continue
        upper = bu if (bu < upper or c[i - 1] > upper) else upper
        lower = bl if (bl > lower or c[i - 1] < lower) else lower
        if direction[i - 1] >= 0:
            direction[i] = -1 if c[i] < lower else 1
            line[i] = lower if direction[i] > 0 else upper
        else:
            direction[i] = 1 if c[i] > upper else -1
            line[i] = lower if direction[i] > 0 else upper
    return (pd.Series(line, index=close.index), pd.Series(direction, index=close.index))


def zscore(series: pd.Series, period: int = 20) -> pd.Series:
    m = series.rolling(period, min_periods=period).mean()
    s = series.rolling(period, min_periods=period).std(ddof=0).replace(0, np.nan)
    return (series - m) / s


def efficiency_ratio(series: pd.Series, period: int = 20) -> pd.Series:
    """Kaufman's efficiency ratio: |net move| / sum(|moves|), 0..1.

    High values mean a clean directional move (good for trend following);
    low values mean chop (good for mean reversion).
    """
    net = (series - series.shift(period)).abs()
    path = series.diff().abs().rolling(period, min_periods=period).sum()
    return (net / path.replace(0, np.nan)).clip(0, 1)


def hurst(series: pd.Series, window: int = 128, max_lag: int = 20) -> pd.Series:
    """Rolling Hurst exponent (0.5 = random walk, >0.5 trending, <0.5 reverting)."""
    def _h(x: np.ndarray) -> float:
        lags = range(2, max_lag)
        try:
            tau = [np.std(x[lag:] - x[:-lag]) for lag in lags]
            if min(tau) <= 0:
                return 0.5
            poly = np.polyfit(np.log(list(lags)), np.log(tau), 1)
            return float(poly[0])
        except Exception:  # noqa: BLE001
            return 0.5
    return series.rolling(window, min_periods=window).apply(_h, raw=True)


def rolling_corr(a: pd.Series, b: pd.Series, period: int = 100) -> pd.Series:
    return a.pct_change().rolling(period, min_periods=period // 2).corr(b.pct_change())


def slope(series: pd.Series, period: int = 20) -> pd.Series:
    """Normalised linear-regression slope (move per bar / price level)."""
    x = np.arange(period, dtype=float)
    x -= x.mean()
    denom = (x ** 2).sum()

    def _s(y: np.ndarray) -> float:
        return float(np.dot(x, y - y.mean()) / denom)

    raw = series.rolling(period, min_periods=period).apply(_s, raw=True)
    return raw / series.replace(0, np.nan)


# ------------------------------------------------------------------- sessions
def session_of(ts) -> str:
    """Forex session label for a UTC timestamp (DST-approximate is fine here)."""
    h = ts.hour
    if 23 <= h or h < 7:
        return "sydney"
    if 0 <= h < 8:
        return "tokyo"
    if 7 <= h < 16:
        return "london"
    if 12 <= h < 21:
        return "newyork"
    return "late"


def in_session(index: pd.DatetimeIndex, sessions=("london", "newyork")) -> pd.Series:
    labels = pd.Series([session_of(t) for t in index], index=index)
    return labels.isin(sessions)


def day_of_week(index: pd.DatetimeIndex) -> pd.Series:
    return pd.Series(index.day_of_week, index=index)
