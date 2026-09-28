"""Reading trades against the bars: trend, location, exit quality.

These tags describe how someone trades, so a wrong one is worse than none --
it would send them to fix a habit they do not have. The scripted price paths
here have known answers."""

import os
import tempfile
import unittest
from datetime import date, timedelta

from tests.test_levels import TRIPLE_REJECTION, WARMUP, build

from ym.barstore import BarStore
from ym.core import Direction, ExitReason, Trade
from ym.data import filter_session, generate_bars, resample
from ym.levels import find_swings
from ym.market_context import (
    AT_LEVEL, CHASING, EXIT_EARLY, EXIT_RODE, EXIT_STOPPED, MID_RANGE,
    TREND_DOWN, TREND_RANGE, TREND_UNKNOWN, TREND_UP, TaggingRules,
    annotate, annotate_all, apply_tags, average_true_range, classify_location,
    excursions, read_trend, run_since_last_swing,
)

# Enough lead-in that ATR and the level tracker are warm before the pattern.
SCRIPTED = WARMUP + WARMUP + WARMUP + TRIPLE_REJECTION + [("leg", 41_100, 40)]
WARMUP_MINUTES = 24 * 3


def scripted_bars():
    return build(SCRIPTED)


def make_trade(bars, minute, entry, stop, exit_minute, exit_price,
               direction=Direction.LONG, reason=None):
    base = bars[0].ts
    trade = Trade("MYM", direction, base + timedelta(minutes=minute), entry, 1, 0.5,
                  stop_price=stop, commission=1.0)
    moved = (exit_price - entry) * direction.sign
    trade.close(
        base + timedelta(minutes=exit_minute), exit_price,
        reason or (ExitReason.TARGET if moved > 0 else ExitReason.STOP),
    )
    return trade


def zigzag(turns, leg_bars=5):
    """A path that turns at each given price, so swings actually form.

    ``turns`` alternates peaks and troughs. A monotonic leg has no interior
    swing -- the turn needs a bar that spikes and closes back, which is what a
    swing high or low is.
    """
    segments = []
    for index, price in enumerate(turns):
        peak = index % 2 == 0
        pullback = price - 40 if peak else price + 40
        segments.append(("leg", price - 25 if peak else price + 25, leg_bars))
        segments.append(("reject", price, pullback))
    return build(segments)


class TestTrendReading(unittest.TestCase):
    def test_rising_swings_read_as_an_uptrend(self):
        bars = zigzag([41_060, 40_960, 41_160, 41_060, 41_260])
        self.assertEqual(read_trend(bars, strength=2), TREND_UP)

    def test_falling_swings_read_as_a_downtrend(self):
        bars = zigzag([41_260, 41_060, 41_160, 40_960, 41_060])
        self.assertEqual(read_trend(bars, strength=2), TREND_DOWN)

    def test_too_little_structure_is_unknown_not_a_guess(self):
        self.assertEqual(
            read_trend(build([("leg", 41_000, 6)]), 2), TREND_UNKNOWN
        )

    def test_a_trend_needs_both_a_higher_high_and_a_higher_low(self):
        # Higher highs but lower lows is a broadening range, not an uptrend.
        bars = zigzag([41_060, 40_960, 41_160, 40_860, 41_260])
        self.assertEqual(read_trend(bars, strength=2), TREND_RANGE)

    def test_the_swings_it_reads_are_the_shared_ones(self):
        bars = zigzag([41_060, 40_960, 41_160, 41_060, 41_260])
        highs, lows = find_swings(bars, 2)
        self.assertGreaterEqual(len(highs), 2)
        self.assertGreaterEqual(len(lows), 2)
        self.assertGreater(highs[-1][1], highs[-2][1])
        self.assertGreater(lows[-1][1], lows[-2][1])


class TestSupportingCalculations(unittest.TestCase):
    def test_atr_needs_a_full_period(self):
        bars = resample(scripted_bars(), 5)
        self.assertIsNone(average_true_range(bars[:5], 14))
        self.assertIsNotNone(average_true_range(bars, 14))

    def test_excursions_are_signed_by_direction(self):
        bars = build([("leg", 41_000, 4), ("reject", 40_940, 41_020), ("leg", 41_060, 4)])
        long_mae, long_mfe = excursions(bars, Direction.LONG, 41_000)
        short_mae, short_mfe = excursions(bars, Direction.SHORT, 41_000)
        self.assertAlmostEqual(long_mae, short_mfe, delta=2)
        self.assertAlmostEqual(long_mfe, short_mae, delta=2)

    def test_excursions_of_nothing_are_zero(self):
        self.assertEqual(excursions([], Direction.LONG, 41_000), (0.0, 0.0))

    def test_the_run_before_entry_is_measured_from_the_last_swing(self):
        bars = resample(scripted_bars(), 5)
        run = run_since_last_swing(bars, Direction.LONG, 41_100, strength=2)
        self.assertIsNotNone(run)
        self.assertGreater(run, 100, "price ran a long way off the floor")

    def test_no_swing_means_no_measurable_run(self):
        bars = resample(build([("leg", 41_100, 12)]), 5)
        self.assertIsNone(run_since_last_swing(bars, Direction.LONG, 41_100, 2))


