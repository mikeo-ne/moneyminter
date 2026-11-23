@echo off
REM ===================================================================
REM Money Minter - Windows VPS launcher with auto-restart
REM Edit the settings below, then double-click this file.
REM ===================================================================

cd /d "%~dp0\.."

REM --- your settings -------------------------------------------------
set SYMBOLS=EURUSD GBPUSD
set TIMEFRAME=M5
set STRATEGY=trend_pullback
set RISK=0.0025
set MAXPOS=2
set MAXDAILY=0.03
set MAXDD=0.15
set JOURNAL=data\live-journal.jsonl
set EXTRA=

REM --- safety gates --------------------------------------------------
REM Leave EXTRA empty to trade a DEMO account (the default).
REM Add --i-understand-live-risk ONLY for a small funded account.

REM --- credentials (optional if MT5 is already logged in) ------------
REM set MT5_LOGIN=12345678
REM set MT5_PASSWORD=your-password
REM set MT5_SERVER=Exness-MT5Trial

REM --- first run only: prove the plumbing works ----------------------
if not exist data\.demo_checked (
    echo Running a 20-second simulated demo trial before going live...
    .venv\Scripts\python.exe -m moneyminter demo --symbols %SYMBOLS% --polls 2000
    if errorlevel 1 (
        echo.
        echo The simulated demo trial failed. Fix that before trading.
        pause
        exit /b 1
    )
    if not exist data mkdir data
    echo ok > data\.demo_checked
    echo.
    echo Verification passed. Starting the live loop.
    timeout /t 3 /nobreak >nul
)

:loop
echo.
echo [%date% %time%] Starting Money Minter...
.venv\Scripts\python.exe -m moneyminter live ^
    --symbols %SYMBOLS% --timeframe %TIMEFRAME% --strategy %STRATEGY% ^
    --risk %RISK% --max-positions %MAXPOS% ^
    --max-daily-loss %MAXDAILY% --max-drawdown %MAXDD% ^
    --journal %JOURNAL% %EXTRA%
echo [%date% %time%] Exited with code %ERRORLEVEL%. Restarting in 30s (Ctrl+C to stop)...
timeout /t 30 /nobreak
goto loop
