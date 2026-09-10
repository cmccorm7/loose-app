"""The behavioral detectors: they must fire on planted habits and stay quiet
on a disciplined trader. Both halves matter -- a detector that always fires is
worse than none, because it teaches you to ignore the report."""

import unittest
from datetime import datetime, time

from ym.behavior import (
    adjust_p, analyze, build_days, build_features, detect_giveback,
    detect_ordinal_decay, detect_revenge_window, detect_size_escalation,
    detect_stop_discipline, detect_time_of_day, welch,
)
from ym.core import Direction, Trade
from ym.data.trader import Habits, simulate_trader_history
from ym.instruments import MYM
from ym.risk import RiskLimits
from ym.sessions import DEFAULT_SESSION, exchange_tz

ET = exchange_tz()

DISCIPLINED = Habits(
    early_edge_r=0.05, late_edge_r=0.05, edge_decays_after=99,
    revenge_probability=0.0, revenge_size_multiplier=1.0, stop_overshoot=0.0,
)
FADES_AFTER_TWO = Habits(
    early_edge_r=0.4, late_edge_r=-0.5, edge_decays_after=2,
    revenge_probability=0.65, stop_overshoot=0.45, unplanned_probability=0.2,
)


class TestStatistics(unittest.TestCase):
    def test_welch_detects_a_real_difference(self):
        import random
        rng = random.Random(1)
        winners = [rng.gauss(1.0, 0.5) for _ in range(40)]
        losers = [rng.gauss(-1.0, 0.5) for _ in range(40)]
        t, p = welch(winners, losers)
        self.assertIsNotNone(t)
        self.assertGreater(t, 0)
        self.assertLess(p, 0.001)

    def test_welch_sees_no_difference_between_like_samples(self):
        import random
        rng = random.Random(2)
        a = [rng.gauss(0.0, 1.0) for _ in range(40)]
        b = [rng.gauss(0.0, 1.0) for _ in range(40)]
        _, p = welch(a, b)
        self.assertGreater(p, 0.05)

    def test_welch_is_unbothered_by_identical_samples(self):
        self.assertEqual(welch([1.0] * 10, [1.0] * 10), (None, None))

    def test_welch_needs_two_observations_a_side(self):
        self.assertEqual(welch([1.0], [2.0, 3.0]), (None, None))

    def test_p_adjustment_scales_with_the_number_of_comparisons(self):
        self.assertAlmostEqual(adjust_p(0.01, 5), 0.05)
        self.assertEqual(adjust_p(0.5, 10), 1.0)
        self.assertIsNone(adjust_p(None, 5))
        self.assertAlmostEqual(adjust_p(0.02, 0), 0.02)


class TestFeatures(unittest.TestCase):
    def setUp(self):
        self.trades = simulate_trader_history(days=20, habits=FADES_AFTER_TWO, seed=3)
        self.features, self.unit, self.notes = build_features(self.trades)

    def test_unit_is_r_when_stops_are_recorded(self):
        self.assertEqual(self.unit, "R")
        self.assertEqual(self.notes, [])

    def test_unit_falls_back_to_dollars_without_stops(self):
        stripped = []
        for trade in self.trades:
            trade.stop_price = None
            stripped.append(trade)
        _, unit, notes = build_features(stripped)
        self.assertEqual(unit, "$")
        self.assertTrue(any("stop" in note for note in notes))

    def test_ordinals_restart_each_day(self):
        by_day = {}
        for feature in self.features:
            by_day.setdefault(feature.session_day, []).append(feature.ordinal)
        for ordinals in by_day.values():
            self.assertEqual(ordinals, list(range(1, len(ordinals) + 1)))

    def test_prior_loss_and_gap_are_tracked(self):
        followers = [f for f in self.features if f.ordinal > 1]
        self.assertTrue(all(f.minutes_since_prior_exit is not None for f in followers))
        self.assertTrue(any(f.prior_was_loss for f in followers))

    def test_first_trade_of_the_day_has_no_prior(self):
        firsts = [f for f in self.features if f.ordinal == 1]
        self.assertTrue(all(f.minutes_since_prior_exit is None for f in firsts))
        self.assertTrue(all(not f.prior_was_loss for f in firsts))

    def test_day_records_capture_the_intraday_peak(self):
        days = build_days(self.features)
        self.assertTrue(days)
        for record in days:
            self.assertGreaterEqual(record.peak_pnl, record.net_pnl - 1e-9)
            self.assertGreaterEqual(record.giveback, 0.0)


