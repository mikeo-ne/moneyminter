# Connecting Money Minter to your Exness account

This guide takes you from a working MT5 terminal to the robot placing orders on
your **Exness demo account**.

> **Read this first.** Money Minter refuses to touch a real-money account unless you
> pass an explicit override flag. That default is deliberate — keep it until the robot
> has proven itself on demo for weeks. Nothing here is financial advice, and the
> backtest numbers in the README come from a *simulator*, not real market data.

---

## 0. The one hard requirement

The `MetaTrader5` Python package **only runs on Windows**. Your options:

| Setup | Works? |
|---|---|
| Windows PC/laptop | ✅ Easiest |
| Windows VPS (~$10/mo, keeps the bot running 24/5) | ✅ Best for real use |
| Linux/macOS + MT5 under Wine + Windows Python | ⚠️ Possible, fiddly |
| Linux/macOS natively | ❌ Not supported by MetaQuotes |

If you're not on Windows, you can still use every other part of the robot
(`backtest`, `optimize`, `walkforward`, `trade`, `dashboard`) — they run anywhere.

## 1. Prepare MT5

1. Install **MetaTrader 5** and log into your **Exness demo** account
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

## 8. Going to real money (later)

Only after weeks of profitable demo:

```powershell
python -m moneyminter live --symbols EUR/USD --timeframe M5 --risk 0.0025 `
    --i-understand-live-risk --max-live-balance 100
```

Fund with an amount you are fully prepared to lose. Scale up only after months of
consistent results, never after a good week.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `MetaTrader5 package is required... Windows` | You're not on Windows — see §0 |
| `initialize() failed` | Terminal not running, not logged in, or wrong `--terminal-path` |
| `Algo Trading is disabled` | Toolbar toggle + Tools → Options (§1) |
| `Symbol EUR/USD not found` | Show it in Market Watch, or set `--symbol-suffix` |
| `Order rejected: retcode=10027` | Algo trading disabled in the terminal |
| `retcode=10019` | Insufficient margin — lower `--risk` |
| "below the broker minimum — skipping" | Account too small for that stop; see §7 |
| Nothing happens for ages | Normal — it waits for closed bars *and* a signal. Use `-v` |

Run `python -m moneyminter live -v` for verbose logs.
