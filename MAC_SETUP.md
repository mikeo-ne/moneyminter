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

> **You must re-activate in every new Terminal window.** If you forget, you'll
> see `zsh: command not found: python` — that is not a missing Python, it's an
> inactive venv.
>
> If `python` is still not found *after* activating (some venvs ship `python3`
> only), use either of these permanently:
>
> ```bash
> ln -sf python3 ~/moneyminter/.venv/bin/python     # add the shim, once
> .venv/bin/python -m moneyminter doctor            # or just call it directly
> ```
>
> To rebuild the environment from scratch at any time:
>
> ```bash
> cd ~/moneyminter && bash scripts/bootstrap-venv.sh
> ```

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
python -m moneyminter doctor
```

`doctor` checks every prerequisite in one shot — Python version, the `.app`
location, the drivers, the backend, and an offline trading session. Run it
whenever something looks wrong; it tells you which specific thing to fix.

> **If you see `mt5_mac.bootstrap.BootstrapError: MetaTrader 5.app not found at
> /Applications/MetaTrader 5.app`** — this is the most common macOS failure, and
> it does **not** usually mean MT5 is missing. `mt5_mac` does not check that the
> folder exists; it checks for one specific file inside it:
>
> ```
> /Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine64
> ```
>
> MT5's build-to-build layout shuffles that Wine runtime around, so a correct
> install can fail this check. Confirm for yourself:
>
> ```bash
> ls "/Applications/MetaTrader 5.app/Contents/SharedSupport/"
> find "/Applications/MetaTrader 5.app" -name "wine64" -maxdepth 6
> ```
>
> If the second command prints a path, the runtime is there and **`connect`
> now finds it by itself** — Money Minter locates the Wine binary wherever the
> bundle keeps it and points the bridge at it.
>
> If it prints nothing but `SharedSupport/` contains `wine`, then that `wine`
> directory is a **launcher script, not a Wine runtime**, and no Python driver
> can use it. That is a packaging problem in the MT5 build, not something
> Python can work around. Your options, in order of reliability:
>
> | Option | Effort | Notes |
> |---|---|---|
> | **Windows VPS** (~$10/mo) | low | MT5 runs natively, no Wine at all. The recommended way to run 24/5. |
> | **`siliconmetatrader5`** | medium | Docker runtime that speaks the MT5 API over a local socket; `pip install siliconmetatrader5`, then `connect --mt5-host 127.0.0.1 --mt5-port 8001` |
> | Reinstall MT5 | low | Sometimes restores the Wine runtime; `brew install --cask metatrader5` may already be installed as your `MetaTrader 5.app` |
> | Buy MT5's VPS from the app | paid | The "Specialized VPS for MetaTrader" banner inside MT5 |
>
> **Important:** all of these keep the same commands and the same code — only
> the connection changes. `--mt5-host` is all that differs.

> **Did you clone before the macOS fixes?** The four lines below are the whole
> difference between the old behaviour and the working one — if your copy is
> older, update it first:
>
> ```bash
> cd ~/moneyminter && git pull
> ```

> Also: `mt5_mac` hard-codes the bundle *name* `/Applications/MetaTrader 5.app`,
> while broker-branded installers write `MetaTrader 5 (Exness).app`. Money
> Minter searches both `/Applications` and `~/Applications` for any
> `*MetaTrader*.app`, so a branded install normally just works.

Find the real name:

```bash
ls /Applications | grep -i meta
```

Then either tell Money Minter where it is (recommended):

```bash
export MT5_APP="/Applications/MetaTrader 5 (Exness).app"
```

…or make a link with the name the bridge insists on:

```bash
ln -s "/Applications/MetaTrader 5 (Exness).app" "/Applications/MetaTrader 5.app"
```

Money Minter now searches `/Applications` and `~/Applications` for any
`*MetaTrader*.app` bundle by itself, so a branded install usually just works —
`MT5_APP` is the override for anything unusual (external drive, renamed bundle).

> On first use `mt5_mac` downloads a small (~8 MB) Windows Python into MT5's Wine
> environment, so give it a minute. It only happens once.

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

## Using a virtual machine on your Mac

**Check one thing before anything else: is your Mac Apple Silicon (M1/M2/M3/M4)
or Intel?**

```bash
sysctl -n machdep.cpu.brand_string    # on the Mac itself
```

That prints either `Apple M2` (Apple Silicon) or something like `Intel(R) Core(TM)
i7-9750H` (Intel).

> **`uname -m` is not the test.** Run it *inside* a VM and it reports the
> **guest's** architecture, not your Mac's. An Apple Silicon Mac running an ARM
> Kali VM says `aarch64`; an Intel Mac running x86 Windows says `x86_64`. Neither
> tells you what you need to know. Use `sysctl` on the host.

### If it says Intel — a Windows VM works

Full walkthrough, including the ISO, VirtualBox settings and the RPyC bridge:
**[WINDOWS_VM.md](WINDOWS_VM.md)**. The short version:

VirtualBox runs x86 Windows fine on an Intel Mac. Make sure the guest is
**64-bit Windows**, because `MetaTrader5` publishes `win_amd64` wheels only.

```bash
# inside the Windows VM, in PowerShell
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install MetaTrader5          # must succeed — it cannot on ARM Windows
```

Then install MT5 there and follow Steps 1–4 inside the VM. This is a genuine
Windows environment with no Wine layer, so it behaves exactly like the VPS route.

### If it says `arm64` (Apple Silicon) — it will not work

VirtualBox on Apple Silicon cannot run x86 Windows guests; it only virtualises
ARM64 systems, and its Windows-on-ARM support is experimental at best. That
matters because of one hard fact:

> **`MetaTrader5` publishes `win32` and `win_amd64` wheels only — never
> `win_arm64`.**

Verified against the current release (5.0.6231): every wheel in every published
version is `win32` or `win_amd64`. So inside an ARM Windows VM, MT5 itself would
install and run, `pip install MetaTrader5` would fail, and no driver could ever
attach to the terminal. `python -m moneyminter doctor` detects this and says so.

Two related traps:

- Running the Intel VirtualBox build through Rosetta 2 does **not** help.
  Rosetta translates a Mac application; it does not turn an ARM guest into an x86
  one, so the Windows Python inside stays ARM and the wheel still won't install.
- `mt5_mac` (the macOS bridge) has the same problem from a different angle: it
  runs the Windows Python from MT5.app's bundled Wine, which is x86. On an Apple
  Silicon Mac that runtime is either absent (your case) or emulated through
  Rosetta, and it is why the bridge is so fragile there.

### The comparison that matters

| Route | Works on your Mac? | Why |
|---|---|---|
| **Windows VPS** (yours or a friend's) | ✅ | x86-64 Windows, native, always on |
| Windows VM on an **Intel** Mac | ✅ | x86-64 guest, native |
| **UTM / QEMU with x86 emulation** | ⚠️ | Can run an x86 guest, but full-system emulation is roughly 5–20× slower. Fine for a smoke test, poor for 24/5 |
| **VirtualBox** on Apple Silicon | ❌ | No x86 guests; ARM Windows only, and then no `MetaTrader5` wheel |
| **Parallels** on Apple Silicon | ❌ | Same architecture wall — it virtualises ARM, and emulation doesn't reach the CPython API |
| `mt5_mac` on this Mac | ❌ | Your bundle has no Wine runtime (Step 5) |

If you have an Apple Silicon Mac and no PC, the honest answer is a **Windows
VPS** — roughly $10/month, and it removes every failure mode in this document at
once.

## Using your friend's VPS

The cheapest route, and it works — the VPS provider never knows or cares whose
account runs inside MetaTrader. A few things to get right:

### 1. Your friend has full control of that machine

This is not a technicality. Whoever holds the VPS control panel (the provider
login) can:

- reset the Windows password and log in to the desktop while your bot runs
- read the `run.jsonl` journal and every log you write
- install a keylogger or a screen recorder and capture your **Exness demo login**
- snapshot the disk, and read it later at leisure

**Never put a real-money account on a machine you do not control.** Demo only —
which is exactly what you're doing, so the worst case here is embarrassing rather
than expensive.

Practical rules:

| Do | Why |
|---|---|
| Use a **demo** account, never live | Your friend can read the credentials |
| Create a **separate Exness demo** just for the VPS | Don't reuse a login you care about |
| Change the VPS admin password the moment you take it over | Or ask your friend to set a fresh one and not keep it |
| Don't store broker email/2FA on that machine | It's simply readable |

### 2. Set a dashboard password — it is automatic now

The dashboard has no login of its own, so anyone who can reach port 8000 can
stop your bot with one request. Money Minter now **requires a password whenever
it binds to `0.0.0.0`**, and prints one if you don't supply it:

```bash
MM_DASH_PASSWORD='pick-something-long' \
  python -m moneyminter dashboard --host 0.0.0.0 --port 8000
