# 💰 Money Minter

**A fully functional automated FX trading robot** — regime-aware strategies, a
trailing-stop trade manager, broker-accurate risk sizing, an event-driven
backtester, a validation battery, and a real MetaTrader 5 / Exness adapter that
defaults to **demo accounts**.

It also ships a **simulated MetaTrader 5 terminal**, so the entire order-placing
code path can be tested end-to-end — with no Windows, no broker, no waiting.

> ⚠️ **Read this before risking anything.** No strategy included here is
> guaranteed to be profitable, and the offline data feed is deliberately
> *calibrated to have no exploitable edge*. On synthetic data the validator will
> correctly tell you most strategies are **rejected**. That is the point: the
> tooling is built to stop you fooling yourself. Nothing here is financial advice.

---

## What actually changed in 2.0

| Area | Before | Now |
|---|---|---|
| **Exits** | Fixed SL/TP only | Trailing stops, break-even at 1R, time stops, structural exits — one shared implementation used by backtest, paper and live |
| **Strategies** | 5 textbook triggers, 2 unprofitable | 6 regime-gated strategies that decline to trade without an edge, plus compatibility aliases |
| **Sizing** | Wrong for JPY/gold pairs | Quote-currency → USD conversion, USD-notional leverage cap, broker lot-grid snapping |
| **Risk** | Per-trade % only | + correlation groups, portfolio risk cap, weekly loss limit, loss-streak pause |
| **Execution** | Optimistic fills | Spread on both sides, extra slippage on stops, commission, triple-Wednesday swap, gap-through-stop |
| **Validation** | Best-of-grid optimise | Multi-sample battery, walk-forward, Monte Carlo, cost sensitivity, explicit verdict |
| **Demo testing** | Needs Windows + a real broker | `moneyminter demo` runs the real live code against a simulated terminal in seconds |
| **Live runner** | Stops sat only in Python | Stops live at the broker *and* trail; broker-initiated closes are reconciled into the P&L |
| **Dashboard** | Paper only | Paper + demo-account panel + validation evidence |

---

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

> **On an Android phone?** The robot cannot run on Android (MT5's mobile app has
> no API) — but it makes an excellent dashboard. See **[ANDROID.md](ANDROID.md)**.

> **On a Mac?** Follow **[MAC_SETUP.md](MAC_SETUP.md)** — a numbered walkthrough
> from a fresh Mac to trading an Exness demo, including the Wine/`mt5_mac`
> quirks that trip people up.

## Quick start

```bash
# 1. See the strategy library
python -m moneyminter strategies

# 2. Backtest (note: synthetic data has no edge — see "About the data" below)
python -m moneyminter backtest --symbol EUR/USD --timeframe H1 --strategy trend_pullback --show-trades

# 3. Test the REAL live code path on a simulated MT5 terminal — no Windows needed
python -m moneyminter demo --symbols EUR/USD GBP/USD --timeframe M5 --polls 3000

# 4. Paper trade with a compressed clock
python -m moneyminter trade --symbols EUR/USD GBP/USD --speed 600

# 5. Dashboard (paper + demo + validation)
python -m moneyminter dashboard --port 8000

# 6. Real Exness demo account (see LIVE_TRADING.md)
python -m moneyminter connect --symbols EUR/USD
python -m moneyminter live --symbols EUR/USD --dry-run
python -m moneyminter live --symbols EUR/USD --risk 0.0025
```

### `moneyminter demo` — the fastest way to prove the plumbing works

```bash
python -m moneyminter demo --symbols EUR/USD GBP/USD --timeframe M5 --polls 6000
```

```
Simulated DEMO account #78234001 — MoneyMinter-Sim
  Balance 10,000.00 USD | leverage 1:500
  OK EUR/USD   -> EURUSDm    1.07527    spread   6pts min lot 0.01 | size 0.24 lots
  OK GBP/USD   -> GBPUSDm    1.27535    spread   9pts min lot 0.01 | size 0.24 lots

==================================================================
  Simulated demo result (20.7s of wall time)
==================================================================
  Start equity     : 10,000.00
  Final equity     : 9,960.31  (-0.40%)
  Realised P&L     : -39.69
  Trades closed    : 1 (0W / 1L, win 0.0%)
  Average R        : -1.02R
  ...
    · Position 100001 closed by broker: stop loss (P&L -39.69)
    · Opened GBP/USD BUY 0.17 lots @ 1.28254 SL 1.28024 TP 1.29054 — uptrend pullback
```

