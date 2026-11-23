# Connecting Money Minter to your Exness account

This guide takes you from a working MT5 terminal to the robot placing orders on
your **Exness demo account**.

> **Do this first, before you install anything:** run the built-in simulator.
> `python -m moneyminter demo` exercises the exact same order-placing code —
> `MT5Broker`, broker-side stops, trailing-stop updates, reconciliation — against
> a fake terminal, in about 20 seconds and on any OS. It will not tell you
> whether a strategy is profitable (the prices are synthetic) but it *will* catch
> symbol-suffix, lot-size and stop-side bugs before you involve a broker.
> See [§0d](#0d-no-terminal-test-the-live-path-in-20-seconds).

> **Read this first.** Money Minter refuses to touch a real-money account unless you
> pass an explicit override flag (see [Moving to a live account](#moving-to-a-live-account)). That default is deliberate — keep it until the robot
> has proven itself on demo for weeks. Nothing here is financial advice, and the
> backtest numbers in the README come from a *simulator*, not real market data.

---

## 0. Pick your setup (Mac / Linux / Windows)

The official `MetaTrader5` Python package is Windows-only. Money Minter works around that
with three backends — pick the row that matches your machine:

| Your machine | Backend | What you install |
|---|---|---|
| **Windows** | `local` | `pip install MetaTrader5` |
| **macOS** (Intel or Apple Silicon) | `mac` | MT5 for macOS + `pip install mt5_mac` |
| **Linux**, or Mac where `mt5_mac` misbehaves | `rpyc` | MT5 under Wine + a bridge, then `--mt5-host` |
| **Any machine + a Windows VPS** | `rpyc` | Bridge on the VPS, `--mt5-host <vps-ip>` |

Money Minter auto-detects, so usually you don't pass anything. Jump to
[§0a macOS](#0a-macos) or [§0b Linux](#0b-linux), then continue at §1.

> **macOS users:** there is a complete numbered walkthrough in
> **[MAC_SETUP.md](MAC_SETUP.md)**. The summary below is the short version.

### 0a. macOS

MetaTrader ships a macOS build that is really the Windows binary inside a bundled Wine
wrapper, and `mt5_mac` talks to the Python inside it.

```bash
# 1. Install MetaTrader 5 for macOS from https://www.metatrader5.com/en/download
#    (it must end up at /Applications/MetaTrader 5.app)
pip install mt5_mac        # first run auto-downloads a small Windows Python into MT5's Wine
```

Then use the normal commands — add `--mt5-backend mac` if auto-detection picks wrong:

```bash
python -m moneyminter connect --symbols EUR/USD --mt5-backend mac
```

Money Minter handles the two macOS bridge quirks for you:

* `mt5_mac` reports success as `RES_E_SUCCESS` rather than `TRADE_RETCODE_DONE`,
  so the adapter accepts either — otherwise every successful fill would look like
  a rejection and the robot would silently never trade.
* MT5 stamps bars with the **broker's** server clock, and the macOS bridge can
  label that as UTC (so a UTC+2 broker yields bars two hours in the future). The
  adapter measures the skew against the newest bar and shifts bars onto UTC when
  the feed is live, logging a `broker clock` warning. Stale/replayed history is
  left alone rather than being "corrected" into the future.

If that proves flaky (Wine on Apple Silicon can be), use the Docker bridge instead:

```bash
pip install siliconmetatrader5     # runs MT5 in Docker/QEMU, serves RPyC on 8001
python -m moneyminter connect --symbols EUR/USD --mt5-host 127.0.0.1 --mt5-port 8001
```

### 0b. Linux

Run MT5 under Wine, then expose its Python API over a local bridge.

```bash
# 1. Wine + MT5
sudo apt install wine winetricks          # or: sudo pacman -S wine winetricks
wget https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe
wine mt5setup.exe                          # install, log into Exness, enable Algo Trading

# 2. Windows Python *inside* the same Wine prefix, plus the MT5 packages
wine python-3.9.13-amd64.exe               # from python.org, tick "Add to PATH"
wine python -m pip install MetaTrader5 mt5linux

# 3. Start the bridge (leave this running, MT5 open)
wine python -m mt5linux

# 4. From your NATIVE Linux python, in another terminal:
pip install rpyc
python -m moneyminter connect --symbols EUR/USD --mt5-host 127.0.0.1
```

### 0c. Windows VPS (most reliable for 24/5 running)

Rent a Windows VPS (~$10/mo), install MT5 + Python + `MetaTrader5` + `mt5linux` there, and
either run Money Minter entirely on the VPS (simplest — follow the Windows path) or run the
bridge there and drive it from your laptop:

```bash
# on the VPS
python -m mt5linux
# on your Mac/Linux laptop
python -m moneyminter connect --symbols EUR/USD --mt5-host <vps-ip>
```

> **Security:** the bridge is unauthenticated — anyone who can reach that port controls your
> account. Keep it on `localhost`, or tunnel over SSH
> (`ssh -L 18812:localhost:18812 user@vps`) and still connect to `127.0.0.1`. Never open
> port 18812 to the internet.

### 0d. No terminal? Test the live path in 20 seconds

```bash
python -m moneyminter demo --symbols EUR/USD GBP/USD --timeframe M5 --polls 6000
```

```
Simulated DEMO account #78234001 — MoneyMinter-Sim
  Balance 10,000.00 USD | leverage 1:500
  OK EUR/USD   -> EURUSDm    1.07527    spread   6pts min lot 0.01 | size 0.24 lots
  OK GBP/USD   -> GBPUSDm    1.27535    spread   9pts min lot 0.01 | size 0.24 lots

  Start equity     : 10,000.00
  Final equity     : 9,960.31  (-0.40%)
  Trades closed    : 1 (0W / 1L, win 0.0%)
  Average R        : -1.02R
  Activity         : close=1, info=1, open=1
    · Position 100001 closed by broker: stop loss (P&L -39.69)
    · Opened GBP/USD BUY 0.17 lots @ 1.28254 SL 1.28024 TP 1.29054 — uptrend pullback
```

What this proves, all of which is broker-independent plumbing:

* orders are constructed correctly and fill at the ask/bid plus slippage,
* broker symbol suffixes (`EURUSDm`) resolve,
* lot sizes land on the broker's grid, and undersized trades are **skipped**,
* stop-losses fire **from the broker side** even if your process dies,
* trailing-stop updates (`TRADE_ACTION_SLTP`) are accepted,
* requotes are retried, invalid stops and insufficient margin are surfaced,
* a disconnect does not corrupt risk state or latch the kill switch,
* broker-initiated closes are reconciled into the P&L with the real fill price.

Useful flags:

| Flag | Effect |
|---|---|
| `--polls 6000 --bars-per-poll 1` | 6,000 M5 bars ≈ 20 trading days |
| `--chaos` | Reject the first order, to exercise the retry path |
| `--close-on-exit` | Flatten at the end of the run |
| `--json run.json` | Save the full state and event log |
| `--symbol-suffix ""` | Emulate a broker with no suffix (Pro/Zero/Raw) |

The dashboard has the same thing on the **Demo account** tab, with a live
equity curve and every broker position/trade.

**Before you put a strategy on a real demo account, validate it:**

```bash
python -m moneyminter validate --strategy trend_pullback --samples 12 --json reports/validation.json
```

Read the verdict, not the score. `REJECTED` on synthetic data is expected and
correct — the simulator has no edge to find. Re-run with `--source yahoo` or a
CSV exported from your broker for a real answer. A strategy earns the demo
account; it does not get handed it.

---

## 1. Prepare MT5

1. Install **MetaTrader 5** (per §0 for your OS) and log into your **Exness demo** account
   (MT5 → File → Login to Trade Account). Note the server name, e.g. `Exness-MT5Trial`.
2. Enable automation: **Tools → Options → Expert Advisors → ✅ Allow algorithmic trading**.
3. Click the **Algo Trading** button in the toolbar so it's green.
4. Open **Market Watch** (Ctrl+M), right-click → *Show All*, so your pairs exist.

Leave the terminal **running and logged in** — the Python API drives it, it is not a
standalone connection.

## 2. Install Money Minter

```powershell
git clone https://github.com/mikeo-ne/moneyminter
cd moneyminter
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install MetaTrader5
```

## 3. Credentials

Money Minter never takes your password as a command-line argument (it would land in your
shell history). Use environment variables — or skip them entirely and let it attach to the
already-logged-in terminal, which is the simplest and safest option.

```powershell
$env:MT5_LOGIN="12345678"
$env:MT5_PASSWORD="your-demo-password"
$env:MT5_SERVER="Exness-MT5Trial"
```

## 4. Test the connection — sends no orders

```powershell
python -m moneyminter connect --symbols EUR/USD GBP/USD --timeframe M5
```

```
DEMO account #12345678 — Your Name
  Server        : Exness-MT5Trial
  Balance       : 10,000.00 USD   Equity: 10,000.00   Free margin: 10,000.00
  Leverage      : 1:2000

Symbols:
  ✓ EUR/USD   -> EURUSDm      price 1.08512      spread    7 pts  min lot 0.01  step 0.01 | example trade 0.5 lots | 600 bars
```

**Check the `->` column.** Exness appends a suffix (`EURUSD` → `EURUSDm` on Standard/Cent
accounts); Money Minter auto-detects it. If it picks the wrong one, force it with
`--symbol-suffix m` (or `--symbol-suffix ""` for Pro/Zero/Raw accounts).

Also sanity-check **example trade lots** — that's the position size your risk settings
imply right now.

## 5. Dry run — full logic, zero orders

```powershell
python -m moneyminter live --symbols EUR/USD --timeframe M5 --strategy ma_crossover --dry-run
```

Runs the real loop against real Exness prices and logs every order it *would* send.
Let this run for a few hours and confirm the signals look sane.

## 6. Go — on demo

```powershell
python -m moneyminter live --symbols EUR/USD GBP/USD --timeframe M5 `
    --strategy trend_pullback --risk 0.0025 --max-positions 2
```

| Flag | Meaning |
|---|---|
| `--risk 0.0025` | Risk 0.25% of equity per trade |
| `--max-positions 2` | Never hold more than 2 at once |
| `--max-daily-loss 0.03` | Stop trading for the day at −3% |
| `--max-drawdown 0.15` | Kill switch at −15% from the peak |
| `--poll 5` | Seconds between checks |
| `--close-on-exit` | Flatten the robot's positions on Ctrl-C |
| `--dry-run` | Log orders instead of sending them |
| `--no-trail` | Do **not** push trailing stops to the broker |
| `--journal run.jsonl` | Append every event to a file |

The robot acts on **closed bars only**, so on `M5` expect a decision every 5 minutes.

### What the robot does to your stops

Every entry carries a stop-loss and take-profit **at the broker**, so a crash or
disconnect cannot leave you naked. While the trade lives, the robot additionally:

1. **Trails the stop** to `trail_atr` ATRs behind the best price seen, and
2. **Moves it to break-even** once the trade is 1R in profit,

re-sending the new stop to the broker with `TRADE_ACTION_SLTP`. The exact same
code runs in the backtester, so the demo and the backtest cannot disagree about
where the stop was.

**Trailing-stop updates are rejected by some brokers during news or when the
terminal loses the symbol.** When that happens the robot logs a `warn` event and
keeps the previous (wider) broker-side stop rather than removing protection.

### Watching the P&L

Stop-outs are executed by the broker, not by Python. After each cycle the runner
**reconciles**: any position it used to own and no longer does is matched to the
broker's closing deal, and the trade is recorded with the broker's own fill price,
P&L and reason (`stop loss` / `take profit`). Without that step a losing robot
looks flat, because most exits are stop-outs.

Check the real number with:

```
equity 9,960.31 (-39.69) | open 0 | trades 7 | avg -0.42R
```

`avg R` is the honest summary: the average result per trade in units of what you
risked. Anything above 0 across a few hundred trades is worth investigating;
anything meaningfully below 0 is a strategy that should not be funded.

## 7. Safety model

Built-in protections, all enforced in code and covered by tests:

- **Demo-only by default.** A live account raises an error unless you pass
  `--i-understand-live-risk`.
- **Live balance cap.** Even then it refuses accounts above `--max-live-balance`
  (default 500) so a first live test is necessarily small.
- **Typed confirmation.** Live runs require typing `TRADE` at a prompt.
- **Magic-number isolation.** It only ever manages positions it opened
  (magic `990707`). Your manual trades are invisible to it.
- **Undersized trades are skipped, not rounded up.** If your risk setting implies
  0.003 lots and the broker minimum is 0.01, taking the minimum would risk ~3× your
  limit — so the trade is skipped with a warning. Seeing this a lot? Your account is
  small for the stop distance: use an **Exness Cent account**, a wider stop, or a
  larger timeframe.
- **Broker-side SL/TP.** Every order carries stop-loss and take-profit *at the broker*,
  so your risk is capped even if your PC dies. Trailing stops are also pushed to the
  broker (`TRADE_ACTION_SLTP`); nothing that protects you lives only in Python.
- **One position per symbol.** A second signal on a pair you already hold is ignored.
- **Correlated exposure is refused.** Long EUR/USD and long GBP/USD is one USD bet —
  the risk manager allows only one per currency bloc.
- **Portfolio risk ceiling.** The sum of open risk cannot exceed 3% of equity.
- **Loss-streak pause.** Four consecutive losses on a symbol sits that symbol out for
  four hours, which stops a mis-fitted strategy from bleeding continuously.
- **No reconciliation gaps.** Trades the broker closed (SL/TP, or you by hand) are
  recorded with the broker's fill price and reason, so the demo P&L is complete.
- **Algo Trading check** before a single order is sent.

# Windows VPS quick start (recommended)

A VPS keeps the bot trading 24/5 with your laptop closed. Full walkthrough:

### 1. Rent and prepare
Any Windows VPS with 2 GB+ RAM works (Contabo, Vultr, Hostinger, Exness sometimes
offers a free VPS to funded accounts). Connect over RDP, then install:

- **MetaTrader 5** (from Exness so the server list is preloaded) — log into your demo
- **Python 3.11+** from python.org — tick **"Add python.exe to PATH"**
- **Git** from git-scm.com

### 2. Install the bot
```powershell
git clone https://github.com/mikeo-ne/moneyminter
cd moneyminter
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install MetaTrader5
```

### 3. Verify, then run
```powershell
python -m moneyminter demo --symbols EURUSD GBPUSD --polls 3000   # no terminal needed
python -m moneyminter connect --symbols EURUSD GBPUSD            # no orders
python -m moneyminter live --symbols EURUSD --dry-run            # no orders
python -m moneyminter live --symbols EURUSD GBPUSD --timeframe M5 --risk 0.0025
```

### 4. Keep it running
Use `deploy\moneyminter.bat` — it restarts the bot automatically if it crashes, and
can be added to the startup folder (`Win+R` → `shell:startup`). See
[deploy/README.md](deploy/README.md).

**Two VPS gotchas that will silently stop your bot:**
- Always leave RDP with **Disconnect**, never "Sign out" — signing out kills MT5.
- Set Power Options to **High performance** with sleep disabled.

### 5. Health check
```powershell
python -m moneyminter live --symbols EURUSD --once
```
Runs one poll, prints equity and recent events, exits. Good for a scheduled heartbeat.

### Resilience built in
The bot expects a VPS to be imperfect. If the terminal link drops it backs off
(5s → up to 60s), reconnects automatically, and resumes — it does **not** treat a
disconnect as a loss. A single unavailable symbol is skipped rather than stopping the
other pairs.

---

# Moving to a live account

Only after **weeks** of profitable demo on the same settings.

Money Minter blocks live trading by default. To enable it you must clear three gates:

```powershell
python -m moneyminter live --symbols EURUSD --timeframe M5 `
    --risk 0.0025 `
    --i-understand-live-risk `
    --max-live-balance 100
```

1. `--i-understand-live-risk` — without it a live account is refused outright.
2. `--max-live-balance` — it still refuses if the balance exceeds this (default **500**).
   Set it just above your actual balance, deliberately.
3. A typed **`TRADE`** confirmation at the prompt (skip with `--yes` only in scripts).

### Sensible first live settings
| Setting | Value | Why |
|---|---|---|
| Deposit | $50–100 | Lose it all and nothing changes in your life |
| `--risk` | `0.0025` (0.25%) | Quarter of the demo default |
| `--max-positions` | `1` | One thing to watch |
| `--max-daily-loss` | `0.02` | Stops for the day at −2% |
| `--max-drawdown` | `0.10` | Hard kill switch at −10% |

Use an **Exness Cent account** for a first live test: lot sizes are 100× smaller, so
risk-based sizing actually fits a small balance instead of being skipped for being
below the minimum lot.

### Expect this to lose money at first
The strategies are built on documented market behaviour (trend persistence, volatility
compression, session structure) and they all decline to trade when that behaviour is
absent. That is a sound *design*, not a proven *edge* — and the simulator cannot prove
anything at all, by construction.

Concretely: on the offline feed everything loses roughly the transaction cost, because
the feed has no exploitable structure. That tells you the cost model and the machinery
work. It does not tell you the strategy works. Only your broker's real prices, across
many samples and out-of-sample folds, can do that:

```powershell
python -m moneyminter validate --source csv --csv data/eurusd_h1.csv `
    --strategy trend_pullback --samples 12
```

Treat your first live months as paying tuition to validate the plumbing, not as income.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No MetaTrader 5 backend available` | Install a backend for your OS — see §0 |
| `Could not reach the MT5 bridge` | Bridge not running, or wrong host/port — see §0b |
| macOS: `MetaTrader 5.app not found` | Install MT5 from metatrader5.com into /Applications |
| Wine: `IPC timeout` | Known Wine flakiness — use the Docker bridge (§0a) or a VPS (§0c) |
| `initialize() failed` | Terminal not running, not logged in, or wrong `--terminal-path` |
| `Algo Trading is disabled` | Toolbar toggle + Tools → Options (§1) |
| `Symbol EUR/USD not found` | Show it in Market Watch, or set `--symbol-suffix` |
| `Order rejected: retcode=10027` | Algo trading disabled in the terminal |
| `retcode=10019` | Insufficient margin — lower `--risk` |
| "below the broker minimum — skipping" | Account too small for that stop; see §7 |
| Nothing happens for ages | Normal — it waits for closed bars *and* a signal. Use `-v` |
| `Lost connection to the MT5 terminal` | MT5 closed or the VPS signed you out (use Disconnect) |
| Bot stopped after a network blip | Shouldn't happen — it auto-reconnects. Report it with `-v` logs |
| `close failed ... position not found` | The broker's SL/TP already closed it — harmless, reconciliation records it |
| `stop update rejected` | Broker refused a trail step (news/symbol unavailable); the previous stop stays in force |
| `position already closed` | Race between your close order and the broker's stop — safe, no double close |
| Strategy never trades on demo | Check `--dry-run` logs; most strategies wait for a regime, and warmup needs history |
| `avg R` deeply negative | The strategy has no edge on these prices. Do not fund it |

Run `python -m moneyminter live -v` for verbose logs.