class TestTagging(unittest.TestCase):
    def setUp(self):
        self.bars = scripted_bars()

    def annotate(self, trade, rules=None):
        return annotate(trade, self.bars, rules or TaggingRules())

    def test_a_long_at_the_held_floor_is_tagged_at_level(self):
        trade = make_trade(self.bars, WARMUP_MINUTES + 46, 40_910, 40_890,
                           WARMUP_MINUTES + 60, 40_950)
        context = self.annotate(trade)
        self.assertTrue(context.has_bars)
        self.assertEqual(context.location, AT_LEVEL)
        self.assertGreaterEqual(context.level_rejections, 2)
        self.assertTrue(context.stop_beyond_level)
        self.assertIn("location:at_level", context.tags())

    def test_a_long_after_a_big_run_is_tagged_chasing(self):
        trade = make_trade(self.bars, WARMUP_MINUTES + 78, 41_060, 41_040,
                           WARMUP_MINUTES + 88, 41_075)
        context = self.annotate(trade)
        self.assertEqual(context.location, CHASING)
        self.assertGreater(context.run_from_swing_atr, 1.5)

    def test_the_stop_distance_rule_needs_the_stop_beyond_the_level(self):
        # Same entry, but the stop sits above the level: being stopped would
        # not mean the level broke, so this is not the setup.
        trade = make_trade(self.bars, WARMUP_MINUTES + 46, 40_910, 40_905,
                           WARMUP_MINUTES + 60, 40_950)
        self.assertNotEqual(self.annotate(trade).location, AT_LEVEL)

    def test_the_atr_zone_rule_is_looser_than_the_stop_rule(self):
        trade = make_trade(self.bars, WARMUP_MINUTES + 46, 40_910, 40_905,
                           WARMUP_MINUTES + 60, 40_950)
        loose = self.annotate(trade, TaggingRules(location_mode="atr_zone"))
        self.assertEqual(loose.location, AT_LEVEL)

    def test_the_two_rules_see_the_same_facts(self):
        trade = make_trade(self.bars, WARMUP_MINUTES + 46, 40_910, 40_890,
                           WARMUP_MINUTES + 60, 40_950)
        tight = self.annotate(trade, TaggingRules(location_mode="stop_distance"))
        loose = self.annotate(trade, TaggingRules(location_mode="atr_zone"))
        self.assertEqual(tight.level_price, loose.level_price)
        self.assertEqual(tight.level_rejections, loose.level_rejections)

    def test_an_invalid_location_mode_is_refused(self):
        with self.assertRaises(ValueError):
            TaggingRules(location_mode="vibes")

    def test_the_stop_is_measured_against_volatility(self):
        trade = make_trade(self.bars, WARMUP_MINUTES + 46, 40_910, 40_890,
                           WARMUP_MINUTES + 60, 40_950)
        context = self.annotate(trade)
        self.assertEqual(context.stop_points, 20)
        self.assertIsNotNone(context.stop_in_atr)
        self.assertIsNotNone(context.stop_vs_bar)

    def test_a_trade_with_no_stop_still_gets_the_other_tags(self):
        trade = make_trade(self.bars, WARMUP_MINUTES + 46, 40_910, None,
                           WARMUP_MINUTES + 60, 40_950)
        context = self.annotate(trade)
        self.assertTrue(context.has_bars)
        self.assertIsNone(context.stop_points)
        self.assertNotEqual(context.trend, TREND_UNKNOWN)

    def test_an_exit_well_before_the_move_ended_is_early(self):
        # Takes 40 points, then price runs on to 41,100.
        trade = make_trade(self.bars, WARMUP_MINUTES + 46, 40_910, 40_890,
                           WARMUP_MINUTES + 56, 40_950)
        context = self.annotate(trade)
        self.assertEqual(context.exit_quality, EXIT_EARLY)
        self.assertGreater(context.forward_points, 0)
        self.assertLess(context.capture_ratio, 0.5)

    def test_riding_most_of_the_move_is_not_early(self):
        trade = make_trade(self.bars, WARMUP_MINUTES + 46, 40_910, 40_890,
                           WARMUP_MINUTES + 115, 41_095)
        context = self.annotate(trade)
        self.assertEqual(context.exit_quality, EXIT_RODE)
        self.assertIsNotNone(context.capture_ratio)
        self.assertGreater(context.capture_ratio, 0.5)
        # No bars after this exit, so the judgement is against its own best.
        self.assertEqual(context.capture_basis, "in_trade")

    def test_a_loss_at_the_stop_is_tagged_stopped(self):
        trade = make_trade(self.bars, WARMUP_MINUTES + 46, 40_910, 40_890,
                           WARMUP_MINUTES + 50, 40_890, reason=ExitReason.STOP)
        self.assertEqual(self.annotate(trade).exit_quality, EXIT_STOPPED)

    def test_direction_is_compared_against_the_trend(self):
        trade = make_trade(self.bars, WARMUP_MINUTES + 46, 40_910, 40_890,
                           WARMUP_MINUTES + 60, 40_950)
        context = self.annotate(trade)
        if context.trend in (TREND_UP, TREND_DOWN):
            self.assertIsInstance(context.with_trend, bool)
            self.assertIn(
                "with-trend" if context.with_trend else "against-trend",
                context.tags(),
            )

    def test_where_in_the_day_the_entry_sat(self):
        trade = make_trade(self.bars, WARMUP_MINUTES + 46, 40_910, 40_890,
                           WARMUP_MINUTES + 60, 40_950)
        context = self.annotate(trade)
        self.assertIsNotNone(context.day_range)
        self.assertGreaterEqual(context.position_in_day_range, 0.0)
        self.assertLessEqual(context.position_in_day_range, 1.0)

    def test_context_is_json_able(self):
        import json
        trade = make_trade(self.bars, WARMUP_MINUTES + 46, 40_910, 40_890,
                           WARMUP_MINUTES + 60, 40_950)
        json.dumps(self.annotate(trade).to_dict())


