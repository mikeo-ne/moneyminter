# Windows VM on an Intel Mac — the full walkthrough

Your Mac is **Intel**, so VirtualBox can run a real x86-64 Windows guest. That
gives you the one thing this project needs and has not had: a genuine Windows
machine for MetaTrader 5, with no Wine and no `mt5_mac` in the path.

There are two ways to finish. **Route A is simpler** (everything in the VM).
**Route B is what you were already reaching for** (Linux runs the robot, Windows
only executes).

```
Route A:  [ Windows VM: MT5 + robot ]  ──► Exness demo

Route B:  [ Kali VM: robot ] ── RPyC ──► [ Windows VM: MT5 + bridge ] ──► Exness demo
```

Start with Route A: it removes the most moving parts. Move to B later if you
want the robot running in Linux.

---

## Step 0 — Confirm the chip properly

On the **Mac**, in a normal Terminal (not the VM):

```bash
sysctl -n machdep.cpu.brand_string
```

You should see something like `Intel(R) Core(TM) i7-9750H`. If it says `Apple M1`
or similar, stop — [VirtualBox cannot run x86 Windows there](MAC_SETUP.md), and
Route B on a Windows VPS is your path instead.

---

## Step 1 — Get a Windows ISO

Microsoft's own download page — **"Windows 11 Disk Image (ISO) for x64 devices"**:

<https://www.microsoft.com/software-download/windows11>

Any of these work; pick what your Mac can spare:

| Edition | RAM | Disk |
|---|---|---|
| Windows 10 Pro | 4 GB | 40 GB |
| Windows 11 Pro | 4 GB | 64 GB |

> Windows 11 needs TPM 2.0 and Secure Boot. VirtualBox 7 supports both, but let
> the VM do a **TPM bypass** only if installing Win 11 is fighting you — Windows
> **10** avoids the whole topic and is fine for this.

You do **not** need a licence key to run Windows unactivated for evaluation. It
nags and personalises less, and it does not stop MT5 or Python.

---

## Step 2 — Install VirtualBox (Intel build)

Download the **macOS Intel** package — *not* the `macOSArm64` one:

<https://www.virtualbox.org/wiki/Downloads>

While you are there, also grab the **Extension Pack** (same version). Then:

1. Open the `.dmg`, run the installer.
2. macOS will block the kernel extension: **System Settings → Privacy &
   Security → Allow** next to "Oracle". Reboot.
3. Double-click the Extension Pack to register it.

---

## Step 3 — Create the VM

In VirtualBox: **New**

| Field | Value |
|---|---|
| Name | `MT5` |
| ISO Image | your Windows ISO |
| Type / Version | Microsoft Windows / Windows 10 (64-bit) or 11 |
| Memory | **4096 MB** (minimum 2048) |
| Processors | **2** |
| Disk | **40 GB** (VDI, dynamically allocated) |

Then **Settings** on the new VM before booting:

- **System → Enable EFI**: on for Windows 11, doesn't matter for 10
- **Display → Video Memory**: 128 MB, **Enable 3D Acceleration**: on
- **Network → Adapter 1 → Attached to**: **NAT** (default is fine)

Boot, install Windows, create an account, let it finish.

> **Install Guest Additions** after first login: menu **Devices → Insert Guest
> Additions CD**, then run `VBoxWindowsAdditions.exe` from the CD drive. This
> gives you shared folders, clipboard, and a resizeable window. It is optional
> but you will want it.

---

## Step 4 — Install MetaTrader 5 in the VM

Inside Windows, open Edge and download MT5:

<https://www.metatrader5.com/en/download>

Install it, launch it, and log into your **Exness demo** (the account you made
earlier: `477834104`, `Exness-MT5Trial9`). If you'd rather start clean, **File →
Open an Account → Exness → demo**.

Then, in MT5:

1. **Tools → Options → Experts** → tick **Allow algorithmic trading**, leave
   *Disable algorithmic trading via external Python API* **unticked**
2. Click the **Algo Trading** toolbar button so it goes **green**
3. **Market Watch** (`Ctrl+M`) → right-click → **Show All** (a new demo lists
   only crypto; you need `EURUSDm`, `GBPUSDm` visible)

---

## Route A — Run everything in the VM

Open **PowerShell as Administrator** (Start → type `powershell` → right-click →
Run as administrator).

If you have not got the repo in the VM yet, you need it once:

```powershell
cd $env:USERPROFILE
git clone -b arena/8f8e7f7c-moneyminter https://github.com/mikeo-ne/moneyminter
```

> No `git`? `winget install --id Git.Git -e`. No `winget`? Install Python and
> Git from their websites, then continue.

Then one command does the rest:

