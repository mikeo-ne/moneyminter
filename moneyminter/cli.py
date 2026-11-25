"""Money Minter command line interface."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from .backtest import optimize, run_backtest, walk_forward
from .broker import ExecutionConfig
from .engine import BotConfig, TradingBot
from .instruments import INSTRUMENTS
from .risk import RiskConfig
from .strategies import available_strategies

BANNER = r"""
  __  __                        __  __ _       _
 |  \/  | ___  _ __   ___ _   _|  \/  (_)_ __ | |_ ___ _ __
 | |\/| |/ _ \| '_ \ / _ \ | | | |\/| | | '_ \| __/ _ \ '__|
 | |  | | (_) | | | |  __/ |_| | |  | | | | | | ||  __/ |
 |_|  |_|\___/|_| |_|\___|\__, |_|  |_|_|_| |_|\__\___|_|
                          |___/   automated FX trading robot
"""


def _risk_from(args) -> RiskConfig:
    return RiskConfig(
        risk_per_trade=args.risk,
        max_positions=args.max_positions,
        max_daily_loss=getattr(args, "max_daily_loss", 0.03),
        max_drawdown=args.max_drawdown)


# --------------------------------------------------------------------- backtest
def cmd_backtest(args) -> int:
    res = run_backtest(args.symbol, args.timeframe, args.strategy, args.balance,
                       source=args.source, bars=args.bars, path=getattr(args, "csv", None),
                       seed=args.seed, risk=_risk_from(args),
                       execution=ExecutionConfig(spread_multiplier=args.spread_mult),
                       strategy_params=json.loads(args.params) if args.params else None)
    res.print_report()
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(res.to_dict(), fh, indent=2)
        print(f"\nSaved JSON report -> {args.json}")
    if args.show_trades:
        print("\nLast 20 trades:")
        for t in res.trades[-20:]:
            r = f"{t.r_multiple:+.2f}R" if t.r_multiple is not None else "     "
            print(f"  {t.closed_at:%Y-%m-%d %H:%M} {t.symbol} {t.side.value:<4} "
                  f"{t.units:>10,.0f}u  {t.entry_price:.5f} -> {t.exit_price:.5f}  "
                  f"${t.pnl:>9,.2f}  {r:>7}  {t.reason}")
    return 0


def cmd_optimize(args) -> int:
    rows = optimize(args.symbol, args.timeframe, args.strategy, json.loads(args.grid),
                    args.metric, balance=args.balance, source=args.source,
                    bars=args.bars, seed=args.seed, risk=_risk_from(args))
    print(f"\nTop {args.top} parameter sets by {args.metric}:")
    for r in rows[:args.top]:
        print(f"  {json.dumps(r['params']):<48} {args.metric}={r[args.metric]:>8.3f} "
              f"net=${r['net_profit']:>10,.2f} pf={r['profit_factor']:.2f} "
              f"trades={r['trades']} dd={r['max_drawdown_pct']:.1f}%")
    print("\nReminder: optimised parameters are fitted. Confirm them out-of-sample "
          "with `moneyminter walkforward`.")
    return 0


def cmd_walkforward(args) -> int:
    grid = json.loads(args.grid) if args.grid else None
    rows = walk_forward(args.symbol, args.timeframe, args.strategy, args.folds,
                        optimize_params=grid, balance=args.balance,
                        source=args.source, bars=args.bars, seed=args.seed,
                        risk=_risk_from(args))
    print(f"\nWalk-forward ({'anchored optimise + OOS test' if grid else 'out-of-sample splits'}):")
    profitable = 0
    for r in rows:
        if r["return_pct"] > 0:
            profitable += 1
        params = json.dumps(r.get("params", {})) if grid else ""
        print(f"  fold {r['fold']}: net=${r['net_profit']:>10,.2f} "
              f"ret={r['return_pct']:>7.2f}% pf={r['profit_factor']:.2f} "
              f"sharpe={r['sharpe']:.2f} trades={r['trades']:>3} {params}")
    if rows:
        print(f"\n  {profitable}/{len(rows)} folds profitable out-of-sample")
    return 0


# --------------------------------------------------------------------- validate
def cmd_validate(args) -> int:
    from .validation import scan, save_reports, validate

    if args.strategy == "all":
        reports = scan(symbol=args.symbol, timeframe=args.timeframe,
                       samples=args.samples, bars=args.bars)
    else:
        reports = [validate(args.strategy, symbol=args.symbol, timeframe=args.timeframe,
                            samples=args.samples, bars=args.bars,
                            params=json.loads(args.params) if args.params else None)]
    print(f"\n{'Strategy':<20} {'Score':>6} {'Verdict':<11} {'Prof':>5}  "
          f"{'PF':>5} {'Median ret':>11} {'Trades':>7} {'Worst':>8}")
    print("-" * 84)
    for r in reports:
        print(f"{r.strategy:<20} {r.score:>6.1f} {r.verdict:<11} "
              f"{r.profitable_pct * 100:>4.0f}%  {r.median_pf:>5.2f} "
              f"{r.median_return_pct:>10.2f}% {r.total_trades:>7} "
              f"{r.worst_return_pct:>7.1f}%")
    if len(reports) == 1:
        print("\n" + reports[0].summary())
    if args.json:
        path = save_reports(reports, args.json)
        print(f"\nSaved validation report -> {path}")
    print("\nNote: samples come from the offline simulator, which is calibrated to "
          "have\nno exploitable edge. 'rejected' here means a strategy cannot beat "
          "costs on\nrandom data — which is the correct answer. Validate on real "
          "prices with\n`--source yahoo` or your broker's CSV before drawing any "
          "conclusion.")
    return 0


# --------------------------------------------------------------------- paper
def cmd_trade(args) -> int:
    cfg = BotConfig.load(args.config) if args.config else BotConfig(
        symbols=args.symbols, timeframe=args.timeframe, strategy=args.strategy,
        balance=args.balance, speed=args.speed, source=args.source, risk=_risk_from(args),
        strategy_params=json.loads(args.params) if args.params else {})
    bot = TradingBot(cfg)
    print(BANNER)
    print(f"Paper trading {', '.join(cfg.symbols)} on {cfg.timeframe} with "
          f"'{cfg.strategy}' (speed x{cfg.speed}). Ctrl-C to stop.\n")
    bot.start()
    started = time.time()
    try:
        while True:
            time.sleep(args.refresh)
            s = bot.state()
            print(f"[{time.strftime('%H:%M:%S')}] equity ${s['equity']:>10,.2f} "
                  f"({s['pnl_pct']:+.2f}%) | open {len(s['open_positions'])} | "
                  f"trades {s['stats']['trades']} | win {s['stats']['win_rate']}% | "
                  f"dd {s['stats']['drawdown_pct']}%")
            for ev in list(bot.events)[:1]:
                if ev["kind"] in ("open", "close"):
                    print(f"          {ev['message']}")
            if args.duration and (time.time() - started) > args.duration:
                break
    except KeyboardInterrupt:
        print("\nStopping...")
    bot.stop()
    s = bot.state()
    print(f"\nFinal equity ${s['equity']:,.2f} ({s['pnl_pct']:+.2f}%) over "
          f"{s['stats']['trades']} trades, win rate {s['stats']['win_rate']}%")
    return 0


def cmd_dashboard(args) -> int:
    import uvicorn
    from .web.app import create_app

    cfg = BotConfig.load(args.config) if args.config else BotConfig(
        symbols=args.symbols, timeframe=args.timeframe, strategy=args.strategy,
        balance=args.balance, speed=args.speed, source=args.source, risk=_risk_from(args))
    # A dashboard on 0.0.0.0 is reachable by anyone on that network. Force
    # authentication unless the operator explicitly opted out or bound to
    # loopback only.
    auth = args.auth
    if auth is None:
        auth = args.host not in ("127.0.0.1", "localhost", "::1")
    app = create_app(cfg, autostart=not args.no_autostart, auth=auth,
                     auth_user=args.user, auth_password=args.password)
    print(BANNER)
    print(f"Dashboard on http://{args.host}:{args.port}"
          + ("  (password required)" if auth else "  (no password — loopback only)"))
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


# --------------------------------------------------------------------- demo sim
def cmd_demo(args) -> int:
    """Full demo-account trial against the simulated MT5 terminal.

    This runs the *live* code path — MT5Broker, broker-side stops, trailing,
    reconciliation — with no terminal and no waiting.
    """
    from .live import LiveConfig, LiveTrader
    from .live.sim_terminal import install

    install(terminal=None, symbols=args.symbols, timeframe=args.timeframe,
            bars=max(1000, args.bars), seed=args.seed, balance=args.balance,
            suffix=args.symbol_suffix if args.symbol_suffix is not None else "m",
            reject_first=1 if args.chaos else 0)
    cfg = LiveConfig(
        symbols=args.symbols, timeframe=args.timeframe, strategy=args.strategy,
        strategy_params=json.loads(args.params) if args.params else {},
        risk=_risk_from(args), sim=True, sim_bars_per_poll=args.bars_per_poll,
        history_bars=max(300, args.bars), poll_seconds=0, journal_path=args.journal)
    trader = LiveTrader(cfg)
    print(BANNER)
    pf = trader.preflight()
    a = pf["account"]
    print(f"Simulated {'DEMO' if a['demo'] else 'LIVE'} account #{a['login']} — "
          f"{a['server']}")
    print(f"  Balance {a['balance']:,.2f} {a['currency']} | leverage 1:{a['leverage']}")
    for r in pf["symbols"]:
        flag = "OK " if r["ready"] else "!! "
        print(f"  {flag}{r['symbol']:<9} -> {r['broker_symbol']:<10} "
              f"{r['price']:<10.5f} spread {r['spread_points']:>3}pts "
              f"min lot {r['min_lot']} | size {r['example_lots']} lots")
    print(f"\nStrategy {args.strategy} on {args.timeframe}, "
          f"{args.polls} polls x {args.bars_per_poll} bars "
          f"({args.polls * args.bars_per_poll} bars of market).")
    print("Running the real live code path against the simulator…\n")

    t0 = time.time()
    try:
        for i in range(args.polls):
            trader.poll()
            if args.verbose and (i + 1) % max(1, args.polls // 10) == 0:
                s = trader.state()
                print(f"  [{i + 1:>5}/{args.polls}] equity {s['equity']:>10,.2f} "
                      f"open {len(s['positions'])} trades {s['stats']['trades']}")
    except KeyboardInterrupt:
        print("\nStopped early.")
    elapsed = time.time() - t0
    if args.close_on_exit:
        trader.stop(close_positions=True)
    else:
        trader.stop(close_positions=False)

    s = trader.state()
    st = s["stats"]
    print("\n" + "=" * 66)
    print(f"  Simulated demo result ({elapsed:.1f}s of wall time)")
    print("=" * 66)
    print(f"  Start equity     : {trader.start_equity:,.2f}")
    print(f"  Final equity     : {s['equity']:,.2f}  ({s['pnl_pct']:+.2f}%)")
    print(f"  Realised P&L     : {st['total_pnl']:+,.2f}")
    print(f"  Trades closed    : {st['trades']} ({st['wins']}W / {st['losses']}L, "
          f"win {st['win_rate']}%)")
    print(f"  Average R        : {st['avg_r']:+.2f}R")
    print(f"  Open at exit     : {len(s['positions'])}")
    if trader.broker.rejected:
        print(f"  Orders rejected  : {trader.broker.rejected}")
    print(f"  Max drawdown     : {st['risk']['drawdown_pct']:.2f}%   "
          f"daily {st['risk']['daily_pnl_pct']:+.2f}%")
    counts = {}
    for e in trader.events:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    print(f"  Activity         : " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print("\n  Recent activity:")
    for e in s["events"][:12]:
        print(f"    · {e['message'][:88]}")
    print("\n  This exercised real order placement, broker-side stops, trailing, and\n"
          "  reconciliation — the same code that will run on your Exness demo. It\n"
          "  says nothing about profitability (the price feed is synthetic).")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"state": s, "events": trader.events[:200]}, fh, indent=2, default=str)
        print(f"\n  Saved -> {args.json}")
    return 0


# --------------------------------------------------------------------- live
def _live_config(args):
    """Build a LiveConfig, taking credentials from env vars (never the CLI)."""
    import os
    from .live import LiveConfig

    login = os.getenv("MT5_LOGIN")
    return LiveConfig(
        symbols=args.symbols, timeframe=args.timeframe, strategy=args.strategy,
        strategy_params=json.loads(args.params) if args.params else {},
        risk=_risk_from(args),
        login=int(login) if login else None,
        password=os.getenv("MT5_PASSWORD"),
        server=os.getenv("MT5_SERVER"),
        terminal_path=os.getenv("MT5_PATH") or args.terminal_path,
        symbol_suffix=args.symbol_suffix,
        allow_live=getattr(args, "i_understand_live_risk", False),
        max_live_balance=getattr(args, "max_live_balance", 500.0),
        dry_run=getattr(args, "dry_run", False),
        host=args.mt5_host or os.getenv("MT5_HOST"),
        port=args.mt5_port,
        backend=args.mt5_backend,
        poll_seconds=getattr(args, "poll", 5.0),
        trail_broker_stops=not getattr(args, "no_trail", False),
        journal_path=getattr(args, "journal", None),
    )


def cmd_connect(args) -> int:
    """Verify the MT5 connection, resolve symbols and show sizing — sends no orders."""
    from .live import LiveTrader, MT5Error

    try:
        trader = LiveTrader(_live_config(args))
    except MT5Error as exc:
        print(f"\n❌ {exc}\n")
        return 2
    info = trader.preflight()
    a = info["account"]
    print(BANNER)
    print(f"{'DEMO' if a['demo'] else '*** LIVE ***'} account #{a['login']} — {a['name']}")
    print(f"  Server        : {a['server']}")
    print(f"  Balance       : {a['balance']:,.2f} {a['currency']}   "
          f"Equity: {a['equity']:,.2f}   Free margin: {a['free_margin']:,.2f}")
    print(f"  Leverage      : 1:{a['leverage']}")
    print("\nSymbols:")
    for r in info["symbols"]:
        flag = "✓" if r["ready"] else "!"
        print(f"  {flag} {r['symbol']:<9} -> {r['broker_symbol']:<12} "
              f"price {r['price']:<12.5f} spread {r['spread_points']:>4} pts  "
              f"min lot {r['min_lot']:<5} step {r['lot_step']:<5} "
              f"| example trade {r['example_lots']} lots | {r['bars_available']} bars")
    print("\nIf the symbols and lot sizes look right, do a dry run first:")
    print(f"  python -m moneyminter live --symbols {' '.join(args.symbols)} "
          f"--timeframe {args.timeframe} --strategy {args.strategy} --dry-run")
    print("Then, on the demo account:")
    print(f"  python -m moneyminter live --symbols {' '.join(args.symbols)} "
          f"--timeframe {args.timeframe} --strategy {args.strategy} "
          f"--risk {args.risk}")
    trader.broker.shutdown()
    return 0


def cmd_doctor(args) -> int:
    """Check every prerequisite for connecting to MetaTrader 5, one by one."""
    import importlib
    import os
    import platform as _platform
    import sys as _sys

    print(BANNER)
    ok = True

    def line(good: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        if not good:
            ok = False
        mark = "✓" if good else "✗"
        print(f"  {mark} {label}" + (f"  {detail}" if detail else ""))

    print("Environment")
    line(True, "Platform",
         f"{_sys.platform} {_platform.machine()} " +
         ("(macOS)" if _sys.platform == "darwin" else
          "(MT5 needs Windows, or macOS+mt5_mac)"))

    # The single most confusing setup: MT5 installed in an ARM Windows VM on an
    # Apple Silicon Mac. The terminal runs, but the Python API is x86-64 only,
    # so no driver can ever attach — the error surfaces as "no backend".
    if _sys.platform == "win32" and _platform.machine().lower() in ("arm64", "aarch64"):
        line(False, "This is ARM Windows",
             "MT5's Python API ships x86-64 wheels only")
        print("      MetaTrader5 on PyPI: win32 and win_amd64 only — never win_arm64.")
        print("      MT5 itself may run here, but pip install MetaTrader5 cannot work,")
        print("      so the robot cannot connect to it. Use an x86-64 Windows VM")
        print("      (Intel Mac, Parallels/VMware, or a Windows VPS) instead.")
    line(_sys.version_info >= (3, 10), "Python",
         f"{_sys.version.split()[0]} (3.10+ required)")
    line(True, "Interpreter", _sys.executable)

    import moneyminter as _pkg
    src = os.path.dirname(os.path.abspath(_pkg.__file__))
    line(True, "Code in use", f"{src} (v{getattr(_pkg, '__version__', '?')})")
    if "site-packages" in src:
        print("      note: running an installed copy — 'git pull' will NOT update it.")
        print("            From the repo directory use  PYTHONPATH=. python -m moneyminter ...")
        print("            or reinstall with  pip install -e .")
    if not _sys.prefix.endswith(".venv") and "venv" not in _sys.prefix:
        print("    note: not inside a virtualenv — consider 'source .venv/bin/activate'")

    print("\nMetaTrader 5 terminal")
    if _sys.platform == "darwin":
        from .live.mt5_broker import diagnose_mac_bundle, find_mac_mt5_app
        app = find_mac_mt5_app()
        if app:
            ok_bundle, why = diagnose_mac_bundle(app)
            if ok_bundle:
                line(True, "MetaTrader 5.app", app)
            else:
                line(False, "MetaTrader 5.app has no usable Wine runtime", app)
                print("      " + why.replace("\n", "\n      "))
    else:
        app = None
        line(True, "Platform check", "macOS-specific checks skipped")

    print("\nPython drivers")
    for mod, label, hint in (
        ("MetaTrader5", "MetaTrader5 (official, Windows only)", "pip install MetaTrader5"),
        ("mt5_mac", "mt5_mac (macOS bridge)", "pip install mt5_mac"),
        ("rpyc", "rpyc (remote bridge)", "pip install rpyc"),
    ):
        try:
            m = importlib.import_module(mod)
            ver = getattr(m, "__version__", "")
            line(True, label, f"{mod} {ver}".strip())
        except ImportError:
            line(False, label, f"not installed — {hint}")

    print("\nBackend selection")
    try:
        from .live.mt5_broker import _import_mt5
        backend = getattr(args, "mt5_backend", "auto")
        mt5 = _import_mt5(host=getattr(args, "mt5_host", None),
                          port=getattr(args, "mt5_port", 18812),
                          backend=backend)
        line(True, "A backend is available", f"{getattr(mt5, '__name__', '?')} (--mt5-backend {backend})")
    except Exception as exc:  # noqa: BLE001
        line(False, "No backend available")
        print("      " + str(exc).replace("\n", "\n      "))

    print("\nOffline mode (works without any broker)")
    try:
        from .live import LiveTrader, LiveConfig
        from .live.sim_terminal import install as _sim_install
        from . import RiskConfig
        _sim_install(symbols=["EUR/USD"], timeframe="M5", bars=800)
        t = LiveTrader(LiveConfig(symbols=["EUR/USD"], timeframe="M5", sim=True,
                                  history_bars=300, journal_path=None,
                                  risk=RiskConfig(risk_per_trade=0.0025)))
        for _ in range(2500):
            t.poll()
        st = t.state()["stats"]
        line(st["trades"] > 0, "Simulated demo session runs",
             f"{st['trades']} trades opened/closed, equity "
             f"{t.broker.equity():,.2f}")
    except Exception as exc:  # noqa: BLE001
        line(False, "Simulated demo session FAILED", str(exc)[:160])

    print()
    if ok:
        print("Everything needed for a demo run is present.")
        print("Next:  python -m moneyminter connect --symbols EURUSD GBPUSD")
    else:
        print("Fix the ✗ items above, then re-run:  python -m moneyminter doctor")
    return 0 if ok else 2


def cmd_live(args) -> int:
    from .live import LiveTrader, MT5Error

    try:
        trader = LiveTrader(_live_config(args))
    except MT5Error as exc:
        print(f"\n❌ {exc}\n")
        return 2

    a = trader.broker.account_summary()
    print(BANNER)
    if not a["demo"]:
        print("*" * 68)
        print(f"*** LIVE MONEY — account #{a['login']}, balance {a['balance']:,.2f} "
              f"{a['currency']} ***")
        print("*" * 68)
        if not args.yes:
            if input("Type 'TRADE' to confirm real-money trading: ").strip() != "TRADE":
                print("Aborted.")
                return 1
    mode = "DRY RUN (no orders sent)" if args.dry_run else "ARMED"
    print(f"\n{mode} | {'DEMO' if a['demo'] else 'LIVE'} #{a['login']} | "
          f"{', '.join(trader.config.symbols)} {args.timeframe} | {args.strategy} | "
          f"risk {args.risk * 100:.2f}%/trade\nCtrl-C to stop.\n")

    if args.once:
        trader.poll()
        s = trader.state()
        print(f"single poll ok | equity {s['equity']:,.2f} | open {len(s['positions'])}")
        for ev in s["events"][:5]:
            print(f"  {ev['kind']}: {ev['message']}")
        trader.broker.shutdown()
        return 0

    trader.start()
    try:
        while True:
            time.sleep(args.refresh)
            s = trader.state()
            print(f"[{time.strftime('%H:%M:%S')}] equity {s['equity']:,.2f} "
                  f"({s['pnl']:+,.2f}) | open {len(s['positions'])} | "
                  f"trades {s['stats']['trades']} | avg {s['stats']['avg_r']:+.2f}R")
            for ev in s["events"][:1]:
                if ev["kind"] in ("open", "close", "error", "blocked"):
                    print(f"          {ev['message']}")
            if args.duration and trader.start_ts and \
                    (time.time() - trader.start_ts.timestamp()) > args.duration:
                break
    except KeyboardInterrupt:
        print("\nStopping...")
    trader.stop(close_positions=args.close_on_exit)
    trader.broker.shutdown()
    return 0


def cmd_strategies(_args) -> int:
    print("\nAvailable strategies:")
    for name, cls in available_strategies().items():
        doc = (cls.__doc__ or "").strip().split("\n")[0]
        style = getattr(cls, "style", "")
        print(f"  {name:<18} {style:<22} {doc}")
    print("\nAvailable instruments:")
    print("  " + ", ".join(INSTRUMENTS))
    return 0


# --------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("moneyminter",
                                description="Money Minter — automated FX trading robot")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, symbols=False):
        if symbols:
            sp.add_argument("--symbols", nargs="+",
                            default=["EUR/USD", "GBP/USD", "USD/JPY"])
        else:
            sp.add_argument("--symbol", default="EUR/USD")
        sp.add_argument("--timeframe", default="H1")
        sp.add_argument("--strategy", default="trend_pullback")
        sp.add_argument("--balance", type=float, default=10_000.0)
        sp.add_argument("--source", default="synthetic",
                        choices=["synthetic", "yahoo", "csv"])
        sp.add_argument("--bars", type=int, default=3000)
        sp.add_argument("--seed", type=int, default=7)
        sp.add_argument("--risk", type=float, default=0.005,
                        help="fraction of equity risked per trade (default 0.5%%)")
        sp.add_argument("--max-positions", type=int, default=3)
        sp.add_argument("--max-daily-loss", type=float, default=0.03)
        sp.add_argument("--max-drawdown", type=float, default=0.15)
        sp.add_argument("--params", help='JSON strategy params, e.g. \'{"fast":20}\'')

    bt = sub.add_parser("backtest", help="run a historical backtest")
    common(bt)
    bt.add_argument("--csv", help="csv path when --source csv")
    bt.add_argument("--spread-mult", type=float, default=1.0)
    bt.add_argument("--json", help="write a JSON report to this path")
    bt.add_argument("--show-trades", action="store_true")
    bt.set_defaults(func=cmd_backtest)

    op = sub.add_parser("optimize", help="grid-search strategy parameters")
    common(op)
    op.add_argument("--grid", required=True,
                    help='JSON grid, e.g. \'{"fast":[8,12],"slow":[26,34]}\'')
    op.add_argument("--metric", default="sharpe")
    op.add_argument("--top", type=int, default=10)
    op.set_defaults(func=cmd_optimize)

    wf = sub.add_parser("walkforward", help="out-of-sample validation folds")
    common(wf)
    wf.add_argument("--folds", type=int, default=4)
    wf.add_argument("--grid", help="optional grid to optimise in each training window")
    wf.set_defaults(func=cmd_walkforward)

    va = sub.add_parser("validate", help="full robustness battery for a strategy")
    common(va)
    va.add_argument("--samples", type=int, default=12,
                    help="independent price samples to test on")
    va.add_argument("--json", help="save the full report as JSON")
    va.set_defaults(func=cmd_validate)

    tr = sub.add_parser("trade", help="run the robot (paper trading)")
    common(tr, symbols=True)
    tr.add_argument("--speed", type=float, default=60.0,
                    help="simulation speed multiplier")
    tr.add_argument("--refresh", type=float, default=2.0)
    tr.add_argument("--duration", type=float, default=0, help="seconds to run (0 = forever)")
    tr.add_argument("--config", help="JSON bot config file")
    tr.set_defaults(func=cmd_trade)

    dm = sub.add_parser("demo", help="full demo trial on a simulated MT5 terminal")
    common(dm, symbols=True)
    dm.add_argument("--polls", type=int, default=3000,
                    help="number of poll cycles (default 3000)")
    dm.add_argument("--bars-per-poll", type=int, default=1,
                    help="simulated bars advanced per poll")
    dm.add_argument("--symbol-suffix", default="m",
                    help="broker symbol suffix to emulate (default 'm' = Exness)")
    dm.add_argument("--chaos", action="store_true",
                    help="reject the first order to exercise retry logic")
    dm.add_argument("--close-on-exit", action="store_true")
    dm.add_argument("--journal")
    dm.add_argument("--json")
    dm.set_defaults(func=cmd_demo)

    db = sub.add_parser("dashboard", help="launch the web dashboard")
    common(db, symbols=True)
    db.add_argument("--speed", type=float, default=60.0)
    db.add_argument("--host", default="0.0.0.0")
    db.add_argument("--port", type=int, default=8000)
    db.add_argument("--config")
    db.add_argument("--no-autostart", action="store_true")
    db.add_argument("--auth", dest="auth", action="store_true", default=None,
                    help="require a password (automatic when binding 0.0.0.0)")
    db.add_argument("--no-auth", dest="auth", action="store_false",
                    help="disable the password (only safe on loopback)")
    db.add_argument("--user", help="dashboard username (default admin)")
    db.add_argument("--password",
                    help="dashboard password (or set MM_DASH_PASSWORD)")
    db.set_defaults(func=cmd_dashboard)

    def live_flags(sp):
        sp.add_argument("--symbol-suffix",
                        help="broker symbol suffix, e.g. 'm' for Exness (auto-detected)")
        sp.add_argument("--terminal-path", help="path to terminal64.exe")
        sp.add_argument("--poll", type=float, default=5.0, help="seconds between checks")
        sp.add_argument("--mt5-host", help="host of a remote MT5 bridge")
        sp.add_argument("--mt5-port", type=int, default=18812)
        sp.add_argument("--mt5-backend", default="auto",
                        choices=["auto", "local", "mac", "rpyc"])
        sp.add_argument("--no-trail", action="store_true",
                        help="do not push trailing stops to the broker")
        sp.add_argument("--journal", help="append events to this JSONL file")

    dr = sub.add_parser("doctor", help="check MT5 prerequisites and diagnose setup problems")
    live_flags(dr)
    dr.set_defaults(func=cmd_doctor, symbols=["EUR/USD"], timeframe="M5",
                    strategy="trend_pullback", risk=0.0025)

    cn = sub.add_parser("connect", help="test the MT5/Exness connection (sends no orders)")
    common(cn, symbols=True)
    live_flags(cn)
    cn.set_defaults(func=cmd_connect)

    lv = sub.add_parser("live", help="trade a real MT5/Exness account (demo by default)")
    common(lv, symbols=True)
    live_flags(lv)
    lv.add_argument("--dry-run", action="store_true",
                    help="run the full loop but never send an order")
    lv.add_argument("--refresh", type=float, default=15.0)
    lv.add_argument("--duration", type=float, default=0)
    lv.add_argument("--close-on-exit", action="store_true",
                    help="flatten all robot positions when stopping")
    lv.add_argument("--yes", action="store_true",
                    help="skip the live-account confirmation prompt")
    lv.add_argument("--once", action="store_true",
                    help="run a single poll and exit (for cron / health checks)")
    lv.add_argument("--i-understand-live-risk", action="store_true",
                    help="permit trading a REAL-money account (demo-only without this)")
    lv.add_argument("--max-live-balance", type=float, default=500.0,
                    help="refuse live accounts richer than this")
    lv.set_defaults(func=cmd_live)

    st = sub.add_parser("strategies", help="list strategies and instruments")
    st.set_defaults(func=cmd_strategies)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    if hasattr(args, "timeframe") and args.timeframe:
        args.timeframe = args.timeframe.upper()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
