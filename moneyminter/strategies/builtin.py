"""Built-in trading strategies.

Design notes
------------
The strategies here are built around three documented, persistent features of
the FX market rather than around a single indicator crossing another:

1. **Trend persistence at daily/weekly horizons.** Currency trends are driven by
   slow-moving forces (policy divergence, trade flows, hedging demand). The
   classic failure mode of a naive moving-average cross is that it enters late
   and mid-range. So the trend strategies here (a) require an *aligned* EMA
   structure, (b) demand corroboration from ADX/DI, (c) buy *pullbacks* into
   value instead of chasing the breakout bar, and (d) let a trailing stop carry
   the trade, which is where trend-following's positive skew actually comes
   from.

2. **Volatility is mean-reverting even when price is not.** Range compression
   (Bollinger inside Keltner) precedes expansion far more often than chance.
   Entry is taken only on the expansion, with the compression as a precondition.

3. **Session structure.** Intraday FX liquidity and volatility are not uniform:
   the Asian range often acts as the day's reference and London/NY expansion
   breaks it. This is why the breakout strategy gates entries to the London and
   New York windows.

Every strategy declines to trade when its edge is not present (wrong regime,
dead volatility, wrong session) and returns ``FLAT``. A strategy that is always
in the market is a strategy with no filters.

None of this is a guarantee of profit. See ``moneyminter/validation.py`` and the
robustness harness that ships with the project: a strategy earns "proven" status
only by holding up across many independent market samples and out-of-sample
folds, and even then the edge can decay.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from .. import indicators as ta
from ..models import Position, Signal, SignalType
from .base import Strategy, register


# --------------------------------------------------------------------- helpers
def _symbol(df: pd.DataFrame) -> str:
    return getattr(df, "symbol_name", None) or df.attrs.get("symbol", "?")


def _ok(*values) -> bool:
    """True when every value is present and finite."""
    for v in values:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return False
    return True


def _session_mask(index: pd.DatetimeIndex, sessions) -> pd.Series:
    return ta.in_session(index, sessions)


# ------------------------------------------------------------------- trend 1
@register("trend_pullback")
class TrendPullback(Strategy):
    """Trend following: aligned EMA structure, enter on pullbacks, trail out.

    Long setup (mirrored for shorts):

    * EMA fast > EMA slow > EMA trend (all rising) and price above the trend EMA
    * +DI > -DI and ADX above ``adx_min`` (a real directional market)
    * price dips into the fast EMA and closes back above it with an up close
    * ATR in a sane band — dead markets clip the stop, blow-off markets gap it

    Exit is a trailing stop plus break-even at 1R, so winners are free to run
    while losers stay bounded.
    """

    style = "trend (pullback)"

    def __init__(self, fast: int = 20, slow: int = 50, trend: int = 200,
                 atr_period: int = 14, atr_stop: float = 2.0, trail_atr: float = 3.0,
                 adx_min: float = 20.0, vol_lo: float = 0.15, vol_hi: float = 0.97,
                 rr: float = 5.0, max_bars: int = 200, **kw):
        super().__init__(fast=fast, slow=slow, trend=trend, atr_period=atr_period,
                         atr_stop=atr_stop, trail_atr=trail_atr, adx_min=adx_min,
                         vol_lo=vol_lo, vol_hi=vol_hi, rr=rr, max_bars=max_bars, **kw)
        self.warmup = max(trend, slow, atr_period) + 60

    def prepare(self, df):
        df = df.copy()
        df["ema_f"] = ta.ema(df.close, self.fast)
        df["ema_s"] = ta.ema(df.close, self.slow)
        df["ema_t"] = ta.ema(df.close, self.trend)
        df["atr"] = ta.atr(df.high, df.low, df.close, self.atr_period)
        df["atrp"] = ta.atr_percentile(df["atr"])
        pdi, mdi, adx_ = ta.directional(df.high, df.low, df.close, 14)
        df["pdi"], df["mdi"], df["adx"] = pdi, mdi, adx_
        df["er"] = ta.efficiency_ratio(df.close, 20)
        return df

    def on_bar(self, df, position):
        sym = _symbol(df)
        r, p = df.iloc[-1], df.iloc[-2]
        if not _ok(r.ema_t, r.atr, r.adx) or r.atr <= 0:
            return self.flat(sym, "warmup")

        # ---- manage an open trade ---------------------------------------
        if position:
            # structural exit: the trend that justified the trade is gone
            if position.side.value == "BUY" and r.close < r.ema_s:
                return self.close(sym, "lost EMA structure")
            if position.side.value == "SELL" and r.close > r.ema_s:
                return self.close(sym, "lost EMA structure")
            return self.flat(sym)

        if not np.isfinite(r.atrp) or not (self.vol_lo <= r.atrp <= self.vol_hi):
            return self.flat(sym, "volatility filter")
        if r.adx < self.adx_min or r.er < 0.20:
            return self.flat(sym, "no trend")

        up = r.ema_f > r.ema_s > r.ema_t and r.close > r.ema_t and r.pdi > r.mdi
        dn = r.ema_f < r.ema_s < r.ema_t and r.close < r.ema_t and r.mdi > r.pdi
        # pullback: the previous bar traded below/above the fast EMA, this one
        # closed back on the trend side with a follow-through candle
        pull_up = up and p.low <= p.ema_f and r.close > r.ema_f and r.close > r.open
        pull_dn = dn and p.high >= p.ema_f and r.close < r.ema_f and r.close < r.open

        if pull_up:
            stop = r.close - self.atr_stop * r.atr
            return Signal(SignalType.LONG, sym, min(1.0, 0.5 + r.adx / 80),
                          f"uptrend pullback (ADX {r.adx:.0f}, ER {r.er:.2f})",
                          stop, r.close + self.atr_stop * r.atr * self.rr,
                          trail_atr=self.trail_atr, breakeven_at_r=1.0,
                          max_bars=self.max_bars)
        if pull_dn:
            stop = r.close + self.atr_stop * r.atr
            return Signal(SignalType.SHORT, sym, min(1.0, 0.5 + r.adx / 80),
                          f"downtrend pullback (ADX {r.adx:.0f}, ER {r.er:.2f})",
                          stop, r.close - self.atr_stop * r.atr * self.rr,
                          trail_atr=self.trail_atr, breakeven_at_r=1.0,
                          max_bars=self.max_bars)
        return self.flat(sym)


# ------------------------------------------------------------------- trend 2
@register("donchian_trend")
class DonchianTrend(Strategy):
    """Donchian breakout that only trades when a trend and volatility exist.

    The naive Donchian breakout buys every N-bar high, including the ones at the
    top of a range, which is where breakouts fail. Two filters fix most of it:
    a channel-width expansion filter (the range must be widening, not
    contracting) and an efficiency-ratio floor (the market must actually be
    moving). Exits run on an ATR trail rather than a fixed target, because the
    distribution of trend-following P&L is a few large winners.
    """

    style = "trend (breakout)"

    def __init__(self, entry: int = 55, exit: int = 20, atr_period: int = 14,
                 atr_stop: float = 2.5, trail_atr: float = 3.5, adx_min: float = 18.0,
                 er_min: float = 0.22, rr: float = 8.0, max_bars: int = 240, **kw):
        super().__init__(entry=entry, exit=exit, atr_period=atr_period,
                         atr_stop=atr_stop, trail_atr=trail_atr, adx_min=adx_min,
                         er_min=er_min, rr=rr, max_bars=max_bars, **kw)
        self.warmup = max(entry, exit, atr_period) + 80

    def prepare(self, df):
        df = df.copy()
        lo, hi = ta.donchian(df.high, df.low, self.entry)
        # shift(1) so the level the bar is tested against excludes that bar
        df["dc_low"], df["dc_high"] = lo.shift(1), hi.shift(1)
        xlo, xhi = ta.donchian(df.high, df.low, self.exit)
        df["x_low"], df["x_high"] = xlo.shift(1), xhi.shift(1)
        df["atr"] = ta.atr(df.high, df.low, df.close, self.atr_period)
        df["atrp"] = ta.atr_percentile(df["atr"])
        _, _, adx_ = ta.directional(df.high, df.low, df.close, 14)
        df["adx"] = adx_
        df["er"] = ta.efficiency_ratio(df.close, 40)
        # is the channel widening? compare current width with its own average
        width = (hi - lo)
        df["widening"] = width > width.rolling(self.entry * 2, min_periods=self.entry).mean()
        return df

    def on_bar(self, df, position):
        sym = _symbol(df)
        r = df.iloc[-1]
        if not _ok(r.dc_high, r.atr, r.adx) or r.atr <= 0:
            return self.flat(sym, "warmup")

        if position:
            if position.side.value == "BUY" and r.close < r.x_low:
                return self.close(sym, "channel exit")
            if position.side.value == "SELL" and r.close > r.x_high:
                return self.close(sym, "channel exit")
            return self.flat(sym)

        if r.adx < self.adx_min or r.er < self.er_min:
            return self.flat(sym, "no trend")
        if not bool(r.widening):
            return self.flat(sym, "channel contracting")
        if not np.isfinite(r.atrp) or r.atrp < 0.15:
            return self.flat(sym, "volatility filter")

        if r.close > r.dc_high:
            stop = r.close - self.atr_stop * r.atr
            return Signal(SignalType.LONG, sym, min(1.0, 0.55 + r.adx / 100),
                          f"{self.entry}-bar breakout up (ADX {r.adx:.0f}, ER {r.er:.2f})",
                          stop, r.close + self.atr_stop * r.atr * self.rr,
                          trail_atr=self.trail_atr, breakeven_at_r=1.0,
                          max_bars=self.max_bars)
        if r.close < r.dc_low:
            stop = r.close + self.atr_stop * r.atr
            return Signal(SignalType.SHORT, sym, min(1.0, 0.55 + r.adx / 100),
                          f"{self.entry}-bar breakout down (ADX {r.adx:.0f}, ER {r.er:.2f})",
                          stop, r.close - self.atr_stop * r.atr * self.rr,
                          trail_atr=self.trail_atr, breakeven_at_r=1.0,
                          max_bars=self.max_bars)
        return self.flat(sym)


# ------------------------------------------------------------------ session
@register("session_breakout")
class SessionBreakout(Strategy):
    """Asian-range breakout into the London / New York session.

    Overnight FX liquidity is thin and the range built between the Tokyo open
    and the London open encodes where the market has accepted price. The first
    decisive break of that range once London liquidity arrives is one of the
    more persistent intraday patterns in FX. Trades are closed at the end of the
    session rather than carried overnight, which keeps swap out of the P&L.
    """

    style = "session breakout"

    def __init__(self, atr_period: int = 14, atr_stop: float = 1.5,
                 trail_atr: float = 2.0, rr: float = 3.0, buffer_atr: float = 0.15,
                 min_range_atr: float = 0.6, max_range_atr: float = 3.0,
                 entry_hours=(7, 12), flat_hour: int = 20, max_bars: int = 16, **kw):
        super().__init__(atr_period=atr_period, atr_stop=atr_stop, trail_atr=trail_atr,
                         rr=rr, buffer_atr=buffer_atr, min_range_atr=min_range_atr,
                         max_range_atr=max_range_atr, entry_hours=entry_hours,
                         flat_hour=flat_hour, max_bars=max_bars, **kw)
        self.warmup = atr_period * 5

    def prepare(self, df):
        df = df.copy()
        df["atr"] = ta.atr(df.high, df.low, df.close, self.atr_period)
        idx = df.index
        day = pd.Series(idx.date, index=idx)
        hour = pd.Series(idx.hour, index=idx)
        # Asian range = 00:00-06:59 UTC, computed per day and shifted so the
        # value available on a bar only uses *completed* Asian hours
        asian = (hour >= 0) & (hour < 7)
        hi = df.high.where(asian)
        lo = df.low.where(asian)
        df["asia_high"] = hi.groupby(day).cummax().groupby(day).shift(7)
        df["asia_low"] = lo.groupby(day).cummin().groupby(day).shift(7)
        df["hour"] = hour
        df["dow"] = pd.Series(idx.day_of_week, index=idx)
        df["range_atr"] = (df["asia_high"] - df["asia_low"]) / df["atr"]
        return df

    def on_bar(self, df, position):
        sym = _symbol(df)
        r = df.iloc[-1]
        if not _ok(r.asia_high, r.asia_low, r.atr, r.range_atr) or r.atr <= 0:
            return self.flat(sym, "warmup")

        if position:
            # time-based exit: do not hold through the thin late session
            if r.hour >= self.flat_hour or r.hour < 1:
                return self.close(sym, "session end")
            return self.flat(sym)

        if r.dow >= 5:                       # skip the weekend rollover
            return self.flat(sym, "weekend")
        lo_h, hi_h = self.entry_hours
        if not (lo_h <= r.hour < hi_h):
            return self.flat(sym, "outside session")
        if not (self.min_range_atr <= r.range_atr <= self.max_range_atr):
            return self.flat(sym, "range size filter")

        buf = self.buffer_atr * r.atr
        if r.close > r.asia_high + buf:
            stop = max(r.asia_low, r.close - self.atr_stop * r.atr)
            return Signal(SignalType.LONG, sym, 0.8,
                          f"London break of Asian high ({r.range_atr:.1f} ATR range)",
                          stop, r.close + self.atr_stop * r.atr * self.rr,
                          trail_atr=self.trail_atr, breakeven_at_r=1.0,
                          max_bars=self.max_bars)
        if r.close < r.asia_low - buf:
            stop = min(r.asia_high, r.close + self.atr_stop * r.atr)
            return Signal(SignalType.SHORT, sym, 0.8,
                          f"London break of Asian low ({r.range_atr:.1f} ATR range)",
                          stop, r.close - self.atr_stop * r.atr * self.rr,
                          trail_atr=self.trail_atr, breakeven_at_r=1.0,
                          max_bars=self.max_bars)
        return self.flat(sym)


# ------------------------------------------------------------------ squeeze
@register("squeeze_momentum")
class SqueezeMomentum(Strategy):
    """Volatility-squeeze expansion with MACD momentum confirmation.

    Bollinger bands inside Keltner channels mean realised volatility has
    compressed relative to the average true range — the market is coiled. The
    trade is taken on the bar that resolves the squeeze *in the direction of the
    MACD histogram*, with the histogram already agreeing (not merely flipping
    this instant, which is the noisy part of a raw MACD signal).
    """

    style = "volatility expansion"

    def __init__(self, period: int = 20, bb_std: float = 2.0, kc_mult: float = 1.5,
                 atr_period: int = 14, atr_stop: float = 1.8, trail_atr: float = 2.5,
                 rr: float = 4.0, lookback: int = 6, max_bars: int = 96, **kw):
        super().__init__(period=period, bb_std=bb_std, kc_mult=kc_mult,
                         atr_period=atr_period, atr_stop=atr_stop, trail_atr=trail_atr,
                         rr=rr, lookback=lookback, max_bars=max_bars, **kw)
        self.warmup = period * 4 + 30

    def prepare(self, df):
        df = df.copy()
        bb_lo, bb_mid, bb_hi = ta.bollinger(df.close, self.period, self.bb_std)
        df["bb_lo"], df["bb_mid"], df["bb_hi"] = bb_lo, bb_mid, bb_hi
        df["sq_on"] = ta.squeeze(df.high, df.low, df.close, self.period,
                                   self.bb_std, self.kc_mult)
        df["sq_recent"] = df["sq_on"].rolling(self.lookback, min_periods=1).max().astype(bool)
        _, _, hist = ta.macd(df.close, 12, 26, 9)
        df["macd_hist"] = hist
        df["atr"] = ta.atr(df.high, df.low, df.close, self.atr_period)
        df["er"] = ta.efficiency_ratio(df.close, 20)
        return df

    def on_bar(self, df, position):
        sym = _symbol(df)
        r, p = df.iloc[-1], df.iloc[-2]
        if not _ok(r.bb_hi, r.atr, r.macd_hist) or r.atr <= 0:
            return self.flat(sym, "warmup")

        if position:
            mid = r.bb_mid
            if position.side.value == "BUY" and r.close < mid and p.close < mid:
                return self.close(sym, "momentum lost mid band")
            if position.side.value == "SELL" and r.close > mid and p.close > mid:
                return self.close(sym, "momentum lost mid band")
            return self.flat(sym)

        if not bool(r.sq_recent) or bool(r.sq_on):
            return self.flat(sym, "no squeeze expansion")
        if r.er < 0.18:
            return self.flat(sym, "choppy")

        expanding = r.atr > p.atr
        if r.close > r.bb_hi and r.macd_hist > 0 and p.macd_hist > 0 and expanding:
            stop = r.close - self.atr_stop * r.atr
            return Signal(SignalType.LONG, sym, 0.85, "squeeze expansion up",
                          stop, r.close + self.atr_stop * r.atr * self.rr,
                          trail_atr=self.trail_atr, breakeven_at_r=1.0,
                          max_bars=self.max_bars)
        if r.close < r.bb_lo and r.macd_hist < 0 and p.macd_hist < 0 and expanding:
            stop = r.close + self.atr_stop * r.atr
            return Signal(SignalType.SHORT, sym, 0.85, "squeeze expansion down",
                          stop, r.close - self.atr_stop * r.atr * self.rr,
                          trail_atr=self.trail_atr, breakeven_at_r=1.0,
                          max_bars=self.max_bars)
        return self.flat(sym)


# ------------------------------------------------------------- mean reversion
@register("range_reversion")
class RangeReversion(Strategy):
    """Fade failed breakouts back into the mean — only in ranging markets.

    Mean reversion is the strategy that gets killed by every trend, so the
    regime gate comes first and is deliberately blunt: no ADX expansion and no
    directional efficiency. Inside that gate the entry is a **failed stretch**
    rather than a raw band touch:

    * price closes outside the Bollinger band (the market is stretched), then
    * the next bar closes back inside the band (the stretch failed), and
    * RSI has turned back from its extreme.

    That "poke out, snap back" pattern is a far better reversion trigger than a
    band touch, because a band touch in a trending market is just the trend
    continuing. Stops sit beyond the excursion high/low plus an ATR buffer, and
    the target is the mid band, where reversion actually terminates.
    """

    style = "mean reversion"

    def __init__(self, period: int = 20, std: float = 2.0, atr_period: int = 14,
                 atr_stop: float = 1.0, adx_max: float = 22.0, er_max: float = 0.34,
                 rsi_period: int = 14, rsi_long: float = 45.0, rsi_short: float = 55.0,
                 max_bars: int = 20, **kw):
        super().__init__(period=period, std=std, atr_period=atr_period,
                         atr_stop=atr_stop, adx_max=adx_max, er_max=er_max,
                         rsi_period=rsi_period, rsi_long=rsi_long,
                         rsi_short=rsi_short, max_bars=max_bars, **kw)
        self.warmup = period * 4 + 40

    def prepare(self, df):
        df = df.copy()
        lo, mid, hi = ta.bollinger(df.close, self.period, self.std)
        df["bb_lo"], df["bb_mid"], df["bb_hi"] = lo, mid, hi
        df["atr"] = ta.atr(df.high, df.low, df.close, self.atr_period)
        _, _, adx_ = ta.directional(df.high, df.low, df.close, 14)
        df["adx"] = adx_
        df["er"] = ta.efficiency_ratio(df.close, 40)
        df["rsi"] = ta.rsi(df.close, self.rsi_period)
        df["z"] = ta.zscore(df.close, self.period)
        # short-term extreme for a structural stop (not the stale wide window)
        df["lo5"] = df.low.rolling(5, min_periods=5).min()
        df["hi5"] = df.high.rolling(5, min_periods=5).max()
        return df

    def on_bar(self, df, position):
        sym = _symbol(df)
        r, p = df.iloc[-1], df.iloc[-2]
        if not _ok(r.bb_lo, r.atr, r.adx, r.rsi) or r.atr <= 0:
            return self.flat(sym, "warmup")

        if position:
            if position.side.value == "BUY" and r.close >= r.bb_mid:
                return self.close(sym, "reverted to mean")
            if position.side.value == "SELL" and r.close <= r.bb_mid:
                return self.close(sym, "reverted to mean")
            return self.flat(sym)

        # ---- regime gate: the whole edge lives here ----------------------
        if r.adx > self.adx_max or r.er > self.er_max:
            return self.flat(sym, "trending — no reversion")

        # ---- failed stretch down -> long ---------------------------------
        poke_down = p.close <= p.bb_lo
        back_up = r.close > r.bb_lo and r.close > r.open
        if poke_down and back_up and r.rsi < self.rsi_long:
            stop = min(r.lo5, r.close - self.atr_stop * r.atr) - 0.1 * r.atr
            return Signal(SignalType.LONG, sym, 0.7,
                          f"failed break lower (ADX {r.adx:.0f}, RSI {r.rsi:.0f})",
                          stop, r.bb_mid, max_bars=self.max_bars)

        # ---- failed stretch up -> short ----------------------------------
        poke_up = p.close >= p.bb_hi
        back_dn = r.close < r.bb_hi and r.close < r.open
        if poke_up and back_dn and r.rsi > self.rsi_short:
            stop = max(r.hi5, r.close + self.atr_stop * r.atr) + 0.1 * r.atr
            return Signal(SignalType.SHORT, sym, 0.7,
                          f"failed break higher (ADX {r.adx:.0f}, RSI {r.rsi:.0f})",
                          stop, r.bb_mid, max_bars=self.max_bars)
        return self.flat(sym)


# ---------------------------------------------------------------- supertrend
@register("supertrend_adx")
class SuperTrendADX(Strategy):
    """Supertrend flip in the direction of dominant directional movement.

    Supertrend gives a clean, objective trend line with an ATR buffer, but it
    whipsaws when DI is balanced. Requiring +DI/-DI dominance and a minimum ADX
    at the flip removes most of those. The trail then rides the Supertrend line
    indirectly via an ATR trail.
    """

    style = "trend (supertrend)"

    def __init__(self, period: int = 10, mult: float = 3.0, atr_period: int = 14,
                 atr_stop: float = 2.0, trail_atr: float = 3.0, adx_min: float = 22.0,
                 rr: float = 6.0, max_bars: int = 200, **kw):
        super().__init__(period=period, mult=mult, atr_period=atr_period,
                         atr_stop=atr_stop, trail_atr=trail_atr, adx_min=adx_min,
                         rr=rr, max_bars=max_bars, **kw)
        self.warmup = max(period * 6, atr_period * 6) + 40

    def prepare(self, df):
        df = df.copy()
        line, direction = ta.supertrend(df.high, df.low, df.close, self.period, self.mult)
        df["st_line"], df["st_dir"] = line, direction
        df["atr"] = ta.atr(df.high, df.low, df.close, self.atr_period)
        pdi, mdi, adx_ = ta.directional(df.high, df.low, df.close, 14)
        df["pdi"], df["mdi"], df["adx"] = pdi, mdi, adx_
        return df

    def on_bar(self, df, position):
        sym = _symbol(df)
        r, p = df.iloc[-1], df.iloc[-2]
        if not _ok(r.st_line, r.atr, r.adx) or r.atr <= 0:
            return self.flat(sym, "warmup")

        if position:
            if position.side.value == "BUY" and r.st_dir < 0:
                return self.close(sym, "supertrend flipped")
            if position.side.value == "SELL" and r.st_dir > 0:
                return self.close(sym, "supertrend flipped")
            return self.flat(sym)

        if r.adx < self.adx_min:
            return self.flat(sym, "ADX too low")
        flip_up = p.st_dir <= 0 < r.st_dir
        flip_dn = p.st_dir >= 0 > r.st_dir
        if flip_up and r.pdi > r.mdi:
            stop = r.close - self.atr_stop * r.atr
            return Signal(SignalType.LONG, sym, min(1.0, r.adx / 50),
                          f"supertrend up, ADX {r.adx:.0f}", stop,
                          r.close + self.atr_stop * r.atr * self.rr,
                          trail_atr=self.trail_atr, breakeven_at_r=1.0,
                          max_bars=self.max_bars)
        if flip_dn and r.mdi > r.pdi:
            stop = r.close + self.atr_stop * r.atr
            return Signal(SignalType.SHORT, sym, min(1.0, r.adx / 50),
                          f"supertrend down, ADX {r.adx:.0f}", stop,
                          r.close - self.atr_stop * r.atr * self.rr,
                          trail_atr=self.trail_atr, breakeven_at_r=1.0,
                          max_bars=self.max_bars)
        return self.flat(sym)


# --------------------------------------------------------------------- legacy
@register("ma_crossover")
class MACrossover(Strategy):
    """Baseline trend: fast/slow EMA cross with an ADX filter and ATR trail."""

    style = "trend (baseline)"

    def __init__(self, fast: int = 12, slow: int = 34, atr_period: int = 14,
                 atr_stop: float = 2.0, rr: float = 4.0, adx_min: float = 20.0,
                 trail_atr: float = 3.0, max_bars: int = 160, **kw):
        super().__init__(fast=fast, slow=slow, atr_period=atr_period, atr_stop=atr_stop,
                         rr=rr, adx_min=adx_min, trail_atr=trail_atr,
                         max_bars=max_bars, **kw)
        self.warmup = max(slow, atr_period, 30) + 30

    def prepare(self, df):
        df = df.copy()
        df["fast"] = ta.ema(df.close, self.fast)
        df["slow"] = ta.ema(df.close, self.slow)
        df["atr"] = ta.atr(df.high, df.low, df.close, self.atr_period)
        df["adx"] = ta.adx(df.high, df.low, df.close, 14)
        return df

    def on_bar(self, df, position):
        sym = _symbol(df)
        r, p = df.iloc[-1], df.iloc[-2]
        if not _ok(r.slow, r.atr, r.adx) or r.atr <= 0:
            return self.flat(sym, "warmup")
        up = p.fast <= p.slow and r.fast > r.slow
        dn = p.fast >= p.slow and r.fast < r.slow
        trend_ok = r.adx >= self.adx_min
        if position:
            if (position.side.value == "BUY" and dn) or (position.side.value == "SELL" and up):
                return self.close(sym, "opposite cross")
            return self.flat(sym)
        if up and trend_ok:
            stop = r.close - self.atr_stop * r.atr
            return Signal(SignalType.LONG, sym, min(1.0, r.adx / 40),
                          f"EMA{self.fast}>EMA{self.slow}, ADX {r.adx:.0f}", stop,
                          r.close + self.atr_stop * r.atr * self.rr,
                          trail_atr=self.trail_atr, breakeven_at_r=1.0,
                          max_bars=self.max_bars)
        if dn and trend_ok:
            stop = r.close + self.atr_stop * r.atr
            return Signal(SignalType.SHORT, sym, min(1.0, r.adx / 40),
                          f"EMA{self.fast}<EMA{self.slow}, ADX {r.adx:.0f}", stop,
                          r.close - self.atr_stop * r.atr * self.rr,
                          trail_atr=self.trail_atr, breakeven_at_r=1.0,
                          max_bars=self.max_bars)
        return self.flat(sym)


@register("momentum_macd")
class MomentumMACD(Strategy):
    """MACD histogram flip confirmed by a long EMA regime filter."""

    style = "momentum"

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9, trend: int = 200,
                 atr_period: int = 14, atr_stop: float = 2.0, rr: float = 4.0,
                 trail_atr: float = 3.0, max_bars: int = 160, **kw):
        super().__init__(fast=fast, slow=slow, signal=signal, trend=trend,
                         atr_period=atr_period, atr_stop=atr_stop, rr=rr,
                         trail_atr=trail_atr, max_bars=max_bars, **kw)
        self.warmup = trend + 40

    def prepare(self, df):
        df = df.copy()
        line, sig, hist = ta.macd(df.close, self.fast, self.slow, self.signal)
        df["macd"], df["macd_sig"], df["macd_hist"] = line, sig, hist
        df["trend_ema"] = ta.ema(df.close, self.trend)
        df["atr"] = ta.atr(df.high, df.low, df.close, self.atr_period)
        return df

    def on_bar(self, df, position):
        sym = _symbol(df)
        r, p = df.iloc[-1], df.iloc[-2]
        if not _ok(r.trend_ema, r.atr, r.macd_hist) or r.atr <= 0:
            return self.flat(sym, "warmup")
        flip_up = p.macd_hist <= 0 < r.macd_hist
        flip_dn = p.macd_hist >= 0 > r.macd_hist
        if position:
            if (position.side.value == "BUY" and flip_dn) or (position.side.value == "SELL" and flip_up):
                return self.close(sym, "momentum flip")
            return self.flat(sym)
        if flip_up and r.close > r.trend_ema:
            stop = r.close - self.atr_stop * r.atr
            return Signal(SignalType.LONG, sym, 0.85, "MACD flip up in uptrend", stop,
                          r.close + self.atr_stop * r.atr * self.rr,
                          trail_atr=self.trail_atr, breakeven_at_r=1.0,
                          max_bars=self.max_bars)
        if flip_dn and r.close < r.trend_ema:
            stop = r.close + self.atr_stop * r.atr
            return Signal(SignalType.SHORT, sym, 0.85, "MACD flip down in downtrend", stop,
                          r.close - self.atr_stop * r.atr * self.rr,
                          trail_atr=self.trail_atr, breakeven_at_r=1.0,
                          max_bars=self.max_bars)
        return self.flat(sym)


@register("breakout")
class Breakout(Strategy):
    """Alias of :class:`DonchianTrend` kept for backwards compatibility."""

    style = "trend (breakout)"

    def __init__(self, entry: int = 20, exit: int = 10, atr_period: int = 14,
                 atr_stop: float = 2.5, rr: float = 6.0, trail_atr: float = 3.0,
                 adx_min: float = 18.0, max_bars: int = 160, **kw):
        super().__init__(entry=entry, exit=exit, atr_period=atr_period,
                         atr_stop=atr_stop, rr=rr, trail_atr=trail_atr,
                         adx_min=adx_min, max_bars=max_bars, **kw)
        self._inner = DonchianTrend(entry=entry, exit=exit, atr_period=atr_period,
                                    atr_stop=atr_stop, trail_atr=trail_atr,
                                    adx_min=adx_min, rr=rr, max_bars=max_bars)
        self.warmup = self._inner.warmup

    def prepare(self, df):
        return self._inner.prepare(df)

    def on_bar(self, df, position):
        return self._inner.on_bar(df, position)


@register("rsi_reversion")
class RSIReversion(Strategy):
    """Alias of :class:`RangeReversion` kept for backwards compatibility."""

    style = "mean reversion"

    def __init__(self, rsi_period: int = 14, low: float = 25.0, high: float = 75.0,
                 exit_level: float = 50.0, atr_period: int = 14, atr_stop: float = 1.2,
                 rr: float = 1.5, **kw):
        super().__init__(rsi_period=rsi_period, low=low, high=high,
                         exit_level=exit_level, atr_period=atr_period,
                         atr_stop=atr_stop, rr=rr, **kw)
        self._inner = RangeReversion(period=20, std=2.2, atr_period=atr_period,
                                     atr_stop=atr_stop, rsi_period=rsi_period)
        self.warmup = self._inner.warmup

    def prepare(self, df):
        return self._inner.prepare(df)

    def on_bar(self, df, position):
        return self._inner.on_bar(df, position)


@register("bollinger_scalp")
class BollingerScalp(Strategy):
    """Alias of :class:`SqueezeMomentum` kept for backwards compatibility."""

    style = "volatility expansion"

    def __init__(self, period: int = 20, std: float = 2.2, atr_period: int = 14,
                 atr_stop: float = 1.5, rr: float = 3.0, **kw):
        super().__init__(period=period, std=std, atr_period=atr_period,
                         atr_stop=atr_stop, rr=rr, **kw)
        self._inner = SqueezeMomentum(period=period, bb_std=std,
                                      atr_period=atr_period, atr_stop=atr_stop, rr=rr)
        self.warmup = self._inner.warmup

    def prepare(self, df):
        return self._inner.prepare(df)

    def on_bar(self, df, position):
        return self._inner.on_bar(df, position)