```

or explicitly:

```bash
python -m moneyminter dashboard --host 0.0.0.0 --port 8000 \
    --user me --password 'pick-something-long'
```

Better still, don't expose it at all — tunnel over SSH from your machine:

```bash
ssh -L 8000:127.0.0.1:8000 yourfriend@vps-ip
# then open http://127.0.0.1:8000 locally — nothing is exposed to the internet
```

### 3. Don't use your friend's MT5 if it's already configured

Two MT5 terminals in one Wine/Windows profile fight over the same data directory.
Install a second one, or make sure the terminal is logged into **your** demo
account, not theirs. If they already have MT5 running, ask them to close it.

### 4. Make sure it actually suits the job

| Requirement | Why it matters |
|---|---|
| **Windows Server** (not Linux) | MT5's Python API is Windows-only; a Linux VPS needs the Wine path you just escaped |
| Always on, no sleep | A laptop-style VM that suspends stops trailing stops |
| ~2 GB RAM, 2 cores | MT5 plus Python is comfortable; 1 GB is tight |
| Low latency to your broker | Fine for `trend_pullback` on M5; matters for scalping |

Exness servers are in London and New York. A VPS in those regions gets ~5 ms,
which is plenty. A cheap European box is a few tens of milliseconds — irrelevant
for this strategy.

### 5. Sharing the cost

Less a concern than the security one, but be explicit about who pays and who
owns the account: if the provider login is in your friend's name, **they own the
machine**, and one day it may vanish on short notice. For a four-week demo test
that's fine. Don't build something you depend on and leave it there indefinitely.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No MetaTrader 5 backend available` | Run `pip install mt5_mac` inside the activated venv, then retry |
| `BootstrapError: MetaTrader 5.app not found` | Usually **not** actually missing — see Step 5. `git pull` for the fix that locates the Wine binary wherever the bundle keeps it |
| `error: argument cmd: invalid choice: 'doctor'` | Your copy predates the command. `cd ~/moneyminter && git pull` |
| `doctor` works but your edit doesn't seem to apply | You are running an installed copy in `site-packages`; check the `Code in use` line in `doctor` |
| `zsh: command not found: python` | Venv not activated. `source .venv/bin/activate`, or run `.venv/bin/python -m moneyminter ...` |
| `✗ wine64` missing / only `SharedSupport/wine` exists | No usable Wine runtime in the bundle — see the table in Step 5 |
| `pip install MetaTrader5` fails in a Windows VM | The VM is ARM Windows. `MetaTrader5` ships `win_amd64` only — use an x86-64 VM or a VPS |
| `doctor` says "This is ARM Windows" | Same cause; `python -m moneyminter doctor` explains it |
| `setup gone` in the log | Normal. Price ran past the stop before the order went out, so the trade was skipped |
| `Order rejected: retcode=10019` | Not enough free margin for that lot size — lower `--risk` |
| `No module named mt5_mac` | `pip install mt5_mac` in the activated venv |
| Lost / can't tell what's wrong | `python -m moneyminter doctor` |
| Dashboard asks for a password | Expected on `--host 0.0.0.0`. Use `MM_DASH_PASSWORD`, or `--no-auth` on loopback only |
| `401 Authentication required` from curl | Pass `-u user:password` |
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

python -m moneyminter doctor                      # diagnose setup, run this when stuck
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
