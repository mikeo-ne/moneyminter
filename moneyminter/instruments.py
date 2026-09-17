"""FX instrument metadata: pip sizes, spreads, contract sizes."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    symbol: str
    pip: float
    typical_spread_pips: float
    contract_size: int = 100_000
    base_price: float = 1.0
    daily_vol_pips: float = 70.0

    @property
    def base(self) -> str:
        return self.symbol[:3]

    @property
    def quote(self) -> str:
        return self.symbol[-3:]

    def pips(self, price_delta: float) -> float:
        return price_delta / self.pip

    def price(self, pips: float) -> float:
        return pips * self.pip


_DEFS = [
    Instrument("EUR/USD", 0.0001, 0.6, base_price=1.0850, daily_vol_pips=65),
    Instrument("GBP/USD", 0.0001, 0.9, base_price=1.2720, daily_vol_pips=80),
    Instrument("USD/JPY", 0.01, 0.8, base_price=151.20, daily_vol_pips=75),
    Instrument("AUD/USD", 0.0001, 0.8, base_price=0.6580, daily_vol_pips=60),
    Instrument("USD/CHF", 0.0001, 1.0, base_price=0.9050, daily_vol_pips=55),
    Instrument("USD/CAD", 0.0001, 1.1, base_price=1.3620, daily_vol_pips=60),
    Instrument("NZD/USD", 0.0001, 1.2, base_price=0.6010, daily_vol_pips=58),
    Instrument("EUR/JPY", 0.01, 1.2, base_price=164.10, daily_vol_pips=85),
    Instrument("GBP/JPY", 0.01, 1.6, base_price=192.30, daily_vol_pips=110),
    Instrument("EUR/GBP", 0.0001, 1.0, base_price=0.8530, daily_vol_pips=45),
]

INSTRUMENTS = {i.symbol: i for i in _DEFS}


def get_instrument(symbol: str) -> Instrument:
    symbol = normalize(symbol)
    if symbol not in INSTRUMENTS:
        raise KeyError(f"Unknown instrument {symbol!r}. Known: {', '.join(INSTRUMENTS)}")
    return INSTRUMENTS[symbol]


def normalize(symbol: str) -> str:
    s = symbol.upper().replace("_", "/").replace("-", "/")
    if "/" not in s and len(s) == 6:
        s = f"{s[:3]}/{s[3:]}"
    return s
