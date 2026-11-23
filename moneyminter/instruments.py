"""Instrument metadata: pip sizes, spreads, contract sizes, USD conversion.

The important addition over a naive "1 unit = 1 USD per big figure" model is
``pip_value_per_unit``. A 1-lot EUR/USD position and a 1-lot USD/JPY position
don't have the same dollar-per-pip value, so risk-based sizing has to convert
from the pair's quote currency into the account currency. Without this, sizing
a JPY pair over-risks by a factor equal to the USD/JPY rate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class Instrument:
    symbol: str
    pip: float
    typical_spread_pips: float
    contract_size: int = 100_000
    base_price: float = 1.0
    daily_vol_pips: float = 70.0
    #: "fx" | "metal" | "index"
    kind: str = "fx"
    #: broker constraints, used so paper trading matches MT5 exactly
    min_lot: float = 0.01
    lot_step: float = 0.01
    max_lot: float = 200.0
    #: quote-currency units per 1 USD (used to convert P&L into USD)
    _usd_per_quote: float = 1.0

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

    def usd_per_quote(self) -> float:
        """How many USD one unit of the quote currency is worth."""
        if self.quote == "USD":
            return 1.0
        if self.base == "USD":
            return 1.0 / self.base_price
        return self._usd_per_quote

    def pip_value_per_unit(self) -> float:
        """USD P&L from a one-pip move in one unit of the base currency."""
        return self.pip * self.usd_per_quote()

    def round_lots(self, lots: float, *, mode: str = "down") -> float:
        """Round a raw lot size to the broker grid.

        ``mode='down'`` never exceeds the requested risk (used for sizing),
        ``mode='nearest'`` is used when snapping an existing position.
        """
        step = self.lot_step or 0.01
        if lots <= 0:
            return 0.0
        if mode == "down":
            n = int(lots / step + 1e-9)
        else:
            n = int(round(lots / step))
        return round(min(n * step, self.max_lot), 8)


# quote->USD at the reference price; refreshed from live prices where available
_DEFS = [
    Instrument("EUR/USD", 0.0001, 0.6, base_price=1.0850, daily_vol_pips=65),
    Instrument("GBP/USD", 0.0001, 0.9, base_price=1.2720, daily_vol_pips=80),
    Instrument("USD/JPY", 0.01, 0.8, base_price=151.20, daily_vol_pips=75),
    Instrument("AUD/USD", 0.0001, 0.8, base_price=0.6580, daily_vol_pips=60),
    Instrument("USD/CHF", 0.0001, 1.0, base_price=0.9050, daily_vol_pips=55),
    Instrument("USD/CAD", 0.0001, 1.1, base_price=1.3620, daily_vol_pips=60),
    Instrument("NZD/USD", 0.0001, 1.2, base_price=0.6010, daily_vol_pips=58),
    Instrument("EUR/JPY", 0.01, 1.2, base_price=164.10, daily_vol_pips=85),
    Instrument("GBP/JPY", 0.01, 1.6, base_price=192.30, daily_vol_pips=110,
               _usd_per_quote=1 / 151.2),
    Instrument("EUR/GBP", 0.0001, 1.0, base_price=0.8530, daily_vol_pips=45,
               _usd_per_quote=1.2720),
    Instrument("XAU/USD", 0.1, 25.0, contract_size=100, base_price=2340.0,
               daily_vol_pips=180.0, kind="metal", max_lot=50.0),
    Instrument("XAG/USD", 0.01, 30.0, contract_size=5000, base_price=27.5,
               daily_vol_pips=200.0, kind="metal", max_lot=50.0),
]

INSTRUMENTS: Dict[str, Instrument] = {i.symbol: i for i in _DEFS}

#: aliases brokers commonly use
_ALIASES = {
    "XAUUSD": "XAU/USD", "GOLD": "XAU/USD",
    "XAGUSD": "XAG/USD", "SILVER": "XAG/USD",
    "USOIL": "XAU/USD",
}


def normalize(symbol: str) -> str:
    s = symbol.upper().replace("_", "/").replace("-", "/").strip()
    if s in _ALIASES:
        return _ALIASES[s]
    if "/" not in s and len(s) == 6:
        s = f"{s[:3]}/{s[3:]}"
    if s.endswith("M") and s not in INSTRUMENTS and len(s) == 7:
        # Exness style EURUSDm -> EUR/USD
        core = s[:-1]
        if len(core) == 6:
            s = f"{core[:3]}/{core[3:]}"
    return s


def get_instrument(symbol: str) -> Instrument:
    symbol = normalize(symbol)
    if symbol not in INSTRUMENTS:
        raise KeyError(f"Unknown instrument {symbol!r}. Known: {', '.join(INSTRUMENTS)}")
    return INSTRUMENTS[symbol]
