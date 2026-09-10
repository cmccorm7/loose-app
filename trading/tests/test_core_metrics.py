"""Trade arithmetic and the performance statistics built on it."""

import unittest
from datetime import datetime, timedelta

from ym.core import Direction, ExitReason, Trade
from ym.metrics import compute_metrics, daily_pnl, equity_curve, format_breakdown
from ym.sessions import exchange_tz

ET = exchange_tz()


def make_trade(
    pnl_points, contracts=1, stop_points=20, direction=Direction.LONG,
    hour=10, day=8, commission=4.0, setup="test",
):
    entry_time = datetime(2026, 9, day, hour, 0, tzinfo=ET)
    entry = 41000.0
    trade = Trade(
        "YM", direction, entry_time, entry, contracts, 5.0,
        stop_price=entry - direction.sign * stop_points,
        commission=commission, setup=setup,
    )
    trade.close(
        entry_time + timedelta(minutes=10),
        entry + direction.sign * pnl_points,
        ExitReason.TARGET if pnl_points > 0 else ExitReason.STOP,
    )
    return trade


class TestTradeArithmetic(unittest.TestCase):
    def test_long_pnl(self):
        trade = make_trade(40, contracts=2)
        self.assertEqual(trade.points, 40)
        self.assertEqual(trade.gross_pnl, 400.0)
        self.assertEqual(trade.net_pnl, 396.0)

    def test_short_pnl_is_signed_correctly(self):
        trade = make_trade(40, direction=Direction.SHORT)
        self.assertEqual(trade.points, 40)
        self.assertGreater(trade.net_pnl, 0)
        self.assertLess(trade.exit_price, trade.entry_price)

    def test_r_multiple_uses_net_pnl_and_initial_risk(self):
        trade = make_trade(40, contracts=1, stop_points=20, commission=0.0)
        self.assertEqual(trade.risk_dollars, 100.0)
        self.assertAlmostEqual(trade.r_multiple, 2.0)

    def test_r_multiple_is_none_without_a_stop(self):
        trade = make_trade(40)
        trade.stop_price = None
        self.assertIsNone(trade.r_multiple)

    def test_excursions_track_the_worst_and_best(self):
        trade = make_trade(40)
        trade.update_excursions(high=41030, low=40990)
        trade.update_excursions(high=41050, low=40995)
        self.assertEqual(trade.mae_points, 10)
        self.assertEqual(trade.mfe_points, 50)

    def test_short_excursions_invert(self):
        trade = make_trade(40, direction=Direction.SHORT)
        trade.update_excursions(high=41030, low=40990)
        self.assertEqual(trade.mae_points, 30)
        self.assertEqual(trade.mfe_points, 10)

    def test_planned_reward_risk(self):
        trade = make_trade(40)
        trade.target_price = 41060
        self.assertAlmostEqual(trade.planned_r_multiple, 3.0)

    def test_open_trade_reports_no_result(self):
        trade = Trade("YM", Direction.LONG, datetime(2026, 9, 8, 10, tzinfo=ET),
                      41000, 1, 5.0, stop_price=40980)
        self.assertFalse(trade.is_closed)
        self.assertIsNone(trade.r_multiple)
        self.assertEqual(trade.points, 0.0)


class TestMetrics(unittest.TestCase):
    def setUp(self):
        # Two winners at +2R, three losers at -1R, commission free.
        self.trades = (
            [make_trade(40, commission=0.0) for _ in range(2)]
            + [make_trade(-20, commission=0.0) for _ in range(3)]
        )

    def test_counts_and_win_rate(self):
        metrics = compute_metrics(self.trades, 25000)
        self.assertEqual(metrics.trades, 5)
        self.assertEqual(metrics.wins, 2)
        self.assertEqual(metrics.losses, 3)
        self.assertAlmostEqual(metrics.win_rate, 40.0)

    def test_expectancy_in_dollars_and_r(self):
        metrics = compute_metrics(self.trades, 25000)
        self.assertAlmostEqual(metrics.net_pnl, 2 * 200 - 3 * 100)
        self.assertAlmostEqual(metrics.expectancy, 100 / 5)
        self.assertAlmostEqual(metrics.expectancy_r, (2 * 2 - 3 * 1) / 5)

    def test_profit_factor(self):
        metrics = compute_metrics(self.trades, 25000)
        self.assertAlmostEqual(metrics.profit_factor, 400 / 300)

    def test_profit_factor_is_infinite_with_no_losses(self):
        metrics = compute_metrics([make_trade(40, commission=0.0)], 25000)
        self.assertEqual(metrics.profit_factor, float("inf"))

    def test_empty_input_is_safe(self):
        metrics = compute_metrics([], 25000)
        self.assertEqual(metrics.trades, 0)
        self.assertEqual(metrics.final_equity, 25000)
        self.assertIn("Trades", metrics.format_report())

    def test_drawdown_measures_peak_to_trough(self):
        ordered = [make_trade(40, commission=0.0)] + [
            make_trade(-20, commission=0.0) for _ in range(3)
        ]
        metrics = compute_metrics(ordered, 10000)
        self.assertAlmostEqual(metrics.max_drawdown, 300.0)
        self.assertAlmostEqual(metrics.max_drawdown_pct, 300.0 / 10200 * 100)

    def test_streaks(self):
        metrics = compute_metrics(self.trades, 25000)
        self.assertEqual(metrics.max_consecutive_wins, 2)
        self.assertEqual(metrics.max_consecutive_losses, 3)

    def test_equity_curve_starts_at_starting_equity(self):
        points = equity_curve(self.trades, 25000)
        self.assertEqual(points[0], ("start", 25000))
        self.assertAlmostEqual(points[-1][1], 25100)

    def test_daily_pnl_groups_by_trading_day(self):
        trades = [make_trade(40, day=8, commission=0.0),
                  make_trade(-20, day=9, commission=0.0)]
        totals = daily_pnl(trades)
        self.assertEqual(len(totals), 2)
        self.assertAlmostEqual(sum(totals.values()), 100.0)

    def test_breakdown_renders_each_bucket(self):
        trades = [make_trade(40, setup="A", commission=0.0),
                  make_trade(-20, setup="B", commission=0.0)]
        table = format_breakdown(trades, "setup")
        self.assertIn("A", table)
        self.assertIn("B", table)

    def test_breakdown_rejects_unknown_grouping(self):
        with self.assertRaises(KeyError):
            format_breakdown(self.trades, "phase_of_moon")


if __name__ == "__main__":
    unittest.main()
