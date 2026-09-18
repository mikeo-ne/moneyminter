@echo off
REM ===================================================================
REM Money Minter - Windows VPS launcher with auto-restart
REM Edit the settings below, then double-click this file.
REM ===================================================================

cd /d "%~dp0\.."

REM --- your settings -------------------------------------------------
set SYMBOLS=EURUSD GBPUSD
set TIMEFRAME=M5
set STRATEGY=ma_crossover
set RISK=0.005
set MAXPOS=2
set EXTRA=

REM --- credentials (optional if MT5 is already logged in) ------------
REM set MT5_LOGIN=12345678
REM set MT5_PASSWORD=your-password
REM set MT5_SERVER=Exness-MT5Trial

:loop
echo.
echo [%date% %time%] Starting Money Minter...
.venv\Scripts\python.exe -m moneyminter live ^
    --symbols %SYMBOLS% --timeframe %TIMEFRAME% --strategy %STRATEGY% ^
    --risk %RISK% --max-positions %MAXPOS% %EXTRA%
echo [%date% %time%] Exited with code %ERRORLEVEL%. Restarting in 30s (Ctrl+C to stop)...
timeout /t 30 /nobreak
goto loop