This runs `MT5Broker` and `LiveTrader` **unmodified** against
`moneyminter.live.sim_terminal.SimTerminal`, which implements the subset of the
`MetaTrader5` API the adapter uses — including server-side SL/TP execution,
requotes, invalid-stop rejection, insufficient margin, and disconnects. What it
proves: orders are constructed correctly, symbols/suffixes resolve, lots land on
the broker grid, stops fire from the broker side, trailing updates are accepted,
and broker-initiated closes are reconciled into the P&L. What it does **not**
prove: that a strategy makes money.

---

## The strategies

Every strategy here is built around a documented, persistent market feature, and
every one **declines to trade** when that feature is absent. A strategy that is
always in the market has no filter.

| Name | Style | Edge it trades | Exits |
|---|---|---|---|
| `trend_pullback` | Trend | Aligned EMA structure + ADX/DI dominance, entered on a **pullback** into value rather than the breakout bar | ATR trail, break-even at 1R, structural |
| `donchian_trend` | Trend breakout | N-bar breakout gated on **widening channels** and directional efficiency | ATR trail, break-even at 1R |
| `session_breakout` | Intraday | London/NY break of the **Asian range**, with a range-size filter; flat by late session | ATR trail, session-end exit |
| `squeeze_momentum` | Volatility expansion | Bollinger-inside-Keltner compression resolving, confirmed by a MACD histogram already in agreement | ATR trail, mid-band stop |
| `supertrend_adx` | Trend | Supertrend flip only when DI is directional and ADX is expanding | ATR trail, flip exit |
| `range_reversion` | Mean reversion | **Failed stretch**: price pokes outside a band then closes back inside, gated on a non-trending regime | Mid-band target, time stop |
| `ma_crossover`, `momentum_macd`, `breakout`, `rsi_reversion`, `bollinger_scalp` | legacy | Kept as compatible aliases that now inherit the improved exits | — |

### Why these specific filters

* **Pullback entries beat breakout entries for trend following** because they
  get a tighter stop for the same structural idea. The breakout bar has already
  moved; the pullback lets the trail do the work.
* **Positive skew is the point.** Trend strategies here target 4–8R and trail,
  so expectancy comes from a minority of large winners. Cutting winners short
  with a fixed 1:1 target is the single most common way to destroy a trend edge.
* **Regime gates first, triggers second.** `range_reversion` will not fire while
  ADX is expanding — that is exactly when mean reversion gets run over.

### Writing your own

```python
from moneyminter import Strategy, register, Signal, SignalType, run_backtest
from moneyminter import indicators as ta

@register("my_edge")
class MyEdge(Strategy):
    """Buy strength above the 50-EMA, trail out with ATR."""
    warmup = 80

    def prepare(self, df):
        df = df.copy()
        df["ema"] = ta.ema(df.close, 50)
        df["atr"] = ta.atr(df.high, df.low, df.close, 14)
        return df

    def on_bar(self, df, position):
        sym = getattr(df, "symbol_name", "?")
        r = df.iloc[-1]
        if position:
            return self.flat(sym)
        if r.close > r.ema:
            return Signal(SignalType.LONG, sym,
                          stop_loss=r.close - 2 * r.atr,
                          take_profit=r.close + 8 * r.atr,
                          trail_atr=3.0, breakeven_at_r=1.0, max_bars=200)
        return self.flat(sym)

run_backtest("EUR/USD", "H1", "my_edge").print_report()
```

`Signal` also accepts `trail_atr`, `breakeven_at_r` and `max_bars`, which the
shared trade manager applies identically in backtest, paper and live.

---

## Risk management

```
units = (equity × risk_per_trade × confidence) / (|entry − stop| × usd_per_quote)
```

then capped by USD-notional leverage, and snapped **down** to the broker's lot
grid. If the snapped size is below the minimum lot the trade is **skipped** — not
rounded up. (Rounding 0.003 lots up to the 0.01 minimum on a small account risks
more than 3× your limit, which is how people blow up "safely".)

Guardrails, all enforced in one place and covered by tests:

| Guard | Default | Behaviour |
|---|---|---|
| `risk_per_trade` | 0.5% | Sized so a stop-out costs exactly this |
| `max_positions` | 3 | Concurrent cap |
| `max_per_currency_group` | 1 | Blocks e.g. long EUR/USD + long GBP/USD (one USD bet) |
| `max_portfolio_risk` | 3% | Sum of open risk |
| `max_daily_loss` | 3% | Pauses until the next session |
| `max_weekly_loss` | 7% | Pauses for the week |
| `max_drawdown` | 15% | Hard kill switch from the peak |
| `loss_streak_pause` | 4 losses | Symbol sits out for 4 hours |

---

## Validation: earning the right to trade