class TestDetectorsFireOnPlantedHabits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trades = simulate_trader_history(days=60, habits=FADES_AFTER_TWO, seed=42)
        cls.report = analyze(cls.trades)
        cls.codes = {finding.code for finding in cls.report.findings}

    def test_the_edge_decaying_after_two_trades_is_found(self):
        self.assertIn("ordinal_decay", self.codes)
        finding = next(f for f in self.report.findings if f.code == "ordinal_decay")
        self.assertEqual(finding.guardrail["max_trades_per_day"], 2)
        self.assertLess(finding.effect, 0)

    def test_the_afternoon_fade_is_found(self):
        self.assertIn("time_of_day_decay", self.codes)
        finding = next(f for f in self.report.findings if f.code == "time_of_day_decay")
        self.assertIsInstance(finding.guardrail["hard_stop_time"], time)

    def test_fast_re_entry_after_a_loss_is_found(self):
        self.assertIn("revenge_window", self.codes)

    def test_sizing_up_after_a_loss_is_found(self):
        self.assertIn("size_escalation", self.codes)
        finding = next(f for f in self.report.findings if f.code == "size_escalation")
        self.assertEqual(finding.effect_unit, " contracts")
        self.assertGreater(finding.effect, 0)

    def test_losses_running_past_the_stop_are_found(self):
        self.assertIn("stop_discipline", self.codes)

    def test_guardrails_merge_into_usable_limits(self):
        limits = self.report.suggested_limits(RiskLimits(risk_per_trade_pct=0.5))
        self.assertEqual(limits.max_trades_per_day, 2)
        self.assertIsNotNone(limits.hard_stop_time)
        self.assertEqual(limits.risk_per_trade_pct, 0.5)  # untouched settings survive

    def test_better_evidenced_guardrails_win_conflicts(self):
        # Both the ordinal and overtrading detectors want max_trades_per_day.
        # The one with more observations behind it should set the value.
        guardrails = self.report.guardrails()
        ordinal = next(
            (f for f in self.report.findings if f.code == "ordinal_decay"), None
        )
        self.assertIsNotNone(ordinal)
        self.assertEqual(
            guardrails["max_trades_per_day"], ordinal.guardrail["max_trades_per_day"]
        )

    def test_findings_are_ordered_by_how_much_they_can_be_trusted(self):
        severities = [f.severity for f in self.report.findings]
        ranks = {"act": 0, "watch": 1, "info": 2}
        self.assertEqual(severities, sorted(severities, key=lambda s: ranks[s]))

    def test_the_report_renders_with_units_and_caveats(self):
        text = self.report.format_report()
        self.assertIn("Behavioral review", text)
        self.assertIn("hypotheses", text)
        self.assertIn("confidence=", text)


class TestDetectorsStayQuietOnDiscipline(unittest.TestCase):
    def test_a_disciplined_trader_raises_few_flags(self):
        total = 0
        for seed in range(1, 11):
            report = analyze(
                simulate_trader_history(days=60, habits=DISCIPLINED, seed=seed)
            )
            total += len(report.actionable())
        # Ten reviews, twelve detectors each: a handful of moderate flags is the
        # cost of the thresholds, but it must not be one per review per detector.
        self.assertLess(total, 15, f"too many false positives: {total}")

    def test_no_habit_specific_detector_fires_every_time(self):
        from collections import Counter
        counts = Counter()
        for seed in range(1, 11):
            report = analyze(
                simulate_trader_history(days=60, habits=DISCIPLINED, seed=seed)
            )
            counts.update(f.code for f in report.actionable())
        for code in ("ordinal_decay", "size_escalation", "stop_discipline"):
            self.assertLessEqual(counts[code], 3, f"{code} fired {counts[code]}/10")


