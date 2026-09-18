# 💰 Money Minter

**A fully functional automated forex trading robot** — strategy engine, risk manager,
simulated broker, event-driven backtester, parameter optimizer and a real-time web
dashboard, all in one Python package.

> ⚠️ **Defaults to paper trading.** It can also trade a **real MetaTrader 5 / Exness
> account** (`connect` / `live` commands) — demo-only unless you explicitly override, see
> **[LIVE_TRADING.md](LIVE_TRADING.md)**. Nothing here is financial advice, and no strategy
> included is guaranteed to be profitable.

---

## Features

| Area | What you get |
|---|---|
| **Strategies** | 5 built-in (trend, mean-reversion, breakout, scalping, momentum) + a plugin API |
| **Indicators** | EMA/SMA, RSI, ATR, ADX, MACD, Bollinger, Donchian — vectorised pandas, no TA-Lib |
| **Risk** | % -of-equity position sizing, ATR stops, max positions, daily loss limit, drawdown kill switch |
| **Execution** | Paper broker with bid/ask spread, slippage, commission per million and overnight swap |
| **Backtesting** | Event-driven, intrabar stop/target fills, 18 performance metrics |
| **Validation** | Grid-search optimizer + walk-forward out-of-sample folds |
| **Live engine** | Threaded multi-symbol tick loop with a virtual market clock (compress days into minutes) |
| **Dashboard** | FastAPI + WebSocket UI: live equity curve, positions, trades, activity log, one-click backtests |
| **Live trading** | MetaTrader 5 adapter (Exness & any MT5 broker): symbol-suffix auto-detection, lot conversion, broker-side SL/TP, magic-number isolation |
| **Data** | Offline synthetic FX simulator (default), Yahoo Finance history, MT5, or your own CSVs |

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Quick start

```bash
# 1. See what's available
python -m moneyminter strategies

# 2. Backtest a strategy
python -m moneyminter backtest --symbol EUR/USD --timeframe H1 --strategy breakout --show-trades

# 3. Paper trade in the terminal (600x speed)
python -m moneyminter trade --symbols EUR/USD GBP/USD USD/JPY --speed 600

# 4. Launch the live dashboard
python -m moneyminter dashboard --port 8000

# 5. Trade your real Exness demo account (Windows / macOS / Linux — see LIVE_TRADING.md)
python -m moneyminter connect --symbols EUR/USD          # check connection, no orders
python -m moneyminter live --symbols EUR/USD --dry-run   # full loop, no orders
python -m moneyminter live --symbols EUR/USD --risk 0.005
```

See **[LIVE_TRADING.md](LIVE_TRADING.md)** for the complete Exness setup guide.

### Example backtest output

```
=== Money Minter backtest — breakout on EUR/USD H1 ===
  Initial balance  : $10,000.00
  Final equity     : $26,718.28
  Net profit       : $16,718.28 (167.18%)
  Trades           : 110 (70W / 40L)
  Win rate         : 63.64%
  Profit factor    : 4.20
  Expectancy       : $152.45/trade
  Max drawdown     : $1,025.20 (4.04%)
  Sharpe / Sortino : 8.21 / 14.07
```

*(Synthetic data — treat these numbers as a smoke test of the engine, not an edge.)*

## CLI reference

| Command | Purpose |
|---|---|
| `backtest` | Historical simulation with a full metrics report (`--json report.json`) |
| `optimize` | Grid-search parameters: `--grid '{"fast":[8,12,20],"slow":[26,34,55]}'` |
| `walkforward` | Sequential out-of-sample folds (`--folds 4`) to check robustness |
| `trade` | Run the robot live against the paper broker |
| `dashboard` | Web UI + REST/WebSocket API |
| `connect` | Verify an MT5/Exness connection, resolve symbols, preview lot sizes |
| `live` | Trade a real MT5/Exness account (demo-only by default) |
| `strategies` | List strategies and instruments |

Shared flags: `--symbol/--symbols --timeframe --strategy --balance --source {synthetic,yahoo,csv}
--bars --seed --risk --max-positions --max-daily-loss --max-drawdown --params '{"fast":8}'`

## Strategies

| Name | Style | Logic |
|---|---|---|
| `ma_crossover` | Trend | Fast/slow EMA cross, ADX filter, ATR stop + R:R target |
| `momentum_macd` | Momentum | MACD histogram flip confirmed by a 200-EMA regime filter |
| `breakout` | Breakout | Donchian N-bar breakout, shorter channel as the exit |
| `rsi_reversion` | Mean reversion | Fades RSI extremes, exits back at RSI 50 |
| `bollinger_scalp` | Scalping | Fades band touches, exits at the middle band |

