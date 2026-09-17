"""Strategy plugin framework."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Optional, Type

import pandas as pd

from ..models import Position, Signal, SignalType

_REGISTRY: Dict[str, Type["Strategy"]] = {}


def register(name: str):
    def deco(cls: Type["Strategy"]):
        cls.name = name
        _REGISTRY[name] = cls
        return cls
    return deco


def get_strategy(name: str, **params) -> "Strategy":
    if name not in _REGISTRY:
        raise KeyError(f"Unknown strategy {name!r}. Available: {', '.join(sorted(_REGISTRY))}")
    return _REGISTRY[name](**params)


def available_strategies() -> Dict[str, Type["Strategy"]]:
    return dict(sorted(_REGISTRY.items()))


class Strategy(ABC):
    """Base strategy.

    ``prepare`` computes indicator columns once (vectorised), ``on_bar`` is
    called per bar with the history up to and including the current bar.
    """

    name: str = "base"
    #: minimum bars required before signals are produced
    warmup: int = 50
    params: dict

    def __init__(self, **params):
        self.params = params
        for k, v in params.items():
            setattr(self, k, v)

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df

    @abstractmethod
    def on_bar(self, df: pd.DataFrame, position: Optional[Position]) -> Signal:
        ...

    def flat(self, symbol: str, reason: str = "") -> Signal:
        return Signal(SignalType.FLAT, symbol, reason=reason)

    def describe(self) -> dict:
        return {"name": self.name, "params": self.params, "warmup": self.warmup,
                "doc": (self.__doc__ or "").strip().split("\n")[0]}
