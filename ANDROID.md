# Running Money Minter from an Android phone

**Short answer:** you cannot run the trading robot on Android. You *can* run it
on a machine (VPS, PC, Mac) and use your phone as the control panel and the
place you watch it from. That is the normal, working setup — and it is better
than what you were trying to do on the Mac.

---

## Why the robot can't run on the phone itself

MetaTrader 5 on Android has **no programming API at all**. MetaQuotes have never
shipped one, and the Android app cannot run Expert Advisors, scripts, or a Python
runtime with access to the terminal. There is no `MetaTrader5` package for
Android and no bridge to build against.

| What | Android | Windows | macOS | Linux |
|---|---|---|---|---|
| MT5 app, manual trading | ✅ | ✅ | ✅ | via Wine |
| **Python API (`MetaTrader5`)** | ❌ | ✅ native | ⚠️ `mt5_mac`, often broken | ⚠️ Wine / bridge |
| Expert Advisors (.ex5) | ❌ | ✅ | ✅ | via Wine |
| **Money Minter robot** | ❌ | ✅ | ⚠️ | ✅ (with a bridge) |
| **Money Minter dashboard** | ✅ **browser** | ✅ | ✅ | ✅ |

So: the phone is the *window*, not the engine.

---

## The setup that actually works

```
   ┌──────────────────┐        ┌───────────────────────┐        ┌──────────────┐
   │  Android phone   │  LAN   │  Windows VPS / PC     │  API   │  Exness demo │
   │                  │ ─────► │  MT5 + Money Minter   │ ─────► │  (broker)    │
   │  Chrome:         │        │  dashboard :8000      │        │              │
   │  dashboard       │        │  live trading loop    │        │              │
   │  MT5 app (view)  │        └───────────────────────┘        └──────────────┘
   └──────────────────┘
```

Your phone's **MT5 app shows the same account**, so you can watch positions and
P&L natively, and take over manually if you ever need to.

### Steps

**1. Get the robot running somewhere that is always on.** A Windows VPS is the
recommended option (~$10/month) because MT5 runs natively there with no Wine.
Follow [MAC_SETUP.md](MAC_SETUP.md) Steps 1–4 on that machine, or
[LIVE_TRADING.md](LIVE_TRADING.md) §0c for the VPS specifics.

**2. Start the dashboard bound to all interfaces** — this is what makes it
reachable from the phone:

```bash
python -m moneyminter dashboard --host 0.0.0.0 --port 8000
```

`0.0.0.0` means "listen on every network interface". `127.0.0.1` would only be
reachable from the machine itself — the single most common reason a phone can't
see it.

**3. Open it on the phone.** In Chrome, same Wi-Fi network:

```
http://<the-machine's-LAN-IP>:8000
```

Find the IP with `ipconfig` (Windows) or `ip addr` (Linux). On a VPS, use the
VPS's public IP and allow TCP 8000 in its firewall.

**4. Check the phone layout.** The dashboard has a responsive layout for screens
under 640px: KPI tiles go two-up, controls stack, wide tables scroll sideways
instead of clipping, and it respects the notch/safe-area insets.

### What you can do from the phone

- Watch equity, open positions, trades and P&L live (WebSocket, no refresh)
- Start and stop the paper/demo session
- Close an individual position
- Run backtests and read validation verdicts
- See every guardrail event — blocked trades, skips, loss streaks

### What you *cannot* do

- You can't place a trade when the laptop/VPS is off; nothing runs locally
- If the phone loses Wi-Fi the bot keeps trading — it does not need the phone
- Don't expose port 8000 to the open internet without a password. The dashboard
  has **no authentication**; it is meant for a trusted LAN or an SSH tunnel.

---

## Two other ways to use the phone

### A. You have no PC at all

Rent a Windows VPS, install MT5 + Money Minter there, and control it entirely
from the phone browser. This is the only path to a working demo account with no
computer at home. Cost is roughly $8–12/month.

### B. Just watch your Exness demo

Install the **Exness MT5 app** from the Play Store, log into the same demo
account, and watch it. It shows positions, P&L and history identically. It is a
fine monitoring tool — it just cannot run the robot.

---

## Common mistakes

| Symptom | Cause | Fix |
|---|---|---|
| Phone can't load the page | Dashboard bound to `127.0.0.1` | Start it with `--host 0.0.0.0` |
| Loads on the machine, not the phone | Different Wi-Fi network, or AP isolation enabled on the router | Same network; switch off "client isolation" |
| Times out on a VPS | Firewall blocks 8000 | Allow TCP 8000 in the VPS provider's firewall *and* the OS firewall |
| Page loads but numbers never update | WebSocket blocked by a proxy | Use `http://`, not `https://`, unless you've configured TLS |
| Can't install `MetaTrader5` on the phone | There is no such package for Android | Run the robot on the VPS instead |

---

## Recommendation

If your Mac's MT5 has no Wine runtime (see [MAC_SETUP.md](MAC_SETUP.md) Step 5),
**a Windows VPS solves both problems at once**: the robot runs natively 24/5, and
the phone becomes a genuinely good dashboard because the thing it talks to never
sleeps.
