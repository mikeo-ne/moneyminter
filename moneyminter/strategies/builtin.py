"""Built-in trading strategies."""
from __future__ import annotations

from typing import Optional

import pandas as pd

from .. import indicators as ta
from ..models import Position, Signal, SignalType
from .base import Strategy, register


@register("ma_crossover")
class MACrossover(Strategy):
    """Trend following: fast/slow EMA cross filtered by ADX, ATR-based stops."""

    def __init__(self, fast: int = 12, slow: int = 34, atr_period: int = 14,
                 atr_stop: float = 2.0, rr: float = 2.0, adx_min: float = 18.0, **kw):
        super().__init__(fast=fast, slow=slow, atr_period=atr_period, atr_stop=atr_stop,
                         rr=rr, adx_min=adx_min, **kw)
        self.warmup = max(slow, atr_period, 30) + 5

    def prepare(self, df):
        df = df.copy()
        df["fast"] = ta.ema(df.close, self.fast)
        df["slow"] = ta.ema(df.close, self.slow)
        df["atr"] = ta.atr(df.high, df.low, df.close, self.atr_period)
        df["adx"] = ta.adx(df.high, df.low, df.close, 14)
        return df

    def on_bar(self, df, position):
        r, p = df.iloc[-1], df.iloc[-2]
        if pd.isna(r.slow) or pd.isna(r.atr) or r.atr <= 0:
            return self.flat(df.symbol_name, "warmup")
        up = p.fast <= p.slow and r.fast > r.slow
        dn = p.fast >= p.slow and r.fast < r.slow
        trend_ok = (r.adx or 0) >= self.adx_min
        if position:
            if (position.side.value == "BUY" and dn) or (position.side.value == "SELL" and up):
                return Signal(SignalType.CLOSE, df.symbol_name, reason="opposite cross")
            return self.flat(df.symbol_name)
        if up and trend_ok:
            stop = r.close - self.atr_stop * r.atr
            return Signal(SignalType.LONG, df.symbol_name, min(1.0, r.adx / 40),
                          f"EMA{self.fast}>EMA{self.slow}, ADX {r.adx:.0f}",
                          stop, r.close + self.atr_stop * r.atr * self.rr)
        if dn and trend_ok:
            stop = r.close + self.atr_stop * r.atr
            return Signal(SignalType.SHORT, df.symbol_name, min(1.0, r.adx / 40),
                          f"EMA{self.fast}<EMA{self.slow}, ADX {r.adx:.0f}",
                          stop, r.close - self.atr_stop * r.atr * self.rr)
        return self.flat(df.symbol_name)


@register("rsi_reversion")
class RSIReversion(Strategy):
    """Mean reversion: RSI extremes against a slow-EMA regime filter."""

    def __init__(self, rsi_period: int = 14, low: float = 30.0, high: float = 70.0,
                 exit_level: float = 50.0, atr_period: int = 14, atr_stop: float = 2.5,
                 rr: float = 1.5, **kw):
        super().__init__(rsi_period=rsi_period, low=low, high=high, exit_level=exit_level,
                         atr_period=atr_period, atr_stop=atr_stop, rr=rr, **kw)
        self.warmup = max(rsi_period, atr_period) * 3

    def prepare(self, df):
        df = df.copy()
        df["rsi"] = ta.rsi(df.close, self.rsi_period)
        df["atr"] = ta.atr(df.high, df.low, df.close, self.atr_period)
        return df

    def on_bar(self, df, position):
        r = df.iloc[-1]
        if pd.isna(r.rsi) or pd.isna(r.atr) or r.atr <= 0:
            return self.flat(df.symbol_name, "warmup")
        if position:
            if position.side.value == "BUY" and r.rsi >= self.exit_level:
                return Signal(SignalType.CLOSE, df.symbol_name, reason="RSI back to mean")
            if position.side.value == "SELL" and r.rsi <= self.exit_level:
                return Signal(SignalType.CLOSE, df.symbol_name, reason="RSI back to mean")
            return self.flat(df.symbol_name)
        if r.rsi <= self.low:
            return Signal(SignalType.LONG, df.symbol_name, (self.low - r.rsi) / self.low + 0.5,
                          f"RSI {r.rsi:.0f} oversold",
                          r.close - self.atr_stop * r.atr, r.close + self.atr_stop * r.atr * self.rr)
        if r.rsi >= self.high:
            return Signal(SignalType.SHORT, df.symbol_name, (r.rsi - self.high) / (100 - self.high) + 0.5,
                          f"RSI {r.rsi:.0f} overbought",
                          r.close + self.atr_stop * r.atr, r.close - self.atr_stop * r.atr * self.rr)
        return self.flat(df.symbol_name)