```powershell
cd $env:USERPROFILE\moneyminter
Set-ExecutionPolicy -Scope Process -Bypass -Force
.\deploy\setup-windows.ps1
```

That script:

1. refuses to continue on **Windows-on-ARM** (where `MetaTrader5` has no wheel)
2. installs Python 3.12 and git via `winget` if missing
3. clones or updates the repo and creates `.venv`
4. installs `requirements.txt` **and** `MetaTrader5`
5. runs the full test suite
6. runs `python -m moneyminter doctor`
7. starts the RPyC bridge, so Route B is available later

Expect the test suite to take about two minutes.

Then, in that same PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m moneyminter connect --symbols EURUSD GBPUSD
```

You should see your account number, balance, and `EURUSDm` with a `✓`. Check the
`example trade ... lots` figure — that is your real position size at 0.25% risk.

Then a dry run, which sends no orders:

```powershell
python -m moneyminter live --symbols EURUSD GBPUSD --timeframe M5 `
    --strategy trend_pullback --dry-run -v
```

And when that looks sane, armed on the demo:

```powershell
python -m moneyminter live --symbols EURUSD GBPUSD --timeframe M5 `
    --strategy trend_pullback --risk 0.0025 --max-positions 2 --journal run.jsonl -v
```

---

## Route B — Linux runs the robot, Windows just executes

Use this from your **Kali VM**. The Windows VM needs to be reachable from it, so:

**1. Switch the VM's network to bridged.** In VirtualBox: **Settings → Network →
Adapter 1 → Attached to: Bridged Adapter** → pick your Mac's active interface
(Wi-Fi or Ethernet). This gives the VM its own address on your network, which
Kali can reach.

Find that address in the Windows VM:

```powershell
ipconfig
# look for IPv4 Address, e.g. 192.168.1.42
```

**2. Start the bridge.** Download `mt5server.exe` from
<https://github.com/lucas-campagna/mt5linux/releases> into the Windows VM, then:

```powershell
.\mt5server.exe --port 18812

# if Kali still can't connect, allow the port:
New-NetFirewallRule -DisplayName "MoneyMinter MT5 bridge" `
    -Direction Inbound -Protocol TCP -LocalPort 18812 -Action Allow
```

**3. In Kali**, point the robot at it:

```bash
python -m moneyminter connect --mt5-host 192.168.1.42 --mt5-port 18812
python -m moneyminter live --symbols EURUSD GBPUSD --timeframe M5 --dry-run -v
```

That's the whole difference: `--mt5-host` instead of a local terminal. Everything
else — strategies, risk, trailing, journal — behaves identically.

> The same flag works against your friend's VPS. Nothing in the code cares
> whether the Windows machine is next to you or in a datacentre.

---

## Don't let the Mac sleep

A VM stops when the host sleeps. Your stop-loss lives at the broker, so you stay
protected, but the robot stops **trailing** it. For a longer test:

```bash
caffeinate -i                 # keep the Mac awake while plugged in
```

and in **System Settings → Battery → Options**, set *Prevent automatic sleeping
when the display is off* while testing.

For 24/5 running, a VPS is the right answer — a laptop is not a server.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| VM won't boot, black screen | ISO is arm64 or corrupt | Re-download the **x64** ISO |
| "VT-x is not available" | Another hypervisor holds VT-x (Docker, VMware) | Quit it, or reboot |
| Windows 11 installer demands TPM | No TPM in the VM | **Settings → System → TPM: Enable**, or use Windows 10 |
| `pip install MetaTrader5` fails in the VM | The VM is ARM Windows | `setup-windows.ps1` checks this first; use an x86-64 VM |
| `connect` → `initialize() failed` | MT5 not running or not logged in | Launch MT5, log in, retry |
| `Algo Trading is disabled` | Toolbar button grey | Green button **and** Experts tab ticked |
| `Symbol EUR/USD not found` | Market Watch empty of FX | `Ctrl+M` → right-click → **Show All** |
| Kali can't reach the bridge | VM on NAT, or firewall | Bridged adapter + inbound rule on 18812 |
| Bridge connects then drops | MT5 terminal closed or logged out | Keep the terminal open; demo accounts expire after ~30 days idle |
| Everything is slow | No Guest Additions / 2 GB RAM | Install Guest Additions, raise RAM to 4 GB |

---

## Honest expectations

- The VM gives you a **correct** environment, not a **profitable** one. The
  strategies are well-built and unproven; demo results are how you find out.
- Watch **`avg R`** — your average result per trade in units of what you risked.
  That single number tells you more than the equity curve.
- Four weeks on unchanged settings before you consider anything else. If the
  demo can't hold up, live won't either.
