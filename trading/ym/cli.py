"""Command line interface.

Run ``python -m ym --help`` for the command list, or ``python -m ym demo`` to
see the whole pipeline run on simulated data without touching your own.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timedelta

from . import behavior as behavior_module
from .backtest import BacktestConfig, Backtester
from .coach import Coach
from .core import Direction, ExitReason, Trade
from .data import generate_bars, load_bars, resample, sniff, summarize, write_csv
from .data.trader import Habits, simulate_trader_history
from .instruments import MYM, REGISTRY, YM, get_instrument
from .journal import Journal
from .metrics import GROUPERS, compute_metrics, format_breakdown
from .risk import RiskLimits, RiskManager, SizingMethod
from .sessions import DEFAULT_SESSION, exchange_tz
from .strategies import REGISTRY as STRATEGIES


# --------------------------------------------------------------------------
# shared argument handling
# --------------------------------------------------------------------------

def add_db_argument(parser: argparse.ArgumentParser) -> None:
    """Let ``--db`` appear after the subcommand as well as before it."""
    parser.add_argument(
        "--db", default=argparse.SUPPRESS, help="journal database file"
    )


def add_risk_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("risk limits")
    group.add_argument("--risk-pct", type=float, default=0.5,
                       help="percent of equity risked per trade (default 0.5)")
    group.add_argument("--risk-dollars", type=float,
                       help="risk a flat dollar amount per trade instead")
    group.add_argument("--max-contracts", type=int, default=10)
    group.add_argument("--daily-loss-pct", type=float, default=2.0,
                       help="daily loss limit as a percent of equity (default 2.0)")
    group.add_argument("--daily-loss", type=float, help="daily loss limit in dollars")
    group.add_argument("--drawdown-pct", type=float, default=10.0,
                       help="max drawdown from peak equity before lockout")
    group.add_argument("--max-trades", type=int, help="max trades per day")
    group.add_argument("--max-consec", type=int, default=3,
                       help="consecutive losses before a breaker fires")
    group.add_argument("--consec-action", choices=("cooldown", "lock_day"),
                       default="cooldown")
    group.add_argument("--cooldown", type=int, default=30,
                       help="cooldown minutes after the loss streak")
    group.add_argument("--hard-stop", help="no new entries after this time, e.g. 12:00")
    group.add_argument("--giveback-pct", type=float,
                       help="percent of the day's peak profit you may give back")
    group.add_argument("--profit-target", type=float,
                       help="dollar daily profit target that ends the session")
    group.add_argument("--min-stop-ticks", type=int, default=4)
    group.add_argument("--min-rr", type=float, help="minimum reward:risk to allow")


def parse_clock(text: str | None) -> time | None:
    """Read a wall-clock time. ``13:30``, ``1330``, ``1:30pm`` and ``13`` all work.

    Bare digits are length-checked so that ``15`` means 15:00 rather than
    01:05 -- which is what a plain format-by-format scan would make of it.
    """
    if not text:
        return None
    raw = text.strip()
    formats = ["%H:%M", "%I:%M%p", "%I%p"]
    if raw.isdigit():
        if len(raw) in (3, 4):
            raw, formats = raw.zfill(4), ["%H%M"]
        elif len(raw) <= 2:
            formats = ["%H"]
        else:
            formats = []
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"cannot read a time from {text!r}; try 13:30, 1330, 1:30pm or 13"
    )


def limits_from_args(args: argparse.Namespace) -> RiskLimits:
    sizing = (
        SizingMethod.FIXED_DOLLAR
        if getattr(args, "risk_dollars", None)
        else SizingMethod.FIXED_FRACTIONAL
    )
    return RiskLimits(
        sizing=sizing,
        risk_per_trade_pct=args.risk_pct,
        fixed_dollar_risk=getattr(args, "risk_dollars", None),
        max_contracts=args.max_contracts,
        max_daily_loss_pct=args.daily_loss_pct,
        max_daily_loss=getattr(args, "daily_loss", None),
        max_drawdown_pct=args.drawdown_pct,
        max_trades_per_day=getattr(args, "max_trades", None),
        max_consecutive_losses=args.max_consec,
        consecutive_loss_action=args.consec_action,
        cooldown_minutes=args.cooldown,
        hard_stop_time=parse_clock(getattr(args, "hard_stop", None)),
        max_daily_giveback_pct=getattr(args, "giveback_pct", None),
        daily_profit_target=getattr(args, "profit_target", None),
        min_stop_ticks=args.min_stop_ticks,
        min_reward_risk=getattr(args, "min_rr", None),
    )


def instrument_from_args(args: argparse.Namespace):
    instrument = get_instrument(args.symbol)
    if getattr(args, "commission", None) is not None:
        instrument = instrument.with_costs(commission_per_side=args.commission)
    return instrument


def coerce(value: str):
    for caster in (int, float):
        try:
            return caster(value)
        except ValueError:
            continue
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    return value


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_size(args: argparse.Namespace) -> int:
    """How many contracts does this stop distance buy?"""
    instrument = instrument_from_args(args)
    manager = RiskManager(instrument, args.equity, limits_from_args(args))
    when = datetime.now(DEFAULT_SESSION.tz)
    decision = manager.evaluate(when, args.entry, args.stop, args.target)

    stop_points = abs(args.entry - args.stop)
    print(f"{instrument.symbol}  {instrument.name}")
    print(f"  entry {args.entry:g}  stop {args.stop:g}  "
          f"({stop_points:g} pts = {instrument.points_to_ticks(stop_points):g} ticks "
          f"= ${stop_points * instrument.point_value:,.2f} per contract)")
    if args.target:
        reward = abs(args.target - args.entry)
        print(f"  target {args.target:g}  ({reward:g} pts, "
              f"{reward / stop_points:.2f}R)")
    print()
    print(decision.explain())
    if decision.approved:
        print()
        print(f"  1R = ${decision.risk_dollars:,.2f}   "
              f"2R = ${decision.risk_dollars * 2:,.2f}   "
              f"commission (round turn) = "
              f"${instrument.round_turn_commission(decision.contracts):,.2f}")
    return 0 if decision.approved else 1


def cmd_data_info(args: argparse.Namespace) -> int:
    dialect = sniff(args.file)
    print(f"{args.file}\n")
    print(dialect.describe())
    print()
    bars = load_bars(args.file, tz=args.tz, limit=args.limit)
    print(summarize(bars))
    return 0


def cmd_data_sample(args: argparse.Namespace) -> int:
    bars = generate_bars(
        date.fromisoformat(args.start), days=args.days, seed=args.seed,
        start_price=args.price, include_overnight=args.overnight,
    )
    write_csv(bars, args.out)
    print(f"wrote {len(bars):,} synthetic bars to {args.out}")
    print(summarize(bars))
    print("\nSynthetic data is for testing the plumbing. It contains no real "
          "market structure -- never judge a strategy on it.")
    return 0


def cmd_data_convert(args: argparse.Namespace) -> int:
    bars = load_bars(args.file, tz=args.tz)
    if args.minutes:
        bars = resample(bars, args.minutes)
    write_csv(bars, args.out)
    print(f"wrote {len(bars):,} bars to {args.out}")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    instrument = instrument_from_args(args)
    if args.file:
        bars = load_bars(args.file, tz=args.tz)
    else:
        bars = generate_bars(date.fromisoformat(args.start), days=args.days, seed=args.seed)
        print("No data file given, so this ran on synthetic bars. The numbers "
              "below test the machinery, not the strategy.\n")

    strategy_class = STRATEGIES[args.strategy]
    params = dict(item.split("=", 1) for item in args.param or [])
    strategy = strategy_class(**{key: coerce(value) for key, value in params.items()})

    config = BacktestConfig(
        slippage_ticks=args.slippage,
        trade_sessions=tuple(args.sessions.split(",")),
        flatten_minutes_before_close=(
            None if args.hold_overnight else args.flatten_before_close
        ),
        pessimistic_intrabar=not args.optimistic,
    )
    backtester = Backtester(
        instrument, strategy, args.equity, limits_from_args(args), config
    )
    result = backtester.run(bars)
    print(result.format_report())
    if args.out:
        result.write_trades_csv(args.out)
        print(f"\nwrote {len(result.trades)} trades to {args.out}")
    if args.to_journal:
        with Journal(args.db) as journal:
            count = journal.record_many(result.trades, source=f"backtest:{strategy.name}")
        print(f"recorded {count} trades in {args.db}")
    return 0


def cmd_journal_import(args: argparse.Namespace) -> int:
    with Journal(args.db) as journal:
        imported, warnings = journal.import_csv(
            args.file, symbol=args.symbol, tz=args.tz,
            excursion_unit=args.excursion_unit,
            default_stop_points=args.default_stop_points,
        )
    print(f"imported {imported} trades into {args.db}")
    for warning in warnings[:10]:
        print(f"  warning: {warning}")
    if len(warnings) > 10:
        print(f"  ... and {len(warnings) - 10} more warnings")
    if not args.default_stop_points:
        print("\nNote: NinjaTrader exports do not include your stop, so R-multiples "
              "will be unavailable. Pass --default-stop-points, or fill in stops "
              "with 'journal set'.")
    return 0


def cmd_journal_add(args: argparse.Namespace) -> int:
    instrument = get_instrument(args.symbol)
    zone = exchange_tz(args.tz) if args.tz else DEFAULT_SESSION.tz
    entry_time = datetime.fromisoformat(args.entry_time).replace(tzinfo=zone)
    exit_time = (
        datetime.fromisoformat(args.exit_time).replace(tzinfo=zone)
        if args.exit_time else None
    )
    trade = Trade(
        symbol=instrument.symbol,
        direction=Direction.parse(args.direction),
        entry_time=entry_time,
        entry_price=args.entry,
        contracts=args.contracts,
        point_value=instrument.point_value,
        stop_price=args.stop,
        target_price=args.target,
        exit_time=exit_time,
        exit_price=args.exit,
        commission=(
            args.commission
            if args.commission is not None
            else instrument.round_turn_commission(args.contracts)
        ),
        exit_reason=ExitReason(args.reason) if args.reason else None,
        setup=args.setup or "",
        tags=args.tag or [],
        notes=args.notes or "",
    )
    with Journal(args.db) as journal:
        trade_id = journal.record(trade, planned=not args.unplanned)
        print(f"recorded trade {trade_id}: {trade.direction.value} "
              f"{trade.contracts}x {trade.symbol}", end="")
        if trade.is_closed:
            r = trade.r_multiple
            print(f"  net ${trade.net_pnl:,.2f}"
                  + (f"  {r:+.2f}R" if r is not None else "  (no stop, no R)"))
        else:
            print("  (open)")
    return 0


def cmd_journal_set(args: argparse.Namespace) -> int:
    fields = {}
    if args.stop is not None:
        fields["stop_price"] = args.stop
    if args.target is not None:
        fields["target_price"] = args.target
    if args.setup is not None:
        fields["setup"] = args.setup
    if args.notes is not None:
        fields["notes"] = args.notes
    if args.tags is not None:
        fields["tags"] = args.tags
    if args.unplanned:
        fields["planned"] = 0
    if args.planned:
        fields["planned"] = 1
    if not fields:
        print("nothing to change")
        return 1
    with Journal(args.db) as journal:
        journal.update(args.id, **fields)
    print(f"updated trade {args.id}: {', '.join(fields)}")
    return 0


def cmd_journal_list(args: argparse.Namespace) -> int:
    with Journal(args.db) as journal:
        trades = journal.trades(start=args.start, end=args.end, symbol=args.symbol,
                                setup=args.setup, limit=args.limit,
                                newest_first=True)
        open_trades = journal.open_trades()
    if not trades and not open_trades:
        print(f"{args.db} has no trades yet.")
        return 0
    header = (f"{'id':>4} {'day':<11}{'time':<7}{'sym':<5}{'dir':<6}{'qty':>4}"
              f"{'entry':>10}{'exit':>10}{'net':>10}{'R':>7}  setup")
    print(header)
    print("-" * len(header))
    for trade in trades:
        r = trade.r_multiple
        print(f"{trade.trade_id:>4} "
              f"{DEFAULT_SESSION.session_day(trade.entry_time)!s:<11}"
              f"{trade.entry_time:%H:%M}  "
              f"{trade.symbol:<5}{trade.direction.value:<6}{trade.contracts:>4}"
              f"{trade.entry_price:>10,.0f}{trade.exit_price:>10,.0f}"
              f"{trade.net_pnl:>10,.2f}"
              f"{('   n/a' if r is None else f'{r:>+7.2f}')}  {trade.setup}")
    if open_trades:
        print(f"\n{len(open_trades)} open trade(s): "
              + ", ".join(f"#{t.trade_id} {t.direction.value} {t.contracts}x {t.symbol}"
                          for t in open_trades))
    return 0


def cmd_journal_stats(args: argparse.Namespace) -> int:
    with Journal(args.db) as journal:
        trades = journal.trades(start=args.start, end=args.end, symbol=args.symbol)
        planned_ratio = journal.planned_ratio()
    if not trades:
        print("no closed trades match that filter")
        return 1
    print(compute_metrics(trades, args.equity).format_report("Journal performance"))
    if planned_ratio is not None and planned_ratio < 1.0:
        print(f"\nPlanned trades: {planned_ratio:.0%} of the journal")
    for grouping in args.by or []:
        print()
        print(format_breakdown(trades, grouping))
    return 0


def cmd_journal_seed(args: argparse.Namespace) -> int:
    """Fill a journal with a simulated history, to explore the analysis."""
    habits = Habits(
        early_edge_r=args.early_edge,
        late_edge_r=args.late_edge,
        edge_decays_after=args.decays_after,
        revenge_probability=args.revenge,
        stop_overshoot=args.stop_overshoot,
        unplanned_probability=args.unplanned_rate,
    )
    trades = simulate_trader_history(
        days=args.days, habits=habits, instrument=get_instrument(args.symbol),
        seed=args.seed,
    )
    with Journal(args.db) as journal:
        for trade in trades:
            journal.record(
                trade, planned="unplanned" not in trade.tags, source="simulated"
            )
    print(f"seeded {args.db} with {len(trades)} simulated trades over "
          f"{args.days} days.")
    print("These are invented trades for exploring the tooling. Use a separate "
          "database file for your real ones.")
    return 0


def cmd_journal_export(args: argparse.Namespace) -> int:
    with Journal(args.db) as journal:
        count = journal.export_csv(args.out, start=args.start, end=args.end)
    print(f"wrote {count} trades to {args.out}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """The behavioral review: what your own record says about your habits."""
    with Journal(args.db) as journal:
        trades = journal.trades(start=args.start, end=args.end, symbol=args.symbol)
    if not trades:
        print(f"{args.db} has no closed trades to review.")
        return 1
    report = behavior_module.analyze(trades, min_trades=args.min_trades)
    print(report.format_report())
    if args.apply:
        suggested = report.suggested_limits(limits_from_args(args))
        print("\nRiskLimits with those guardrails applied:")
        for name in (
            "risk_per_trade_pct", "max_trades_per_day", "max_consecutive_losses",
            "consecutive_loss_action", "cooldown_minutes", "hard_stop_time",
            "max_daily_giveback_pct", "daily_profit_target", "max_daily_loss_pct",
            "max_drawdown_pct",
        ):
            value = getattr(suggested, name)
            if value is not None:
                rendered = value.strftime("%H:%M") if hasattr(value, "hour") else value
                print(f"  {name} = {rendered}")
        print("\nThese are suggestions from your own sample. Adopt the ones you "
              "recognize; a guardrail you do not believe in will not hold.")
    return 0


def cmd_coach(args: argparse.Namespace) -> int:
    """Where you stand right now, and what your history says about it."""
    with Journal(args.db) as journal:
        all_trades = journal.trades()
    if not all_trades:
        print(f"{args.db} has no trades yet -- nothing to coach from.")
        return 1

    when = (
        datetime.fromisoformat(args.at).replace(tzinfo=DEFAULT_SESSION.tz)
        if args.at else datetime.now(DEFAULT_SESSION.tz)
    )
    today = DEFAULT_SESSION.session_day(when)
    history = [
        trade for trade in all_trades
        if DEFAULT_SESSION.session_day(trade.entry_time) < today
    ]
    todays = [
        trade for trade in all_trades
        if DEFAULT_SESSION.session_day(trade.entry_time) == today
    ]

    instrument = instrument_from_args(args)
    manager = RiskManager(instrument, args.equity, limits_from_args(args))
    for trade in todays:
        manager.register_trade(trade)

    coach = Coach.from_trades(history) if history else Coach()
    print(f"As of {when:%Y-%m-%d %H:%M %Z}  (session day {today})")
    print(f"Profile built from {len(history)} prior trades; "
          f"{len(todays)} trade(s) today.\n")
    print(coach.brief(when, manager, todays))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Run the whole pipeline on simulated data."""
    separator = "\n" + "=" * 72 + "\n"

    print("1. RISK: sizing a YM trade with a 15 point stop on a $25,000 account")
    manager = RiskManager(YM, 25_000, RiskLimits(risk_per_trade_pct=0.5))
    print()
    print(manager.evaluate(datetime.now(DEFAULT_SESSION.tz), 41_000, 40_985).explain())

    print(separator + "2. BACKTEST: opening range breakout on synthetic bars")
    bars = generate_bars(date(2026, 6, 1), days=40, seed=11)
    for instrument in (YM, MYM):
        result = Backtester(
            instrument, STRATEGIES["orb"](range_minutes=30, target_r=2.0),
            25_000, RiskLimits(risk_per_trade_pct=0.5, max_daily_loss_pct=2.0),
            BacktestConfig(slippage_ticks=1),
        ).run(bars)
        summary = (
            f"{result.metrics.trades} trades, net ${result.metrics.net_pnl:,.2f}"
            if result.metrics.trades
            else f"no trades -- every signal refused: {result.blocked_summary()}"
        )
        print(f"  {instrument.symbol}: {summary}")
    print("\n  The wide range-based stop costs more than 0.5% of $25,000 on YM, so "
          "\n  the risk manager refuses it and allows the micro contract instead.")

    print(separator + "3. BEHAVIOR: reviewing a simulated trader who fades after "
          "two trades")
    history = simulate_trader_history(days=60, habits=Habits(
        early_edge_r=0.4, late_edge_r=-0.5, edge_decays_after=2,
        revenge_probability=0.65, stop_overshoot=0.45, unplanned_probability=0.2))
    report = behavior_module.analyze(history)
    print()
    print(report.format_report())

    print(separator + "4. COACH: that trader, about to take a fourth trade at 13:05")
    limits = report.suggested_limits(RiskLimits(risk_per_trade_pct=0.5))
    manager = RiskManager(MYM, 25_000, limits)
    day = date(2026, 9, 8)
    todays: list[Trade] = []
    for hour, minute, points in ((9, 45, 50), (10, 40, 34), (12, 40, -26)):
        entry = datetime.combine(day, time(hour, minute), tzinfo=DEFAULT_SESSION.tz)
        trade = Trade("MYM", Direction.LONG, entry, 41_000, 2, 0.5,
                      stop_price=40_975, commission=2.0, setup="ORB")
        trade.close(entry + timedelta(minutes=12), 41_000 + points,
                    ExitReason.TARGET if points > 0 else ExitReason.STOP)
        todays.append(trade)
        manager.register_trade(trade)
    now = datetime.combine(day, time(13, 5), tzinfo=DEFAULT_SESSION.tz)
    print()
    print(Coach.from_trades(history).brief(now, manager, todays))
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ym",
        description="Risk management, backtesting and behavioral review for "
                    "Dow e-mini futures (YM / MYM).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Start with:  python -m ym demo",
    )
    parser.add_argument("--db", default="journal.db", help="journal database file")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # size ------------------------------------------------------------------
    size = subparsers.add_parser("size", help="position sizing and a pre-trade check")
    size.add_argument("--symbol", default="YM", choices=sorted(REGISTRY))
    size.add_argument("--equity", type=float, required=True)
    size.add_argument("--entry", type=float, required=True)
    size.add_argument("--stop", type=float, required=True)
    size.add_argument("--target", type=float)
    size.add_argument("--commission", type=float, help="commission per side")
    add_risk_arguments(size)
    size.set_defaults(func=cmd_size)

    # data ------------------------------------------------------------------
    data = subparsers.add_parser("data", help="inspect, convert and generate bar data")
    data_subparsers = data.add_subparsers(dest="data_command", required=True)

    info = data_subparsers.add_parser("info", help="sniff a file and summarize it")
    info.add_argument("file")
    info.add_argument("--tz", default="America/New_York",
                      help="timezone the file's timestamps are written in")
    info.add_argument("--limit", type=int)
    info.set_defaults(func=cmd_data_info)

    sample = data_subparsers.add_parser("sample", help="generate synthetic bars")
    sample.add_argument("--out", default="sample_ym.csv")
    sample.add_argument("--start", default="2026-06-01")
    sample.add_argument("--days", type=int, default=30)
    sample.add_argument("--price", type=float, default=41000.0)
    sample.add_argument("--seed", type=int, default=7)
    sample.add_argument("--overnight", action="store_true")
    sample.set_defaults(func=cmd_data_sample)

    convert = data_subparsers.add_parser("convert", help="normalize or resample a file")
    convert.add_argument("file")
    convert.add_argument("--out", required=True)
    convert.add_argument("--tz", default="America/New_York")
    convert.add_argument("--minutes", type=int, help="resample to this timeframe")
    convert.set_defaults(func=cmd_data_convert)

    # backtest --------------------------------------------------------------
    backtest = subparsers.add_parser("backtest", help="run a strategy over bars")
    backtest.add_argument("file", nargs="?", help="bar data file (omit for synthetic)")
    backtest.add_argument("--strategy", default="orb", choices=sorted(STRATEGIES))
    backtest.add_argument("-p", "--param", action="append", metavar="KEY=VALUE",
                          help="strategy parameter, repeatable")
    backtest.add_argument("--symbol", default="YM", choices=sorted(REGISTRY))
    backtest.add_argument("--equity", type=float, default=25000.0)
    backtest.add_argument("--commission", type=float)
    backtest.add_argument("--slippage", type=float, default=1.0,
                          help="ticks of slippage per side (default 1)")
    backtest.add_argument("--sessions", default="rth",
                          help="comma-separated sessions to trade (default rth)")
    backtest.add_argument("--flatten-before-close", type=int, default=5)
    backtest.add_argument("--hold-overnight", action="store_true")
    backtest.add_argument("--optimistic", action="store_true",
                          help="assume the target fills first when a bar holds both")
    backtest.add_argument("--tz", default="America/New_York")
    backtest.add_argument("--start", default="2026-06-01", help="synthetic start date")
    backtest.add_argument("--days", type=int, default=40, help="synthetic day count")
    backtest.add_argument("--seed", type=int, default=11)
    backtest.add_argument("--out", help="write the trade list to this CSV")
    backtest.add_argument("--to-journal", action="store_true",
                          help="also record the trades in the journal")
    add_risk_arguments(backtest)
    add_db_argument(backtest)
    backtest.set_defaults(func=cmd_backtest)

    # journal ---------------------------------------------------------------
    journal = subparsers.add_parser("journal", help="record and analyze real trades")
    journal_subparsers = journal.add_subparsers(dest="journal_command", required=True)

    importer = journal_subparsers.add_parser(
        "import", help="import a NinjaTrader or backtester trade CSV"
    )
    importer.add_argument("file")
    importer.add_argument("--symbol", default="YM")
    importer.add_argument("--tz", default=None)
    importer.add_argument("--excursion-unit", choices=("currency", "points"),
                          default="currency",
                          help="unit NinjaTrader reported MAE/MFE in")
    importer.add_argument("--default-stop-points", type=float,
                          help="assume this stop distance when none is recorded")
    add_db_argument(importer)
    importer.set_defaults(func=cmd_journal_import)

    add = journal_subparsers.add_parser("add", help="record one trade by hand")
    add.add_argument("--symbol", default="YM")
    add.add_argument("--direction", required=True)
    add.add_argument("--entry-time", required=True, help="e.g. 2026-09-08T09:45")
    add.add_argument("--entry", type=float, required=True)
    add.add_argument("--contracts", type=int, default=1)
    add.add_argument("--stop", type=float, help="initial stop -- record it, it defines 1R")
    add.add_argument("--target", type=float)
    add.add_argument("--exit-time")
    add.add_argument("--exit", type=float)
    add.add_argument("--commission", type=float)
    add.add_argument("--reason", choices=[reason.value for reason in ExitReason])
    add.add_argument("--setup")
    add.add_argument("--tag", action="append")
    add.add_argument("--notes")
    add.add_argument("--unplanned", action="store_true",
                     help="mark this as off-plan -- be honest, it is the useful field")
    add.add_argument("--tz")
    add_db_argument(add)
    add.set_defaults(func=cmd_journal_add)

    setter = journal_subparsers.add_parser("set", help="fill in fields on a trade")
    setter.add_argument("id", type=int)
    setter.add_argument("--stop", type=float)
    setter.add_argument("--target", type=float)
    setter.add_argument("--setup")
    setter.add_argument("--notes")
    setter.add_argument("--tags")
    setter.add_argument("--unplanned", action="store_true")
    setter.add_argument("--planned", action="store_true")
    add_db_argument(setter)
    setter.set_defaults(func=cmd_journal_set)

    lister = journal_subparsers.add_parser("list", help="list journalled trades")
    lister.add_argument("--start")
    lister.add_argument("--end")
    lister.add_argument("--symbol")
    lister.add_argument("--setup")
    lister.add_argument("--limit", type=int, default=50)
    add_db_argument(lister)
    lister.set_defaults(func=cmd_journal_list)

    stats = journal_subparsers.add_parser("stats", help="performance from the journal")
    stats.add_argument("--start")
    stats.add_argument("--end")
    stats.add_argument("--symbol")
    stats.add_argument("--equity", type=float, default=25000.0)
    stats.add_argument("--by", action="append", choices=sorted(GROUPERS),
                       help="add a breakdown, repeatable")
    add_db_argument(stats)
    stats.set_defaults(func=cmd_journal_stats)

    seed = journal_subparsers.add_parser(
        "seed", help="fill a journal with simulated trades, to explore the analysis"
    )
    seed.add_argument("--days", type=int, default=60)
    seed.add_argument("--symbol", default="MYM")
    seed.add_argument("--seed", type=int, default=42)
    seed.add_argument("--early-edge", type=float, default=0.4,
                      help="expectancy in R on the day's first trades")
    seed.add_argument("--late-edge", type=float, default=-0.5,
                      help="expectancy in R once the edge has faded")
    seed.add_argument("--decays-after", type=int, default=2)
    seed.add_argument("--revenge", type=float, default=0.65,
                      help="probability of re-entering fast after a loss")
    seed.add_argument("--stop-overshoot", type=float, default=0.45,
                      help="extra R lost past the stop")
    seed.add_argument("--unplanned-rate", type=float, default=0.2)
    add_db_argument(seed)
    seed.set_defaults(func=cmd_journal_seed)

    exporter = journal_subparsers.add_parser("export", help="export the journal to CSV")
    exporter.add_argument("--out", required=True)
    exporter.add_argument("--start")
    exporter.add_argument("--end")
    add_db_argument(exporter)
    exporter.set_defaults(func=cmd_journal_export)

    # review ----------------------------------------------------------------
    review = subparsers.add_parser(
        "review", help="behavioral review: find your habits and suggest guardrails"
    )
    review.add_argument("--start")
    review.add_argument("--end")
    review.add_argument("--symbol")
    review.add_argument("--min-trades", type=int, default=20)
    review.add_argument("--apply", action="store_true",
                        help="print the RiskLimits those guardrails imply")
    add_risk_arguments(review)
    add_db_argument(review)
    review.set_defaults(func=cmd_review)

    # coach -----------------------------------------------------------------
    coach = subparsers.add_parser(
        "coach", help="pre-trade check against today and your own history"
    )
    coach.add_argument("--symbol", default="YM", choices=sorted(REGISTRY))
    coach.add_argument("--equity", type=float, required=True)
    coach.add_argument("--at", help="pretend it is this time, e.g. 2026-09-08T13:05")
    coach.add_argument("--commission", type=float)
    add_risk_arguments(coach)
    add_db_argument(coach)
    coach.set_defaults(func=cmd_coach)

    # demo ------------------------------------------------------------------
    demo = subparsers.add_parser("demo", help="run everything on simulated data")
    demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, KeyError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
