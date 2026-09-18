# Connecting Money Minter to your Exness account

This guide takes you from a working MT5 terminal to the robot placing orders on
your **Exness demo account**.

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
    --strategy ma_crossover --risk 0.005 --max-positions 2
```

| Flag | Meaning |
|---|---|
| `--risk 0.005` | Risk 0.5% of equity per trade |
| `--max-positions 2` | Never hold more than 2 at once |
| `--max-daily-loss 0.03` | Stop trading for the day at −3% |
| `--max-drawdown 0.15` | Kill switch at −15% from the peak |
| `--poll 5` | Seconds between checks |
| `--close-on-exit` | Flatten the robot's positions on Ctrl-C |
| `--dry-run` | Log orders instead of sending them |

The robot acts on **closed bars only**, so on `M5` expect a decision every 5 minutes.

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
  so your risk is capped even if your PC dies.
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
git clone -b arena/01a0b105-moneyminter https://github.com/mikeo-ne/moneyminter
cd moneyminter
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install MetaTrader5
```

### 3. Verify, then run
```powershell
python -m moneyminter connect --symbols EURUSD GBPUSD   # no orders
python -m moneyminter live --symbols EURUSD --dry-run   # no orders
python -m moneyminter live --symbols EURUSD GBPUSD --timeframe M5 --risk 0.005
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
The strategies here are textbook (EMA cross, Donchian breakout, RSI). They are a working
*framework*, not a proven edge. The README's backtest figures come from a **simulator**.
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

Run `python -m moneyminter live -v` for verbose logs.