class TestMissingBars(unittest.TestCase):
    def test_no_bars_at_all_is_reported_not_guessed(self):
        bars = scripted_bars()
        trade = make_trade(bars, 40, 40_910, 40_890, 60, 40_950)
        context = annotate(trade, [], TaggingRules())
        self.assertFalse(context.has_bars)
        self.assertIn("no bar data", context.note)
        self.assertEqual(context.tags(), [])

    def test_too_little_history_before_the_entry_is_reported(self):
        bars = scripted_bars()
        trade = make_trade(bars, 3, 40_910, 40_890, 20, 40_950)
        context = annotate(trade, bars[:6], TaggingRules())
        self.assertFalse(context.has_bars)
        self.assertIn("not enough bars", context.note)


class TestAnnotateAll(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        os.unlink(handle.name)
        self.path = handle.name
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.minute = filter_session(
            generate_bars(date(2026, 6, 1), days=40, seed=11), "rth"
        )
        self.store = BarStore(self.path)
        self.addCleanup(self.store.close)
        self.store.add("MYM", self.minute)

    def traded(self):
        """Trades that genuinely align with the stored bars."""
        from ym.backtest import BacktestConfig, Backtester
        from ym.instruments import MYM
        from ym.risk import RiskLimits
        from ym.strategies import SupportRejection

        return Backtester(
            MYM, SupportRejection(min_rejections=2), 25_000,
            RiskLimits(risk_per_trade_pct=1.0), BacktestConfig(slippage_ticks=1.0),
        ).run(resample(self.minute, 5)).trades

    def test_every_trade_with_bars_is_annotated(self):
        trades = self.traded()
        self.assertGreater(len(trades), 5)
        contexts = annotate_all(trades, self.store)
        self.assertEqual(len(contexts), len(trades))
        self.assertTrue(all(context.has_bars for context in contexts),
                        [c.note for c in contexts if not c.has_bars][:3])

    def test_results_come_back_in_time_order(self):
        contexts = annotate_all(self.traded(), self.store)
        times = [context.entry_time for context in contexts]
        self.assertEqual(times, sorted(times))

    def test_trades_outside_the_bar_range_are_flagged_not_dropped(self):
        trades = self.traded()
        stray = trades[0]
        from copy import deepcopy
        orphan = deepcopy(stray)
        orphan.entry_time = orphan.entry_time.replace(year=2020)
        orphan.exit_time = orphan.exit_time.replace(year=2020)
        contexts = annotate_all([orphan, *trades], self.store)
        self.assertEqual(len(contexts), len(trades) + 1)
        self.assertFalse(contexts[0].has_bars)

    def test_tags_are_written_onto_the_trades(self):
        trades = self.traded()
        contexts = annotate_all(trades, self.store)
        changed = apply_tags(trades, contexts)
        self.assertEqual(changed, sum(1 for c in contexts if c.has_bars))
        self.assertEqual(changed, len(trades))
        self.assertTrue(any(tag.startswith("location:") for tag in trades[0].tags))

    def test_re_tagging_does_not_pile_up_duplicates(self):
        trades = self.traded()
        contexts = annotate_all(trades, self.store)
        apply_tags(trades, contexts)
        first = list(trades[0].tags)
        apply_tags(trades, contexts)
        self.assertEqual(trades[0].tags, first)

    def test_tags_you_wrote_yourself_are_preserved(self):
        trades = self.traded()
        trades[0].tags.append("unplanned")
        contexts = annotate_all(trades, self.store)
        apply_tags(trades, contexts)
        self.assertIn("unplanned", trades[0].tags)

    def test_a_trade_without_bars_keeps_its_own_tags_untouched(self):
        from copy import deepcopy
        orphan = deepcopy(self.traded()[0])
        orphan.entry_time = orphan.entry_time.replace(year=2020)
        orphan.exit_time = orphan.exit_time.replace(year=2020)
        orphan.tags = ["mine"]
        apply_tags([orphan], annotate_all([orphan], self.store))
        self.assertEqual(orphan.tags, ["mine"])


if __name__ == "__main__":
    unittest.main()
