# Running Money Minter on a Windows VPS

## Auto-restart launcher

`moneyminter.bat` runs the bot and restarts it if it ever exits. Edit the settings
block at the top, then double-click it.

To start it automatically when the VPS boots:

1. Press `Win+R`, type `shell:startup`, press Enter.
2. Put a **shortcut** to `moneyminter.bat` in that folder.

MetaTrader 5 must also start on boot and log in automatically — in MT5 tick
*Tools → Options → Server → Keep personal settings and data at startup*.

## Health check

Confirm the bot can still see the terminal without starting a trading session:

```powershell
.venv\Scripts\python -m moneyminter live --symbols EURUSD --once
```

Schedule that with Task Scheduler if you want an automated heartbeat.

## Keeping the VPS awake

MT5 must stay running, so the VPS must not sleep or log you out:

- `Control Panel → Power Options → High performance`, sleep = Never.
- Disconnect from RDP with the **Disconnect** button, *not* "Sign out" — signing
  out closes MT5.
