"""The sweep: it must split honestly and refuse to dress noise up as a result."""

import unittest
from datetime import date

from ym.backtest import BacktestConfig
from ym.data import filter_session, generate_bars, resample
from ym.instruments import MYM
from ym.risk import RiskLimits
from ym.sessions import DEFAULT_SESSION
from ym.strategies import OpeningRangeBreakout, SupportRejection
from ym.sweep import Slice, split_bars, sweep

LIMITS = RiskLimits(risk_per_trade_pct=0.5, max_daily_loss_pct=2.0)
CONFIG = BacktestConfig(slippage_ticks=1.0)


def five_minute_bars(days=80, seed=11):
    return resample(
        filter_session(generate_bars(date(2026, 3, 2), days=days, seed=seed), "rth"), 5
    )


class TestSplitting(unittest.TestCase):
    def setUp(self):
        self.bars = five_minute_bars(days=40)

    def test_split_is_chronological_and_complete(self):
        first, second = split_bars(self.bars)
        self.assertEqual(len(first) + len(second), len(self.bars))
        self.assertLess(first[-1].ts, second[0].ts)

    def test_split_never_cuts_a_session_in_half(self):
        first, second = split_bars(self.bars)
        first_days = {DEFAULT_SESSION.session_day(bar.ts) for bar in first}
        second_days = {DEFAULT_SESSION.session_day(bar.ts) for bar in second}
        self.assertEqual(first_days & second_days, set())

    def test_fraction_moves_the_boundary(self):
        early, _ = split_bars(self.bars, 0.25)
        half, _ = split_bars(self.bars, 0.5)
        self.assertLess(len(early), len(half))

    def test_both_halves_always_have_bars(self):
        for fraction in (0.01, 0.5, 0.99):
            with self.subTest(fraction=fraction):
                first, second = split_bars(self.bars, fraction)
                self.assertTrue(first)
                self.assertTrue(second)

    def test_invalid_fractions_and_short_series_are_rejected(self):
        with self.assertRaises(ValueError):
            split_bars(self.bars, 0.0)
        with self.assertRaises(ValueError):
            split_bars(self.bars, 1.0)
        one_day = [
            bar for bar in self.bars
            if DEFAULT_SESSION.session_day(bar.ts)
            == DEFAULT_SESSION.session_day(self.bars[0].ts)
        ]
        with self.assertRaises(ValueError):
            split_bars(one_day)


class TestUncertainty(unittest.TestCase):
    def test_a_wide_standard_error_is_not_significant(self):
        piece = Slice(metrics=type("M", (), {"expectancy_r": 0.05})(), blocked=0,
                      expectancy_r_se=0.20)
        self.assertFalse(piece.is_significant)

    def test_a_clear_edge_is_significant(self):
        piece = Slice(metrics=type("M", (), {"expectancy_r": 0.50})(), blocked=0,
                      expectancy_r_se=0.10)
        self.assertTrue(piece.is_significant)

    def test_no_standard_error_means_no_claim(self):
        piece = Slice(metrics=type("M", (), {"expectancy_r": 0.50})(), blocked=0,
                      expectancy_r_se=None)
        self.assertFalse(piece.is_significant)

    def test_the_sweep_computes_a_standard_error_per_row(self):
        report = sweep(
            MYM, SupportRejection, "min_rejections", [2, 3], five_minute_bars(days=60),
            limits=LIMITS, config=CONFIG,
        )
        for row in report.rows:
            if row.full.metrics.trades >= 3:
                self.assertIsNotNone(row.full.expectancy_r_se)
                self.assertGreater(row.full.expectancy_r_se, 0)