### Writing your own

```python
from moneyminter import Strategy, register, Signal, SignalType, run_backtest
from moneyminter import indicators as ta

@register("my_edge")
class MyEdge(Strategy):
    """Buy strength above the 50-EMA."""
    warmup = 60

    def prepare(self, df):
        df = df.copy()
        df["ema"] = ta.ema(df.close, 50)
        df["atr"] = ta.atr(df.high, df.low, df.close, 14)
        return df

    def on_bar(self, df, position):
        r = df.iloc[-1]
        if position:
            return self.flat(df.symbol_name)
        if r.close > r.ema:
            return Signal(SignalType.LONG, df.symbol_name,
                          stop_loss=r.close - 2 * r.atr,
                          take_profit=r.close + 4 * r.atr)
        return self.flat(df.symbol_name)

run_backtest("EUR/USD", "H1", "my_edge").print_report()
```

`on_bar` receives all history up to the current bar and the open position (or `None`),
and returns a `Signal` (`LONG`, `SHORT`, `CLOSE` or `FLAT`).

## Risk management

Every entry is sized so that **hitting the stop loses exactly `risk_per_trade` of equity**:

```
units = (equity × risk_per_trade × confidence) / |entry − stop|
```

capped by leverage. Guardrails that block new entries:

- `max_positions` — concurrent position cap
- `max_daily_loss` — pauses trading for the rest of the day (resets at the next session)
- `max_drawdown` — hard kill switch from the equity peak
- `max_leverage` — notional exposure ceiling

## Python API

```python
from moneyminter import run_backtest, optimize, TradingBot, BotConfig, RiskConfig

res = run_backtest("GBP/USD", "H1", "momentum_macd", balance=25_000,
                   risk=RiskConfig(risk_per_trade=0.005))
print(res.metrics.summary())

best = optimize("EUR/USD", "H1", "breakout", {"entry": [20, 40], "rr": [2, 3]}, metric="sharpe")

bot = TradingBot(BotConfig(symbols=["EUR/USD"], strategy="breakout", speed=600))
bot.start(); ...; bot.stop()
print(bot.state()["equity"])
```

## Dashboard API

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard UI |
| `GET /api/state` | Full bot state (equity, positions, trades, events) |
| `GET /api/meta` | Strategies, instruments, timeframes |
| `POST /api/start` | Start with `{symbols, timeframe, strategy, balance, speed, risk_per_trade}` |
| `POST /api/stop` | Stop and flatten |
| `POST /api/close/{id}` | Manually close a position |
| `POST /api/backtest` | Run a backtest, returns metrics + equity curve |
| `WS /ws` | State stream, 1 Hz |

## Data sources

- **`synthetic`** (default) — offline simulator: mean-reverting random walk with
  session volatility (Tokyo/London/NY), trending regimes and news shocks. Deterministic per `--seed`.
- **`yahoo`** — free Yahoo Finance FX candles; silently falls back to synthetic if offline.
- **`csv`** — `--source csv --csv data/eurusd.csv` with columns `timestamp,open,high,low,close[,volume]`.

## Tests

```bash
python -m pytest tests -q     # 37 tests: indicators, risk maths, broker P&L, strategies,
                              # engine, web API, and the MT5 adapter (mocked terminal)
```

## Going live

**MetaTrader 5 / Exness is supported out of the box** on **Windows, macOS and Linux** —
see **[LIVE_TRADING.md](LIVE_TRADING.md)**. The official `MetaTrader5` package is
Windows-only, so Money Minter also ships a `mt5_mac` backend for macOS and an RPyC remote
backend for Linux/Wine, Docker, or a Windows VPS (`--mt5-host`).

For other brokers, the `Broker` base class in `moneyminter/broker.py` is the single
integration point: implement `open()`, `close()` and `equity()` (see
`moneyminter/live/mt5_broker.py` as a reference). Before you risk real money:

1. Run `walkforward` — if out-of-sample folds don't hold up, the edge isn't real.
2. Paper trade for weeks, not hours.
3. Start at `--risk 0.0025` and keep the drawdown kill switch on.
4. Synthetic backtest results say nothing about live performance.

## License

MIT
