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
    return RiskConfig(risk_per_trade=args.risk, max_positions=args.max_positions,
                      max_daily_loss=args.max_daily_loss, max_drawdown=args.max_drawdown)


def cmd_backtest(args) -> int:
    res = run_backtest(args.symbol, args.timeframe, args.strategy, args.balance,
                       source=args.source, bars=args.bars, path=args.csv, seed=args.seed,
                       risk=_risk_from(args),
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
            print(f"  {t.closed_at:%Y-%m-%d %H:%M} {t.symbol} {t.side.value:<4} "
                  f"{t.units:>10,.0f}u  {t.entry_price:.5f} -> {t.exit_price:.5f}  "
                  f"${t.pnl:>9,.2f}  {t.reason}")
    return 0


def cmd_optimize(args) -> int:
    grid = json.loads(args.grid)
    rows = optimize(args.symbol, args.timeframe, args.strategy, grid, args.metric,
                    balance=args.balance, source=args.source, bars=args.bars, seed=args.seed)
    print(f"\nTop {args.top} parameter sets by {args.metric}:")
    for r in rows[:args.top]:
        print(f"  {json.dumps(r['params']):<50} {args.metric}={r[args.metric]:>8.3f} "
              f"net=${r['net_profit']:>10,.2f} pf={r['profit_factor']:.2f} trades={r['trades']}")
    return 0


def cmd_walkforward(args) -> int:
    rows = walk_forward(args.symbol, args.timeframe, args.strategy, args.folds,
                        balance=args.balance, source=args.source, bars=args.bars, seed=args.seed)
    print("\nWalk-forward folds:")
    for r in rows:
        print(f"  fold {r['fold']}: net=${r['net_profit']:>10,.2f} ret={r['return_pct']:>7.2f}% "
              f"pf={r['profit_factor']:.2f} sharpe={r['sharpe']:.2f} trades={r['trades']}")
    return 0


def cmd_trade(args) -> int:
    cfg = BotConfig.load(args.config) if args.config else BotConfig(
        symbols=args.symbols, timeframe=args.timeframe, strategy=args.strategy,
        balance=args.balance, speed=args.speed, source=args.source, risk=_risk_from(args),
        strategy_params=json.loads(args.params) if args.params else {})
    bot = TradingBot(cfg)
    print(BANNER)
    print(f"Paper trading {', '.join(cfg.symbols)} on {cfg.timeframe} with '{cfg.strategy}' "
          f"(speed x{cfg.speed}). Ctrl-C to stop.\n")
    bot.start()
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
            if args.duration and bot.started_at and \
                    (time.time() - bot.started_at.timestamp()) > args.duration:
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
    app = create_app(cfg, autostart=not args.no_autostart)
    print(BANNER)
    print(f"Dashboard on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


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
        flag = "✓" if r["ready"] else "✗"
        print(f"  {flag} {r['symbol']:<9} -> {r['broker_symbol']:<12} "
              f"price {r['price']:<12.5f} spread {r['spread_points']:>4} pts  "
              f"min lot {r['min_lot']:<5} step {r['lot_step']:<5} "
              f"| example trade {r['example_lots']} lots | {r['bars_available']} bars")
    print("\nIf the symbols and lot sizes look right, start with:")
    print(f"  python -m moneyminter live --symbols {' '.join(args.symbols)} "
          f"--timeframe {args.timeframe} --strategy {args.strategy} --dry-run")
    trader.broker.shutdown()
    return 0


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

    trader.start()
    try:
        while True:
            time.sleep(args.refresh)
            s = trader.state()
            print(f"[{time.strftime('%H:%M:%S')}] equity {s['equity']:,.2f} "
                  f"({s['pnl']:+,.2f}) | open {len(s['positions'])} | "
                  f"trades {len(trader.broker.trades)}")
            for ev in s["events"][:1]:
                if ev["kind"] in ("open", "close", "error", "blocked"):
                    print(f"          {ev['message']}")
            if args.duration and (time.time() - trader.start_ts if hasattr(trader, "start_ts") else 0) > args.duration:
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
        print(f"  {name:<18} {doc}")
    print("\nAvailable instruments:")
    print("  " + ", ".join(INSTRUMENTS))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("moneyminter", description="Money Minter — automated FX trading robot")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, symbols=False):
        if symbols:
            sp.add_argument("--symbols", nargs="+", default=["EUR/USD", "GBP/USD", "USD/JPY"])
        else:
            sp.add_argument("--symbol", default="EUR/USD")
        sp.add_argument("--timeframe", default="H1")
        sp.add_argument("--strategy", default="ma_crossover")
        sp.add_argument("--balance", type=float, default=10_000.0)
        sp.add_argument("--source", default="synthetic", choices=["synthetic", "yahoo", "csv"])
        sp.add_argument("--bars", type=int, default=3000)
        sp.add_argument("--seed", type=int, default=7)
        sp.add_argument("--risk", type=float, default=0.01, help="fraction of equity risked per trade")
        sp.add_argument("--max-positions", type=int, default=3)
        sp.add_argument("--max-daily-loss", type=float, default=0.05)
        sp.add_argument("--max-drawdown", type=float, default=0.25)
        sp.add_argument("--params", help='JSON strategy params, e.g. \'{"fast":8,"slow":21}\'')

    bt = sub.add_parser("backtest", help="run a historical backtest")
    common(bt)
    bt.add_argument("--csv", help="csv path when --source csv")
    bt.add_argument("--spread-mult", type=float, default=1.0)
    bt.add_argument("--json", help="write a JSON report to this path")
    bt.add_argument("--show-trades", action="store_true")
    bt.set_defaults(func=cmd_backtest)

    op = sub.add_parser("optimize", help="grid-search strategy parameters")
    common(op)
    op.add_argument("--grid", required=True, help='JSON grid, e.g. \'{"fast":[8,12],"slow":[26,34]}\'')
    op.add_argument("--metric", default="sharpe")
    op.add_argument("--top", type=int, default=10)
    op.set_defaults(func=cmd_optimize)

    wf = sub.add_parser("walkforward", help="sequential out-of-sample validation")
    common(wf)
    wf.add_argument("--folds", type=int, default=4)
    wf.set_defaults(func=cmd_walkforward)

    tr = sub.add_parser("trade", help="run the robot (paper trading)")
    common(tr, symbols=True)
    tr.add_argument("--speed", type=float, default=60.0, help="simulation speed multiplier")
    tr.add_argument("--refresh", type=float, default=2.0)
    tr.add_argument("--duration", type=float, default=0, help="seconds to run (0 = forever)")
    tr.add_argument("--config", help="JSON bot config file")
    tr.set_defaults(func=cmd_trade)

    db = sub.add_parser("dashboard", help="launch the web dashboard")
    common(db, symbols=True)
    db.add_argument("--speed", type=float, default=60.0)
    db.add_argument("--host", default="0.0.0.0")
    db.add_argument("--port", type=int, default=8000)
    db.add_argument("--config")
    db.add_argument("--no-autostart", action="store_true")
    db.set_defaults(func=cmd_dashboard)

    def live_flags(sp):
        sp.add_argument("--symbol-suffix", help="broker symbol suffix, e.g. 'm' for Exness (auto-detected)")
        sp.add_argument("--terminal-path", help="path to terminal64.exe")
        sp.add_argument("--poll", type=float, default=5.0, help="seconds between checks")
        sp.add_argument("--mt5-host", help="host of a remote MT5 bridge (Linux/Wine, macOS "
                                           "Docker, or a Windows box on your LAN)")
        sp.add_argument("--mt5-port", type=int, default=18812,
                        help="bridge port (mt5linux 18812, siliconmetatrader5 8001)")
        sp.add_argument("--mt5-backend", default="auto",
                        choices=["auto", "local", "mac", "rpyc"],
                        help="which MT5 backend to use (default: auto-detect)")

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
    lv.add_argument("--yes", action="store_true", help="skip the live-account confirmation prompt")
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
    if args.timeframe if hasattr(args, "timeframe") else None:
        args.timeframe = args.timeframe.upper()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