class TestSweep(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = five_minute_bars(days=80)
        cls.report = sweep(
            MYM, SupportRejection, "min_rejections", [2, 3, 4], cls.bars,
            limits=LIMITS, config=CONFIG, split=True,
        )

    def test_one_row_per_value(self):
        self.assertEqual([row.value for row in self.report.rows], [2, 3, 4])

    def test_each_row_carries_all_three_slices_when_split(self):
        for row in self.report.rows:
            self.assertIsNotNone(row.full)
            self.assertIsNotNone(row.train)
            self.assertIsNotNone(row.test)

    def test_the_halves_sum_to_roughly_the_whole(self):
        for row in self.report.rows:
            combined = row.train.metrics.trades + row.test.metrics.trades
            # Not exact: the halves each start flat, so a trade spanning the
            # boundary and the risk state differ. But they must be comparable.
            self.assertGreater(combined, 0)
            self.assertLess(abs(combined - row.full.metrics.trades),
                            max(10, row.full.metrics.trades * 0.5))

    def test_different_values_produce_different_results(self):
        counts = {row.value: row.full.metrics.trades for row in self.report.rows}
        self.assertGreater(len(set(counts.values())), 1,
                           f"the parameter had no effect: {counts}")

    def test_more_rejections_means_fewer_opportunities(self):
        counts = [row.full.metrics.trades for row in self.report.rows]
        self.assertEqual(counts, sorted(counts, reverse=True),
                         "demanding more tests of a level should find fewer setups")

    def test_best_and_ranking(self):
        best = self.report.best("train")
        self.assertIn(best.value, [2, 3, 4])
        ranking = self.report.ranking("test")
        self.assertEqual(sorted(ranking), sorted(r.value for r in self.report.rows))

    def test_the_report_shows_both_halves_and_a_verdict(self):
        text = self.report.format_report()
        self.assertIn("FIRST HALF (in sample)", text)
        self.assertIn("SECOND HALF (out of sample)", text)
        self.assertIn("Reading this:", text)
        self.assertIn("configurations tried", text)
        self.assertIn("standard error", text)

    def test_the_report_names_the_strategy_class_not_the_last_value(self):
        self.assertEqual(self.report.strategy, "SupportRejection")

    def test_an_unsplit_report_warns_about_picking_a_winner(self):
        report = sweep(
            MYM, SupportRejection, "min_rejections", [2, 3], self.bars,
            limits=LIMITS, config=CONFIG, split=False,
        )
        text = report.format_report()
        self.assertIn("ALL DATA", text)
        self.assertIn("--split", text)
        self.assertNotIn("SECOND HALF", text)

    def test_noise_is_reported_as_noise(self):
        # Synthetic bars are a random walk, so nothing here should clear two
        # standard errors. If this ever starts failing, suspect the statistics
        # before celebrating the strategy.
        text = self.report.format_report()
        self.assertIn("within noise", text)

    def test_fixed_parameters_are_held_across_the_sweep(self):
        report = sweep(
            MYM, SupportRejection, "min_rejections", [2, 3], self.bars,
            limits=LIMITS, config=CONFIG,
            fixed_params={"target_r": 3.0, "stop_buffer_atr": 0.5},
        )
        self.assertEqual(report.fixed_params["target_r"], 3.0)
        self.assertIn("target_r=3.0", report.format_report())

    def test_sweeping_a_different_parameter(self):
        report = sweep(
            MYM, SupportRejection, "target_r", [1.5, 2.0, 3.0], self.bars,
            limits=LIMITS, config=CONFIG,
        )
        self.assertEqual(report.param, "target_r")
        self.assertEqual(len(report.rows), 3)

    def test_it_works_on_another_strategy(self):
        report = sweep(
            MYM, OpeningRangeBreakout, "range_minutes", [15, 30, 60],
            five_minute_bars(days=40), limits=LIMITS, config=CONFIG,
        )
        self.assertEqual(report.strategy, "OpeningRangeBreakout")
        self.assertEqual(len(report.rows), 3)

    def test_an_unusable_value_is_reported_clearly(self):
        with self.assertRaises(ValueError) as caught:
            sweep(MYM, SupportRejection, "min_rejections", [1], self.bars,
                  limits=LIMITS, config=CONFIG)
        self.assertIn("min_rejections=1", str(caught.exception))

    def test_an_empty_value_list_is_rejected(self):
        with self.assertRaises(ValueError):
            sweep(MYM, SupportRejection, "min_rejections", [], self.bars)

    def test_an_unknown_parameter_is_reported_clearly(self):
        with self.assertRaises(ValueError):
            sweep(MYM, SupportRejection, "not_a_parameter", [1, 2], self.bars,
                  limits=LIMITS, config=CONFIG)


if __name__ == "__main__":
    unittest.main()
