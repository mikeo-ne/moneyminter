"""Money Minter — a fully functional automated forex trading robot."""
__version__ = "2.0.0"

from .models import (Candle, Position, Side, Signal, SignalType, Tick, Trade,
                     utcnow)
from .instruments import INSTRUMENTS, get_instrument, normalize
from .risk import RiskConfig, RiskManager
from .broker import Broker, ExecutionConfig, PaperBroker
from .management import ExitDecision, hard_breach, manage_bar
from .strategies import Strategy, available_strategies, get_strategy, register
from .backtest import BacktestResult, optimize, run_backtest, walk_forward
from .validation import (MonteCarloResult, SampleResult, ValidationReport, monte_carlo,
                         scan, validate)
from .engine import BotConfig, TradingBot

__all__ = [
    "Candle", "Position", "Side", "Signal", "SignalType", "Tick", "Trade", "utcnow",
    "INSTRUMENTS", "get_instrument", "normalize",
    "RiskConfig", "RiskManager",
    "Broker", "ExecutionConfig", "PaperBroker",
    "ExitDecision", "hard_breach", "manage_bar",
    "Strategy", "available_strategies", "get_strategy", "register",
    "BacktestResult", "optimize", "run_backtest", "walk_forward",
    "ValidationReport", "SampleResult", "MonteCarloResult", "monte_carlo",
    "validate", "scan",
    "BotConfig", "TradingBot", "__version__",
]
