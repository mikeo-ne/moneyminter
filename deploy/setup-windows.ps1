# Money Minter — Windows setup for a MetaTrader 5 demo account
#
# Run in PowerShell **as Administrator** inside the Windows machine (VM or VPS)
# that has MetaTrader 5 installed and logged into your demo account:
#
#     Set-ExecutionPolicy -Scope Process -Bypass -Force
#     .\deploy\setup-windows.ps1
#
# It installs Python + the repo + the MT5 driver, verifies the terminal, runs the
# offline test suite, and finally starts the RPyC bridge so a Linux/macOS machine
# can trade through this one with `--mt5-host`.
#
# Safe to re-run: every step checks first and skips work already done.

[CmdletBinding()]
param(
    [string]$InstallDir = "$env:USERPROFILE\moneyminter",
    [string]$Branch     = "arena/8f8e7f7c-moneyminter",
    [string]$RepoUrl    = "https://github.com/mikeo-ne/moneyminter",
    [int]   $BridgePort = 18812,
    [switch]$NoBridge,          # skip starting the bridge
    [switch]$SkipTests          # skip the pytest run
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"

function Say  ($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Good ($m) { Write-Host "  [ok]   $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "  [warn] $m" -ForegroundColor Yellow }
function Die  ($m) { Write-Host "`n  [FAIL] $m" -ForegroundColor Red; exit 1 }

Write-Host @"
  __  __                        __  __ _       _
 |  \/  | ___  _ __   ___ _   _|  \/  (_)_ __ | |_ ___ _ __
 | |\/| |/ _ \| '_ \ / _ \ | | | |\/| | | '_ \| __/ _ \ '__|
 | |  | | (_) | | | |  __/ |_| | |  | | | | | | ||  __/ |
 |_|  |_|\___/|_| |_|\___|\__, |_|  |_|_|_| |_|\__\___|_|
                          |___/   Windows setup
"@ -ForegroundColor Yellow

# ---------------------------------------------------------------- 1. checks
Say "Checking this machine"

if (-not [Environment]::Is64BitOperatingSystem) {
    Die "This is 32-bit Windows. MetaTrader5 ships win64 wheels only."
}

$arch = $env:PROCESSOR_ARCHITECTURE
if ($arch -ne "AMD64") {
    Die @"
This machine reports PROCESSOR_ARCHITECTURE=$arch.

The MetaTrader5 package publishes win32 and win_amd64 wheels only — never
win_arm64 — so 'pip install MetaTrader5' cannot succeed on Windows-on-ARM,
even though MetaTrader 5 itself installs and runs there.

Use an x86-64 Windows VM (VirtualBox on an Intel Mac works) or an x86-64 VPS.
"@
}
Good "64-bit x86-64 Windows ($arch)"

$mt5 = Get-ChildItem "C:\Program Files\*MetaTrader*" -Directory -ErrorAction SilentlyContinue |
       Select-Object -First 1
if ($mt5) { Good "MetaTrader 5 found: $($mt5.FullName)" }
else {
    Warn "MetaTrader 5 not found under C:\Program Files"
    Warn "Install it from https://www.metatrader5.com/en/download, then re-run."
}

$mt5Running = Get-Process terminal64 -ErrorAction SilentlyContinue
if ($mt5Running) { Good "MT5 terminal is running (pid $($mt5Running.Id))" }
else { Warn "MT5 is not running. Start it and log into your demo before the last step." }

# ---------------------------------------------------------------- 2. python
Say "Ensuring Python 3.10+"

$py = $null
foreach ($c in @("py -3.12", "py -3.11", "py -3.10", "python")) {
    try {
        $exe, $rest = $c.Split(" ", 2)
        $out = & $exe $rest -c "import sys;print(sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            $ver = $out.Trim()
            if ($ver -match "\(3, (1[0-9])\)") { $py = $c; break }
        }
    } catch { }
}

if (-not $py) {
    Warn "No Python 3.10+ found — installing Python 3.12 with winget"
    try {
        winget install --id Python.Python.3.12 -e --source winget `
            --accept-package-agreements --accept-source-agreements --silent | Out-Null
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("Path","User")
        $py = "py -3.12"
    } catch {
        Die "Could not install Python automatically. Install 3.12 from python.org, then re-run."
    }
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Warn "git missing — installing with winget"
    winget install --id Git.Git -e --source winget `
        --accept-package-agreements --accept-source-agreements --silent | Out-Null
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path","User")
}
Good "Python: $py"

# ---------------------------------------------------------------- 3. repo
Say "Fetching Money Minter"
if (Test-Path (Join-Path $InstallDir ".git")) {
    Push-Location $InstallDir
    git fetch origin --quiet
    git checkout $Branch --quiet 2>&1 | Out-Null
    git reset --hard "origin/$Branch" --quiet
    Pop-Location
    Good "Updated existing checkout at $InstallDir"
} else {
    git clone --branch $Branch $RepoUrl $InstallDir
    if ($LASTEXITCODE -ne 0) { Die "git clone failed" }
    Good "Cloned to $InstallDir"
}

Push-Location $InstallDir
try {
    # ------------------------------------------------------------ 4. venv
    Say "Creating the virtual environment"
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        Invoke-Expression "$py -m venv .venv"
    }
    $vpy = ".\.venv\Scripts\python.exe"
    & $vpy -m pip install --quiet --upgrade pip
    & $vpy -m pip install --quiet -r requirements.txt
    & $vpy -m pip install --quiet MetaTrader5
    if ($LASTEXITCODE -ne 0) {
        Die "pip install MetaTrader5 failed — this usually means an ARM (not x86-64) Windows."
    }
    Good "Dependencies + MetaTrader5 installed"

    # ------------------------------------------------------------ 5. tests
    if (-not $SkipTests) {
        Say "Running the test suite (about 2 minutes)"
        & $vpy -m pytest tests -q
        if ($LASTEXITCODE -ne 0) { Warn "Some tests failed — investigate before trading" }
        else { Good "All tests passed" }
    }

    # ------------------------------------------------------------ 6. doctor
    Say "Diagnosing the MT5 setup"
    & $vpy -m moneyminter doctor

    # ------------------------------------------------------------ 7. bridge
    if (-not $NoBridge) {
        Say "Starting the RPyC bridge on port $BridgePort"
        $server = Get-ChildItem -Path $InstallDir, "$env:USERPROFILE\Downloads" `
                  -Filter "mt5server*.exe" -Recurse -ErrorAction SilentlyContinue |
                  Select-Object -First 1
        if ($server) {
            $ip = (Get-NetIPAddress -AddressFamily IPv4 |
                   Where-Object { $_.IPAddress -notmatch "^127\." } |
                   Select-Object -First 1).IPAddress
            Write-Host "  Starting $($server.FullName)" -ForegroundColor Gray
            Start-Process -FilePath $server.FullName -ArgumentList "--port", $BridgePort
            Start-Sleep -Seconds 3
            Good "Bridge listening on $ip`:$BridgePort"
            Write-Host @"

  From the Linux/macOS machine that will run the robot:

      python -m moneyminter connect --mt5-host $ip --mt5-port $BridgePort
      python -m moneyminter live --symbols EURUSD GBPUSD --timeframe M5 --dry-run

  If it times out, allow inbound TCP $BridgePort:
      New-NetFirewallRule -DisplayName "MoneyMinter MT5 bridge" ``
          -Direction Inbound -Protocol TCP -LocalPort $BridgePort -Action Allow

"@ -ForegroundColor Yellow
        } else {
            Warn "mt5server.exe not found — the bridge was not started."
            Write-Host "  Download it from https://github.com/lucas-campagna/mt5linux/releases" -ForegroundColor Gray
            Write-Host "  and re-run this script, or start it manually:" -ForegroundColor Gray
            Write-Host "      .\mt5server.exe --port $BridgePort" -ForegroundColor Gray
        }
    }
}
finally {
    Pop-Location
}

Write-Host @"

Done.

  Install dir : $InstallDir
  Activate    : cd `"$InstallDir`"; .\.venv\Scripts\Activate.ps1
  Always run  : python -m moneyminter doctor
  Backtest    : python -m moneyminter validate --strategy trend_pullback --samples 8
  Trade demo  : python -m moneyminter live --symbols EURUSD GBPUSD ``
                --timeframe M5 --risk 0.0025 --max-positions 2

Reminder: the robot refuses real-money accounts unless you pass
--i-understand-live-risk. Leave that flag off while testing.

"@ -ForegroundColor Green
