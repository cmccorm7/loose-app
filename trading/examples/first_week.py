"""A worked example: from nothing to a reviewed week.

Run it with ``python examples/first_week.py`` from the ``trading/`` directory.
Every number here comes from simulated data -- the point is the workflow, not
the results.
"""

import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

# Run from anywhere: put the project root (the parent of ym/) on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ym.backtest import BacktestConfig, Backtester
from ym.behavior import analyze
from ym.coach import Coach
from ym.core import Direction, ExitReason, Trade
from ym.data import generate_bars
from ym.data.trader import Habits, simulate_trader_history
from ym.instruments import MYM, YM
from ym.journal import Journal
from ym.risk import RiskLimits, RiskManager
from ym.sessions import DEFAULT_SESSION
from ym.strategies import OpeningRangeBreakout

HERE = Path(__file__).parent
RULE = "\n" + "-" * 72 + "\n"


def step_1_size_a_trade() -> None:
    """Risk first: decide what a trade may cost before deciding to take it."""
    print("STEP 1 -- What does this trade cost, and how big can it be?\n")
    limits = RiskLimits(
        risk_per_trade_pct=0.5,      # half a percent of equity per trade
        max_daily_loss_pct=2.0,      # stop for the day down 2%
        max_drawdown_pct=10.0,       # stop entirely down 10% from the peak
        max_consecutive_losses=3,
    )
    for instrument in (YM, MYM):
        manager = RiskManager(instrument, 25_000, limits)
        decision = manager.evaluate(
            datetime.now(DEFAULT_SESSION.tz), 41_000, 40_970  # a 30 point stop
        )
        print(f"{instrument.symbol} with a 30 point stop "
              f"(${30 * instrument.point_value:,.2f} per contract):")
        print("  " + decision.explain().replace("\n", "\n  "))
        print()
    print("The same idea on the same chart is a different trade on each contract.\n"
          "On a $25,000 account the full-size Dow contract barely fits; the micro\n"
          "leaves room to be wrong.")


def step_2_backtest() -> None:
    """Test the idea with the same risk rules you would actually trade."""
    print("STEP 2 -- Backtest an opening range breakout\n")
    bars = generate_bars(date(2026, 6, 1), days=60, seed=11)
    result = Backtester(
        MYM,
        OpeningRangeBreakout(range_minutes=30, target_r=2.0, breakeven_at_r=1.0),
        starting_equity=25_000,
        limits=RiskLimits(risk_per_trade_pct=0.5, max_daily_loss_pct=2.0),
        config=BacktestConfig(slippage_ticks=1.0),   # one tick each way
    ).run(bars)
    print(result.format_report())
    print("\nThis ran on a random walk, so an expectancy near zero is the correct\n"
          "answer. Re-run it on your own NinjaTrader export before believing\n"
          "anything: python -m ym backtest YM_1min.txt --strategy orb")


def step_3_journal_and_review(db_path: Path) -> None:
    """Record what you did, then let the record tell you about your habits."""
    print("STEP 3 -- Journal three months, then review the behavior\n")
    if db_path.exists():
        db_path.unlink()

    history = simulate_trader_history(
        days=60,
        habits=Habits(
            early_edge_r=0.4,        # the setups you waited for
            late_edge_r=-0.5,        # the ones you went looking for
            edge_decays_after=2,
            revenge_probability=0.65,
            stop_overshoot=0.45,     # stops honored late
            unplanned_probability=0.2,
        ),
        seed=42,
    )
    with Journal(db_path) as journal:
        for trade in history:
            journal.record(
                trade, planned="unplanned" not in trade.tags, source="simulated"
            )
        print(f"journalled {journal.count()} trades\n")
        print(journal.breakdown("hour"))
        trades = journal.trades()

    report = analyze(trades)
    print()
    print(report.format_report())
    return report, trades


def step_4_coach(report, history) -> None:
    """The same findings, delivered at the moment they matter."""
    print("STEP 4 -- A live session, with those findings in the loop\n")
    limits = report.suggested_limits(RiskLimits(risk_per_trade_pct=0.5))
    manager = RiskManager(MYM, 25_000, limits)
    coach = Coach.from_trades(history)  # the same record the review just read

    day = date(2026, 9, 8)
    today: list[Trade] = []
    script = [
        (time(9, 45), 50, "first setup of the day, a winner"),
        (time(10, 40), 34, "second setup, also a winner"),
        (time(12, 40), -26, "a stop-out just after lunch"),
    ]
    for when, points, label in script:
        entry = datetime.combine(day, when, tzinfo=DEFAULT_SESSION.tz)
        trade = Trade("MYM", Direction.LONG, entry, 41_000, 2, 0.5,
                      stop_price=40_975, commission=2.0, setup="ORB")
        trade.close(entry + timedelta(minutes=12), 41_000 + points,
                    ExitReason.TARGET if points > 0 else ExitReason.STOP)
        today.append(trade)
        manager.register_trade(trade)
        print(f"  {when:%H:%M}  {label:<36} net ${trade.net_pnl:+,.2f}")

    now = datetime.combine(day, time(12, 46), tzinfo=DEFAULT_SESSION.tz)
    print(f"\nIt is {now:%H:%M}. You are looking at a fourth trade.\n")
    print(coach.brief(now, manager, today))


def main() -> None:
    step_1_size_a_trade()
    print(RULE)
    step_2_backtest()
    print(RULE)
    report, history = step_3_journal_and_review(HERE / "example_journal.db")
    print(RULE)
    step_4_coach(report, history)
    print(RULE)
    print("Next: replace the simulated pieces with your own data.\n"
          "  python -m ym data info  /path/to/YM_1min.txt\n"
          "  python -m ym journal import /path/to/NinjaTrader_trades.csv \\\n"
          "      --default-stop-points 30\n"
          "  python -m ym review --apply")


if __name__ == "__main__":
    main()