```bash
python -m moneyminter validate --strategy trend_pullback --samples 12 --json reports/validation.json
python -m moneyminter validate --strategy all --samples 8
```

The battery runs five independent checks and refuses to hand out a verdict on
vibes:

1. **Cross-sectional samples** — does the *same* parameters set work on many
   independent price paths and symbols? Reported as the share profitable and the
   **worst** case, not the average.
2. **Walk-forward** — parameters fitted on slice *n* must survive slice *n+1*.
3. **Monte Carlo on trade order** — reshuffles trades to expose path dependence;
   reports the 5th-percentile outcome, 95th-percentile drawdown, risk of ruin.
4. **Cost sensitivity** — doubles the spread. An edge that evaporates is spread
   noise, not edge.
5. **Sample sufficiency** — fewer than 60 trades proves nothing.

`VALIDATED` requires ≥70% of samples profitable, median PF ≥ 1.3, ≥60 trades,
walk-forward folds holding up, ≥60% spread survival, and Monte Carlo drawdown
under the plan. It is deliberately hard to reach.

Two self-tests keep the validator honest:

* `test_null_strategy_is_rejected_on_noise` — a trend follower on random data
  must **not** pass.
* `test_score_rises_monotonically_with_edge_strength` — injecting genuine serial
  correlation must raise the score monotonically (33.8 → 38.6 → 98.8 in the
  reference run).

A validator that passes everything is worse than none.

---

## About the data (please read)

The default feed is `synthetic`: an offline simulator calibrated to be
statistically honest — returns with **no autocorrelation**, a variance ratio of
≈1.0, fat tails, volatility clustering and only weak slow trend regimes.

That means strategy results on it hover around **minus the cost budget**. This is
correct behaviour, not a bug. A generator with an easy edge would make every
strategy look profitable and teach you nothing. Real validation needs real
prices:

```bash
python -m moneyminter validate --source yahoo --strategy trend_pullback --samples 8
python -m moneyminter backtest --source csv --csv data/eurusd_h1.csv --strategy donchian_trend
```

Yahoo FX history is free and offline-falls-back to synthetic. For serious work,
export candles from your broker (MT5 → *File → Save As*, or `copy_rates_range`)
and use `--source csv`.

---

## Live / demo trading

Full walkthrough in **[LIVE_TRADING.md](LIVE_TRADING.md)**. The safety model:

* **Demo-only by default.** A live account raises unless you pass
  `--i-understand-live-risk`.
* **Balance cap** on live accounts (`--max-live-balance`, default 500) and a
  typed `TRADE` confirmation.
* **Magic-number isolation** — it only manages positions it opened.
* **Broker-side SL/TP on every order**, so your risk is capped even if your
  machine dies.
* **Trailing stops are pushed to the broker**, not just tracked in Python.
* **Reconciliation** — trades the broker closed (SL/TP, or you by hand) appear in
  the P&L with the broker's own fill price and reason.

---

## CLI reference

| Command | Purpose |
|---|---|
| `backtest` | Historical simulation with 20+ metrics (`--json`, `--show-trades`) |
| `optimize` | Grid search: `--grid '{"fast":[12,20],"slow":[34,55]}'` |
| `walkforward` | Out-of-sample folds, optionally with per-fold optimisation |
| `validate` | The full robustness battery, with an explicit verdict |
| `demo` | **Real live code path on a simulated MT5 terminal** |
| `trade` | Paper trading with a compressed clock |
| `dashboard` | Web UI: paper, demo, validation |
| `connect` / `live` | Real MT5/Exness connection check / trading (demo by default) |
| `strategies` | List strategies and instruments |

---

## Dashboard API

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard (paper / demo / research tabs) |
| `GET /api/state` | Paper bot state |
| `GET /api/meta` | Strategies, instruments, timeframes, last validation |
| `POST /api/start` · `/api/stop` | Control the paper bot |
| `POST /api/close/{id}` | Manually close a position |
| `POST /api/backtest` | Run a backtest |
| `POST /api/validate` | Run the validation battery |
| `POST /api/demo/start` · `/api/demo/stop` · `GET /api/demo/state` | Simulated demo account |
| `WS /ws` · `WS /ws/demo` | 1 Hz state streams |

---

## Tests

```bash
python -m pytest tests -q     # 113 tests
```

Coverage includes indicators, sizing maths (including the JPY conversion trap),
the trade manager's stop/target/trail ordering, broker realism (lot grid, stop
slippage, commission/swap), risk guardrails, every strategy, the walk-forward and
Monte Carlo machinery, the validator's ability to reject noise *and* detect edge,
the MT5 adapter against a mock terminal, and **the full live path end-to-end
against the simulated terminal**.

---

## License

MIT
