"""Findings that need the bars.

Each detector must fire on the habit it claims to find and stay quiet when
that habit is absent. A detector that always fires teaches you to ignore the
report, which is worse than not having it."""

import os
import tempfile
import unittest
from datetime import date

from ym.backtest import BacktestConfig, Backtester
from ym.barstore import BarStore
from ym.context_findings import analyse, pair
from ym.data import (
    DiscretionaryHabits, DiscretionaryTrader, filter_session, generate_bars, resample,
)
from ym.instruments import MYM
from ym.market_context import annotate_all
from ym.risk import RiskLimits, SizingMethod

SMALL_ACCOUNT = RiskLimits(
    sizing=SizingMethod.FIXED_DOLLAR, fixed_dollar_risk=10.0, max_contracts=1,
    max_daily_loss_pct=None, max_consecutive_losses=None, max_drawdown_pct=None,
    min_stop_ticks=1,
)

BAD_HABITS = DiscretionaryHabits()            # tight stop, grabs profits, chases
DISCIPLINED = DiscretionaryHabits(
    stop_points=90.0,          # wide enough to sit beyond a level
    target_points=260.0,
    take_early=0.0,            # rides the move
    runner_target_points=260.0,
    at_level=1.0, chase=0.0, fight_level=0.0,
)


def build_report(habits, days=90, seed=5, equity=4_000.0):
    """Simulate a trader against real bars, then read the trades back."""
    minute = filter_session(generate_bars(date(2026, 5, 1), days=days, seed=7), "rth")
    limits = SMALL_ACCOUNT
    if habits.stop_points > 40:
        limits = RiskLimits(
            sizing=SizingMethod.FIXED_DOLLAR, fixed_dollar_risk=60.0,
            max_contracts=1, max_daily_loss_pct=None, max_consecutive_losses=None,
            max_drawdown_pct=None, min_stop_ticks=1,
        )
    trades = Backtester(
        MYM, DiscretionaryTrader(habits, seed=seed), equity, limits,
        BacktestConfig(slippage_ticks=1.0),
    ).run(resample(minute, 5)).trades

    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    os.unlink(handle.name)
    store = BarStore(handle.name)
    try:
        store.add("MYM", minute)
        contexts = annotate_all(trades, store)
    finally:
        store.close()
        if os.path.exists(handle.name):
            os.unlink(handle.name)
    return trades, contexts, analyse(trades, contexts)


class ReportCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trades, cls.contexts, cls.report = build_report(BAD_HABITS)
        cls.codes = {finding.code for finding in cls.report.findings}


class TestPairing(ReportCase):
    def test_trades_are_matched_to_their_context(self):
        pairs, unit, notes = pair(self.trades, self.contexts)
        self.assertGreater(len(pairs), 50)
        self.assertEqual(unit, "R")
        self.assertEqual(notes, [])

    def test_trades_without_bars_are_left_out_of_the_pairs(self):
        from copy import deepcopy
        contexts = deepcopy(self.contexts)
        contexts[0].has_bars = False
        pairs, _, _ = pair(self.trades, contexts)
        self.assertEqual(len(pairs), len(self.contexts) - 1)

    def test_missing_stops_switch_the_unit_to_dollars(self):
        from copy import deepcopy
        trades = deepcopy(self.trades)
        for trade in trades:
            trade.stop_price = None
        _, unit, notes = pair(trades, self.contexts)
        self.assertEqual(unit, "$")
        self.assertTrue(any("Record stops" in note for note in notes))


class TestDetectorsFireOnTheHabits(ReportCase):
    def test_a_stop_inside_the_noise_is_flagged(self):
        self.assertIn("stop_vs_volatility", self.codes)
        finding = next(f for f in self.report.findings
                       if f.code == "stop_vs_volatility")
        self.assertLess(finding.effect, 1.0)
        self.assertEqual(finding.severity, "act")

    def test_risk_that_is_not_structural_is_flagged(self):
        self.assertIn("structural_risk", self.codes)

    def test_taking_profit_early_is_flagged(self):
        self.assertIn("early_exits", self.codes)
        finding = next(f for f in self.report.findings if f.code == "early_exits")
        self.assertLess(finding.effect, 0.6, "should report capturing under 60%")

    def test_shorting_above_a_holding_floor_is_flagged(self):
        self.assertIn("fighting_levels", self.codes)

    def test_the_gap_between_rule_and_practice_is_reported(self):
        self.assertIn("rule_adherence", self.codes)
        finding = next(f for f in self.report.findings if f.code == "rule_adherence")
        self.assertLess(finding.effect, 0.5)

    def test_the_headline_numbers_are_present(self):
        for key in ("at a held level", "stop vs a 5-min bar",
                    "share of the move taken"):
            self.assertIn(key, self.report.headline)

    def test_findings_are_ordered_by_how_much_they_can_be_trusted(self):
        ranks = {"act": 0, "watch": 1, "info": 2}
        severities = [ranks[f.severity] for f in self.report.findings]
        self.assertEqual(severities, sorted(severities))

    def test_the_report_renders(self):
        text = self.report.format_report()
        self.assertIn("Market context review", text)
        self.assertIn("confidence=", text)


class TestDetectorsStayQuietOnDiscipline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trades, cls.contexts, cls.report = build_report(DISCIPLINED)
        cls.codes = {f.code for f in cls.report.findings}

    def test_a_wide_structural_stop_is_not_flagged(self):
        self.assertNotIn("stop_vs_volatility", self.codes)

    def test_riding_the_move_is_not_flagged_as_early(self):
        self.assertNotIn("early_exits", self.codes)

    def test_a_trader_who_never_shorts_is_not_accused_of_fighting_levels(self):
        self.assertNotIn("fighting_levels", self.codes)

    def test_a_trader_who_never_chases_is_not_accused_of_chasing(self):
        chasing = next((f for f in self.report.findings if f.code == "chasing"), None)
        self.assertIsNone(chasing)


class TestThinSamples(unittest.TestCase):
    def test_too_few_trades_produces_no_findings(self):
        trades, contexts, _ = build_report(BAD_HABITS, days=90)
        report = analyse(trades[:5], contexts[:5], min_trades=12)
        self.assertEqual(report.findings, [])
        self.assertTrue(any("below the" in note for note in report.notes))

    def test_the_headline_still_appears_on_a_thin_sample(self):
        trades, contexts, _ = build_report(BAD_HABITS, days=90)
        report = analyse(trades[:5], contexts[:5], min_trades=12)
        self.assertTrue(report.headline)

    def test_no_trades_at_all_is_safe(self):
        report = analyse([], [])
        self.assertEqual(report.findings, [])
        self.assertEqual(report.analysed, 0)
        self.assertIn("Market context review", report.format_report())

    def test_trades_without_bars_are_counted_separately(self):
        from copy import deepcopy
        trades, contexts, _ = build_report(BAD_HABITS, days=90)
        contexts = deepcopy(contexts)
        for context in contexts[:10]:
            context.has_bars = False
        report = analyse(trades, contexts)
        self.assertEqual(report.unanalysed, 10)
        self.assertIn("without bar data", report.format_report())


if __name__ == "__main__":
    unittest.main()