class TestDetectorEdgeCases(unittest.TestCase):
    def test_too_few_trades_produces_no_findings(self):
        report = analyze(simulate_trader_history(days=3, habits=FADES_AFTER_TWO))
        self.assertEqual(report.findings, [])
        self.assertTrue(any("minimum" in note for note in report.notes))

    def test_empty_input_is_safe(self):
        report = analyze([])
        self.assertEqual(report.trades, 0)
        self.assertEqual(report.guardrails(), {})
        self.assertIn("minimum", report.format_report())

    def test_suggested_limits_without_findings_returns_the_base_unchanged(self):
        base = RiskLimits(risk_per_trade_pct=0.75)
        self.assertEqual(analyze([]).suggested_limits(base), base)

    def test_detectors_return_none_when_a_slice_is_below_the_minimum(self):
        trades = simulate_trader_history(days=2, habits=FADES_AFTER_TWO)
        features, unit, _ = build_features(trades)
        for detector in (
            detect_ordinal_decay, detect_revenge_window,
            detect_size_escalation, detect_stop_discipline,
        ):
            with self.subTest(detector=detector.__name__):
                self.assertIsNone(detector(features, unit))
        self.assertIsNone(detect_time_of_day(features, unit, DEFAULT_SESSION))
        self.assertIsNone(detect_giveback(build_days(features)))

    def test_thin_sample_findings_never_become_guardrails(self):
        # Detectors called on a short history can still speak, but at "info"
        # severity -- and only actionable findings may set a guardrail.
        trades = simulate_trader_history(days=6, habits=FADES_AFTER_TWO, seed=3)
        features, unit, _ = build_features(trades)
        thin = [
            finding
            for finding in (
                detect_ordinal_decay(features, unit),
                detect_time_of_day(features, unit, DEFAULT_SESSION),
                detect_size_escalation(features, unit),
            )
            if finding is not None
        ]
        self.assertTrue(thin, "expected at least one low-confidence finding")
        for finding in thin:
            with self.subTest(code=finding.code):
                self.assertEqual(finding.confidence, "low")
                self.assertEqual(finding.severity, "info")
        # The invariant that matters: a short history must not reconfigure your
        # risk limits. A detector may still say something (stop discipline, for
        # one, is readable off few trades) as long as it sets no guardrail.
        report = analyze(trades, min_trades=1)
        self.assertEqual(report.guardrails(), {})
        for finding in report.actionable():
            with self.subTest(code=finding.code):
                self.assertEqual(finding.guardrail, {})

    def test_open_trades_are_ignored(self):
        closed = simulate_trader_history(days=30, habits=FADES_AFTER_TWO)
        open_trade = Trade("MYM", Direction.LONG, datetime(2026, 9, 8, 10, tzinfo=ET),
                           41000, 1, 0.5, stop_price=40980)
        report = analyze(closed + [open_trade])
        self.assertEqual(report.trades, len(closed))

    def test_dollar_mode_skips_the_r_only_detectors(self):
        trades = simulate_trader_history(days=60, habits=FADES_AFTER_TWO, seed=42)
        for trade in trades:
            trade.stop_price = None
        report = analyze(trades)
        self.assertEqual(report.unit, "$")
        codes = {finding.code for finding in report.findings}
        self.assertNotIn("stop_discipline", codes)
        self.assertNotIn("winners_round_trip", codes)
        self.assertIn("ordinal_decay", codes)


if __name__ == "__main__":
    unittest.main()
