# Money Minter on macOS — step by step

A complete walkthrough from a fresh Mac to a robot trading an **Exness demo
account**. Follow it in order; each step tells you what you should see. If a
step doesn't match, stop there — the troubleshooting table at the bottom maps
symptoms to fixes.

**Total time:** about 25 minutes, most of it downloads.

---

## Step 0 — Understand what can and can't happen

Before you type anything, three facts:

1. **This trades a demo account.** Money Minter refuses real-money accounts
   unless you explicitly pass `--i-understand-live-risk`. That's a code-level
   guard, not a suggestion.
2. **A demo account is not real money, but the prices are real.** Your P&L will
   be an honest test of the plumbing and the strategy — the one thing it can't
   tell you is slippage/latency on a live server.
3. **Nothing in this repo guarantees profit.** The bundled strategies are
   well-designed, not proven. You will most likely lose a little money on demo at
   first. That is the normal, expected outcome and it is what the testing is for.

You will need:
- macOS (Apple Silicon or Intel)
- ~2 GB of free disk
- An Exness demo account ([signup](https://www.exness.com)) — takes 2 minutes,
  no deposit needed

---

## Step 1 — Install MetaTrader 5 for Mac

Download MT5 from the official site — **not** the Mac App Store:

<https://www.metatrader5.com/en/download>

Open the `.dmg`, drag **MetaTrader 5** to `/Applications`, and launch it.

> **Important:** MT5 for Mac is the Windows build running inside a bundled Wine
> wrapper. That's normal and expected — it's why the next step works.

**Check:** MetaTrader 5 opens and shows a login window.

### Apple Silicon note

If macOS blocks it with *"MetaTrader 5 cannot be opened because the developer
cannot be verified"*, go to **System Settings → Privacy & Security**, scroll
down, and click **Open Anyway**. Then launch it again.

---

## Step 2 — Create and log into your Exness demo

1. In MT5: **File → Open an Account**
2. Choose **Exness (MT5)** → **Open a demo account**
3. Fill in the form. Settings that matter:

| Field | Use this | Why |
|---|---|---|
| Deposit | `10000` USD | Matches the examples below |
| Leverage | `1:500` | Enough for 0.01 lots to be tradable |
| Account type | **Standard** | Cent accounts also work, later |

4. Note the **server name** shown after login (e.g. `Exness-MT5Trial`,
   `Exness-MT5Trial8`). You'll rarely need to type it, but note it anyway.

**Check:** the bottom-right of MT5 shows your account number, `USD`, balance
`10,000.00`, and a green/blue connection indicator.

> A newly created demo often sits in a **login dialog inside the Wine window** —
> the small window with Login / Password / Server fields. Don't close it; fill
> in the password you set at signup (Exness emails it to you) and click
> **OK**. MT5 is not usable until that window is dismissed. If you lost the
> password, create a fresh demo from **File → Open an Account**.

### Turn on algorithmic trading

Go to **Tools → Options → Experts** and make sure these are set as follows:

| Setting | State | Why |
|---|---|---|
| **Allow algorithmic trading** | ✅ **ticked** | Without this nothing can place an order |
| Disable algorithmic trading when the account has been changed | ⬜ unticked | Otherwise a reconnect silently disables the robot |
| Disable ... when the profile has been changed | ⬜ unticked | Same |
| Disable ... when the charts symbol or period has been changed | ⬜ unticked | Same |
| **Disable algorithmic trading via external Python API** | ⬜ **unticked** | ⚠️ **Critical** — this one breaks Python specifically |
| Allow DLL imports | ⬜ not needed | Money Minter doesn't use DLLs |

That fifth row is the one people miss. Ticked, it kills the Python connection
even though the toolbar button is green.

Then click the **Algo Trading** button in the top toolbar so it turns **green**.

**Check:** the Algo Trading toolbar button is green, not grey/red.

### Make sure your pairs exist — this one catches almost everybody

A brand-new Exness demo opens with only **crypto** in Market Watch. Yours shows
`BTCUSD`, `ETHUSD`, `XRPUSD`, `SOLUSD` and so on, and **no FX pairs at all**.
Leave it like that and the first real command fails with `Symbol EUR/USD not
found on this account`.

Open **Market Watch** (`Ctrl+M`), right-click anywhere inside the symbol list,
and choose **Show All**.

**Check:** `EURUSD`, `GBPUSD`, `USDJPY` now appear in the list.

> **Two traps here.** First, right-clicking the *list* is what matters — the
> "Show All" item is not in the top menu bar. Second, note the exact spelling:
> Exness usually appends a lowercase `m` (`EURUSDm`). Money Minter detects that
> automatically, but the case matters to MetaTrader, so don't "fix" it by
> hand.

> **Note the exact names.** Exness usually appends an `m` (`EURUSDm`). Money
> Minter auto-detects this — you don't need to do anything, but it's what you'll
> see in the output later, so don't be surprised.

---

## Step 3 — Install the tooling

You need **Python 3.10 or newer** and **Git**. Both come with the Xcode command
line tools, and Python is easiest from Homebrew.

### 3a. Check what you already have

Open **Terminal** (⌘+Space, type `terminal`) and run:

```bash
python3 --version
git --version
```

- If Python prints `3.10` or higher → skip to Step 3c.
- If Git prints a version → good.
- If Git says *"command line tools required"*, click **Install** in the dialog
  and wait for it — it takes a few minutes.

### 3b. Install Homebrew + Python (if needed)

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

At the end, Homebrew prints two commands to add it to your PATH — **run them**.
They look like this on Apple Silicon:

```bash
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```

Then:

```bash
brew install python@3.12
```

**Check:** `python3 --version` prints `Python 3.12.x`.

### 3c. Get the code and install dependencies

```bash
cd ~
git clone https://github.com/mikeo-ne/moneyminter
cd moneyminter
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

**Check:** your prompt is prefixed with `(.venv)`.

> Every time you open a **new** Terminal window you must re-activate:
> `cd ~/moneyminter && source .venv/bin/activate`

---

## Step 4 — Test everything that does *not* need a broker

Do this before touching MetaTrader. It takes 30 seconds and it catches most
setup problems.

### 4a. The test suite

```bash
python -m pytest tests -q
```

**Expect:** `119 passed`. If anything fails, your Python version is probably too
old — check Step 3a.

### 4b. A simulated demo account (the important one)

```bash
python -m moneyminter demo --symbols EUR/USD --timeframe M5 --polls 2000
```

This runs the **real** live trading code — order placement, broker-side stops,
trailing stops, reconciliation — against a fake terminal. It needs no broker and
takes about 20 seconds.

**Expect:** something like

```
Simulated DEMO account #78234001 — MoneyMinter-Sim
  Balance 10,000.00 USD | leverage 1:500
  OK EUR/USD   -> EURUSDm    1.07527    spread   6pts min lot 0.01 | size 0.24 lots

  Start equity     : 10,000.00
  Final equity     : 9,937.11  (-0.63%)
  Trades closed    : 4 (1W / 3L, win 25.0%)
  Average R        : -0.41R
```

The exact numbers vary per run. What matters: it **opened trades**, **closed
trades**, and **exited cleanly**. The P&L being slightly negative is correct and
expected — the simulated price feed has no exploitable edge by design.

**If this step fails, stop.** Everything after it is harder to debug.

---

## Step 5 — Connect the Mac to MetaTrader

```bash
pip install mt5_mac
```

> `mt5_mac` drives the Wine-based Python inside `MetaTrader 5.app`. On first use
> it downloads a small (~8 MB) Windows Python into MT5's Wine prefix, so give it
> a minute.

**MetaTrader 5 must be running and logged in** — this talks to it, it is not a
standalone connection.

Now check the connection:

```bash
python -m moneyminter connect --symbols EURUSD GBPUSD
```

**Expect:**

```
DEMO account #12345678 — Your Name
  Server        : Exness-MT5Trial8
  Balance       : 10,000.00 USD   Equity: 10,000.00   Free margin: 10,000.00
  Leverage      : 1:500

Symbols:
  ✓ EUR/USD   -> EURUSDm      price 1.08512      spread    7 pts  min lot 0.01  step 0.01  | example trade 0.12 lots | 600 bars
  ✓ GBP/USD   -> GBPUSDm      price 1.27188      spread   11 pts  min lot 0.01  step 0.01  | example trade 0.12 lots | 600 bars
```

Three things to verify:

| Column | What to check |
|---|---|
| the tick `✓` | Both symbols must be `✓`, not `!`. If `!`, redo Step 2's Market Watch. |
| `-> EURUSDm` | The auto-detected broker symbol. Should end in `m` on Exness. |
| `example trade 0.12 lots` | Your live position size at 0.5% risk. **If it shows `0.0`**, your account is too small for that stop — see "Zero lot size" below. |

If the suffix looks wrong:

```bash
python -m moneyminter connect --symbols EURUSD --symbol-suffix ""
```

---

## Step 6 — Dry run (no orders sent)

Let it think for a while with the order-sending disabled:

```bash
python -m moneyminter live --symbols EURUSD GBPUSD --timeframe M5 \
    --strategy trend_pullback --dry-run -v
```

**Expect:** a line every poll:

```
[14:32:05] equity 10,000.00 (+0.00) | open 0 | trades 0 | avg +0.00R
```

and, eventually, events like:

```
          Opened EUR/USD BUY @ 1.08512 SL 1.08398 TP 1.09313 — uptrend pullback
```

Because `--dry-run` is on, **the terminal receives nothing** — no order exists.
The log is showing what it *would* have done.

Let this run for **2–3 hours**. You are looking for:
- signals that appear at sensible times (not every bar — most bars should be silent)
- stop/target levels that bracket the entry sensibly
- no repeated `error` lines

Press `Ctrl+C` to stop.

> **Why so long?** On M5 with a regime-gated strategy, a given pair may only
> produce 1–3 signals per day. Patience here saves you from debugging a real
> order later.

---

## Step 7 — Trade the demo account

```bash
python -m moneyminter live --symbols EURUSD GBPUSD --timeframe M5 \
    --strategy trend_pullback --risk 0.0025 --max-positions 2 \
    --journal run.jsonl -v
```

Note it prints `ARMED` rather than `DRY RUN`, and — because this is a demo
account — no confirmation prompt appears. (A live account would demand you type
`TRADE`.)

Leave it running. **Do not close the lid.** See Step 8.

Watch for:

```
[14:32:05] equity 10,000.00 (+0.00) | open 1 | trades 0 | avg +0.00R
          Opened EURUSDm BUY 0.12 lots @ 1.08512 SL 1.08398 TP 1.09313
[15:47:20] equity 9,975.30 (-24.70) | open 0 | trades 1 | avg -1.00R
          Position 123456789 closed by broker: stop loss (P&L -24.70)
```

That second message is the important one: the **broker** closed the trade, and
the robot reconciled it into the P&L with the real fill price. Most exits are
stop-outs, so if you didn't see this, your results would look falsely flat.

Cross-check in the MT5 window: **View → Toolbox → Trade** should show the same
positions, with a stop-loss attached.

Press `Ctrl+C` to stop. Positions stay open unless you add `--close-on-exit`.

---

## Step 8 — Keep it running (this is the bit people get wrong)

A laptop that sleeps stops trading mid-trade. Your stop-loss is at the broker, so
you are still protected — but the robot won't trail it.

For short sessions, prevent sleep:

```bash
caffeinate -i python -m moneyminter live --symbols EURUSD GBPUSD \
    --timeframe M5 --strategy trend_pullback --risk 0.0025
```

For anything longer, use a **Windows VPS** (~$10/month). That's the recommended
way to run 24/5, and it avoids the macOS Wine layer entirely. See
[LIVE_TRADING.md](LIVE_TRADING.md) §0c and the Windows VPS section.

---

## Step 9 — Judge the results honestly

After a week, look at the actual numbers:

```bash
python -m moneyminter validate --source csv --csv data/eurusd_h1.csv \
    --strategy trend_pullback --samples 12
```

Better: export real candles from MT5 (**File → Save As** in a chart, or
right-click a chart → **Save As**), then point `--csv` at it.

Read the **verdict**, not the score:

| Verdict | Meaning |
|---|---|
| `REJECTED` | No edge on these prices. Do not increase risk. |
| `PROMISING` | Inconclusive. Needs more samples or a different regime. |
| `VALIDATED` | Held up across samples, folds, costs and Monte Carlo. |

And the single most useful number on the demo run is **`avg R`** — your average
result per trade in units of what you risked. Above 0 over a few hundred trades
is worth investigating. Deeply below 0 means the strategy should not be funded,
regardless of how good the equity curve looks.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No MetaTrader 5 backend available` | Run `pip install mt5_mac` inside the activated venv, then retry |
| `MetaTrader 5.app not found` | MT5 must be in `/Applications`, installed from metatrader5.com |
| `initialize() failed` | MT5 not running, or not logged in. Launch it manually first |
| `Algo Trading is disabled` | Toolbar button green **and** Tools → Options → Expert Advisors ticked |
| `Symbol EURUSD not found` | Market Watch (`Ctrl+M`) → right-click → **Show All** |
| `Could not reach the MT5 bridge` | You passed `--mt5-host`. On Mac, drop that flag and use `mt5_mac` |
| `Retcode ... 10027` | Algo Trading is off in the terminal |
| `Retcode ... 10019` | Insufficient margin — lower `--risk` |
| **Zero lot size / "below broker minimum"** | Your account is too small for that stop distance. Use `--risk 0.01`, or a longer timeframe, or an Exness **Cent** account (100× smaller lots) |
| Nothing happens for a long time | Normal. Most bars produce no signal, and `M5` means a decision every 5 minutes |
| Bot says `stopped` immediately | Re-activate the venv: `cd ~/moneyminter && source .venv/bin/activate` |
| `attempting reconnect` messages | Expected on flaky links; it backs off 5s → 60s and recovers by itself |
| `stop update rejected` | Broker refused a trail step during news. The previous stop stays in force — this is safe |
| Bars look 1–2 hours off | Handled automatically (broker-clock correction), but check `-v` logs for a `broker clock` warning |
| Permission blocked on first `mt5_mac` run | System Settings → Privacy & Security → allow, then retry |

---

## Cheat sheet

```bash
cd ~/moneyminter && source .venv/bin/activate     # every new Terminal window

python -m moneyminter strategies                  # what's available
python -m moneyminter demo --symbols EUR/USD      # no broker needed — run this first
python -m moneyminter connect --symbols EURUSD    # verify the MT5 link, no orders
python -m moneyminter live --symbols EURUSD --dry-run              # think, don't trade
python -m moneyminter live --symbols EURUSD --risk 0.0025          # trade the demo
python -m moneyminter dashboard --port 8000       # browser UI at localhost:8000
python -m moneyminter validate --strategy trend_pullback --samples 12
```

### Safety defaults you should not change at first

| Setting | Value | Why |
|---|---|---|
| `--risk` | `0.0025` | 0.25% per trade. 40 consecutive losses = −10%. |
| `--max-positions` | `2` | Fewer things to watch go wrong |
| `--max-daily-loss` | `0.03` | Stops for the day at −3% |
| `--max-drawdown` | `0.15` | Hard kill switch at −15% from the peak |

Run on demo for **at least 4 weeks** on unchanged settings before you even think
about `--i-understand-live-risk`. If the demo can't hold up, live won't either.