@register("breakout")
class DonchianBreakout(Strategy):
    """Donchian channel breakout with ATR volatility filter and trailing exit."""

    def __init__(self, entry: int = 20, exit: int = 10, atr_period: int = 14,
                 atr_stop: float = 2.0, rr: float = 3.0, **kw):
        super().__init__(entry=entry, exit=exit, atr_period=atr_period,
                         atr_stop=atr_stop, rr=rr, **kw)
        self.warmup = max(entry, atr_period) + 5

    def prepare(self, df):
        df = df.copy()
        lo, hi = ta.donchian(df.high, df.low, self.entry)
        df["dc_low"], df["dc_high"] = lo.shift(1), hi.shift(1)
        xlo, xhi = ta.donchian(df.high, df.low, self.exit)
        df["x_low"], df["x_high"] = xlo.shift(1), xhi.shift(1)
        df["atr"] = ta.atr(df.high, df.low, df.close, self.atr_period)
        return df

    def on_bar(self, df, position):
        r = df.iloc[-1]
        if pd.isna(r.dc_high) or pd.isna(r.atr) or r.atr <= 0:
            return self.flat(df.symbol_name, "warmup")
        if position:
            if position.side.value == "BUY" and r.close < r.x_low:
                return Signal(SignalType.CLOSE, df.symbol_name, reason="channel exit")
            if position.side.value == "SELL" and r.close > r.x_high:
                return Signal(SignalType.CLOSE, df.symbol_name, reason="channel exit")
            return self.flat(df.symbol_name)
        if r.close > r.dc_high:
            return Signal(SignalType.LONG, df.symbol_name, 0.8, f"{self.entry}-bar breakout up",
                          r.close - self.atr_stop * r.atr, r.close + self.atr_stop * r.atr * self.rr)
        if r.close < r.dc_low:
            return Signal(SignalType.SHORT, df.symbol_name, 0.8, f"{self.entry}-bar breakout down",
                          r.close + self.atr_stop * r.atr, r.close - self.atr_stop * r.atr * self.rr)
        return self.flat(df.symbol_name)


@register("bollinger_scalp")
class BollingerScalp(Strategy):
    """Band-touch scalper: fade Bollinger extremes, exit at the mid band."""

    def __init__(self, period: int = 20, std: float = 2.2, atr_period: int = 14,
                 atr_stop: float = 1.5, rr: float = 1.2, **kw):
        super().__init__(period=period, std=std, atr_period=atr_period,
                         atr_stop=atr_stop, rr=rr, **kw)
        self.warmup = period + atr_period + 5

    def prepare(self, df):
        df = df.copy()
        lo, mid, hi = ta.bollinger(df.close, self.period, self.std)
        df["bb_low"], df["bb_mid"], df["bb_high"] = lo, mid, hi
        df["atr"] = ta.atr(df.high, df.low, df.close, self.atr_period)
        return df

    def on_bar(self, df, position):
        r = df.iloc[-1]
        if pd.isna(r.bb_low) or pd.isna(r.atr) or r.atr <= 0:
            return self.flat(df.symbol_name, "warmup")
        if position:
            if position.side.value == "BUY" and r.close >= r.bb_mid:
                return Signal(SignalType.CLOSE, df.symbol_name, reason="mid band")
            if position.side.value == "SELL" and r.close <= r.bb_mid:
                return Signal(SignalType.CLOSE, df.symbol_name, reason="mid band")
            return self.flat(df.symbol_name)
        if r.low <= r.bb_low:
            return Signal(SignalType.LONG, df.symbol_name, 0.7, "lower band touch",
                          r.close - self.atr_stop * r.atr, r.close + self.atr_stop * r.atr * self.rr)
        if r.high >= r.bb_high:
            return Signal(SignalType.SHORT, df.symbol_name, 0.7, "upper band touch",
                          r.close + self.atr_stop * r.atr, r.close - self.atr_stop * r.atr * self.rr)
        return self.flat(df.symbol_name)


@register("momentum_macd")
class MomentumMACD(Strategy):
    """MACD histogram flip confirmed by a 200-EMA trend filter."""

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9, trend: int = 200,
                 atr_period: int = 14, atr_stop: float = 2.0, rr: float = 2.5, **kw):
        super().__init__(fast=fast, slow=slow, signal=signal, trend=trend,
                         atr_period=atr_period, atr_stop=atr_stop, rr=rr, **kw)
        self.warmup = trend + 10

    def prepare(self, df):
        df = df.copy()
        line, sig, hist = ta.macd(df.close, self.fast, self.slow, self.signal)
        df["macd"], df["macd_sig"], df["macd_hist"] = line, sig, hist
        df["trend_ema"] = ta.ema(df.close, self.trend)
        df["atr"] = ta.atr(df.high, df.low, df.close, self.atr_period)
        return df

    def on_bar(self, df, position):
        r, p = df.iloc[-1], df.iloc[-2]
        if pd.isna(r.trend_ema) or pd.isna(r.atr) or r.atr <= 0:
            return self.flat(df.symbol_name, "warmup")
        flip_up = p.macd_hist <= 0 < r.macd_hist
        flip_dn = p.macd_hist >= 0 > r.macd_hist
        if position:
            if (position.side.value == "BUY" and flip_dn) or (position.side.value == "SELL" and flip_up):
                return Signal(SignalType.CLOSE, df.symbol_name, reason="momentum flip")
            return self.flat(df.symbol_name)
        if flip_up and r.close > r.trend_ema:
            return Signal(SignalType.LONG, df.symbol_name, 0.85, "MACD flip up in uptrend",
                          r.close - self.atr_stop * r.atr, r.close + self.atr_stop * r.atr * self.rr)
        if flip_dn and r.close < r.trend_ema:
            return Signal(SignalType.SHORT, df.symbol_name, 0.85, "MACD flip down in downtrend",
                          r.close + self.atr_stop * r.atr, r.close - self.atr_stop * r.atr * self.rr)
        return self.flat(df.symbol_name)
