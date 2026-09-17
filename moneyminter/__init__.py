"""Money Minter — a fully functional automated forex trading robot."""
__version__ = "1.0.0"

from .models import Candle, Position, Side, Signal, SignalType, Tick, Trade
from .instruments import INSTRUMENTS, get_instrument
from .risk import RiskConfig, RiskManager
from .broker import ExecutionConfig, PaperBroker
from .strategies import Strategy, available_strategies, get_strategy, register
from .backtest import BacktestResult, optimize, run_backtest, walk_forward
from .engine import BotConfig, TradingBot

__all__ = [
    "Candle", "Position", "Side", "Signal", "SignalType", "Tick", "Trade",
    "INSTRUMENTS", "get_instrument", "RiskConfig", "RiskManager",
    "ExecutionConfig", "PaperBroker", "Strategy", "available_strategies",
    "get_strategy", "register", "BacktestResult", "optimize", "run_backtest",
    "walk_forward", "BotConfig", "TradingBot", "__version__",
]
