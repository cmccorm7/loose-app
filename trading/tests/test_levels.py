"""Support/resistance detection. The central property is no lookahead: a level
cannot exist before the bars that prove it."""

import unittest
from datetime import datetime, timedelta

from ym.core import Bar
from ym.levels import LevelTracker
from ym.sessions import exchange_tz

ET = exchange_tz()
START = datetime(2026, 9, 8, 9, 30, tzinfo=ET)


def build(segments, start=START):
    """Build bars from a script.

    ``("leg", target, count)`` walks linearly to ``target``.
    ``("reject", extreme, close)`` is a single bar that spikes to ``extreme``
    and closes back at ``close`` -- a rejection bar.
    ``("break", close)`` is a single bar closing decisively through.
    """
    specs = []
    price = None
    for segment in segments:
        if segment[0] == "reject":
            _, extreme, close = segment
            open_ = price if price is not None else close
            if extreme < min(open_, close):
                specs.append((open_, max(open_, close) + 1, extreme, close))
            else:
                specs.append((open_, extreme, min(open_, close) - 1, close))
            price = close
        elif segment[0] == "break":
            _, close = segment
            open_ = price if price is not None else close
            specs.append((open_, max(open_, close) + 1, min(open_, close) - 1, close))
            price = close
        else:
            _, target, count = segment
            begin = price if price is not None else target
            for i in range(count):
                o = begin + (target - begin) * i / count
                c = begin + (target - begin) * (i + 1) / count
                specs.append((o, max(o, c) + 1, min(o, c) - 1, c))
            price = target
    return [
        Bar(start + timedelta(minutes=i), round(o), round(h), round(l), round(c), 100)
        for i, (o, h, l, c) in enumerate(specs)
    ]


# The bare pattern. Level tests use it directly with a fixed tolerance, so the
# bar indices below are exact and the "confirmed N bars later" assertions mean
# something.
TRIPLE_REJECTION = [
    ("leg", 40_960, 6),
    ("reject", 40_900, 40_935),
    ("leg", 40_985, 6),
    ("reject", 40_903, 40_940),
    ("leg", 40_975, 6),
    ("reject", 40_898, 40_945),
    ("leg", 41_020, 8),
]


# Lead-in bars for tests that use an ATR-scaled tolerance: until ATR has warmed
# up, the tolerance is zero and no level can form at all.
WARMUP = [
    ("leg", 41_060, 8),
    ("leg", 41_005, 8),
    ("leg", 41_045, 8),
]


def feed(tracker, bars, atr=20.0, upto=None):
    """Observe every bar, returning the bar indices that produced a rejection."""
    crossings = []
    for index in range(len(bars) if upto is None else upto):
        tracker.observe(bars, index, atr=atr)
        for level in tracker.touched_on(index, "support"):
            crossings.append((index, level.rejections))
    return crossings


class TestPivots(unittest.TestCase):
    def test_a_level_appears_only_after_the_pivot_is_confirmed(self):
        bars = build(TRIPLE_REJECTION)
        tracker = LevelTracker(pivot_strength=3, tolerance_points=12)
        # The first swing low is bar 6; it cannot be known until bar 9.
        for index in range(9):
            tracker.observe(bars, index, atr=20.0)
        self.assertEqual(tracker.active("support"), [])
        tracker.observe(bars, 9, atr=20.0)
        self.assertEqual(len(tracker.active("support")), 1)

    def test_the_level_created_is_at_the_swing_low(self):
        bars = build(TRIPLE_REJECTION)
        tracker = LevelTracker(pivot_strength=3, tolerance_points=12)
        feed(tracker, bars, upto=10)
        level = tracker.active("support")[0]
        self.assertAlmostEqual(level.price, 40_900, delta=2)
        self.assertEqual(level.created_index, 6)

    def test_a_stronger_pivot_requirement_finds_fewer_levels(self):
        bars = build(TRIPLE_REJECTION)
        loose = LevelTracker(pivot_strength=2, tolerance_points=5)
        strict = LevelTracker(pivot_strength=6, tolerance_points=5)
        feed(loose, bars)
        feed(strict, bars)
        self.assertGreaterEqual(
            len(loose.active()) + len(loose.broken_levels),
            len(strict.active()) + len(strict.broken_levels),
        )

    def test_flat_bases_do_not_register_as_many_pivots(self):
        flat = [Bar(START + timedelta(minutes=i), 41_000, 41_005, 40_995, 41_000, 100)
                for i in range(30)]
        tracker = LevelTracker(pivot_strength=3, tolerance_points=10)
        feed(tracker, flat)
        self.assertEqual(tracker.active("support"), [])


class TestRejectionCounting(unittest.TestCase):
    def setUp(self):
        self.bars = build(TRIPLE_REJECTION)
        self.tracker = LevelTracker(
            pivot_strength=3, tolerance_points=12, min_bars_between_touches=3
        )
        self.crossings = feed(self.tracker, self.bars)
        self.level = self.tracker.active("support")[0]

    def test_three_rejections_are_counted(self):
        self.assertEqual(self.level.rejections, 3)

    def test_the_forming_low_counts_as_the_first_rejection(self):
        first = self.level.touches[0]
        self.assertEqual(first.bar_index, 6)
        self.assertAlmostEqual(first.extreme, 40_900, delta=1)

    def test_later_rejections_are_news_on_their_own_bar(self):
        reported = [count for _, count in self.crossings]
        self.assertIn(2, reported)
        self.assertIn(3, reported)

    def test_the_floor_is_the_lowest_price_the_level_has_seen(self):
        self.assertAlmostEqual(self.level.floor, 40_898, delta=1)
        self.assertLess(self.level.floor, self.level.price)

    def test_all_three_pivots_cluster_into_one_level(self):
        self.assertEqual(len(self.tracker.active("support")), 1)
        self.assertEqual(len(self.level.pivots), 3)

    def test_a_tight_tolerance_keeps_them_apart(self):
        tracker = LevelTracker(pivot_strength=3, tolerance_points=1)
        feed(tracker, self.bars)
        self.assertGreater(len(tracker.active("support")), 1)

    def test_consecutive_bars_in_the_zone_count_once(self):
        bars = build([
            ("leg", 40_960, 6),
            ("reject", 40_900, 40_935),
            ("leg", 40_910, 4),
            ("reject", 40_902, 40_906),   # chopping on the level
            ("reject", 40_901, 40_907),
            ("reject", 40_903, 40_908),
            ("leg", 41_000, 6),
        ])
        tracker = LevelTracker(
            pivot_strength=3, tolerance_points=12, min_bars_between_touches=5
        )
        feed(tracker, bars)
        level = max(tracker.active("support") + tracker.broken_levels,
                    key=lambda lvl: lvl.rejections)
        self.assertLess(level.rejections, 4, "a single consolidation was over-counted")

    def test_a_touch_confirmed_a_bar_later_still_counts(self):
        bars = build([
            ("leg", 40_960, 6),
            ("reject", 40_900, 40_935),
            ("leg", 40_990, 6),
            ("leg", 40_905, 4),           # drifts into the zone, closes inside it
            ("leg", 40_960, 4),           # then closes back above
        ])
        tracker = LevelTracker(
            pivot_strength=3, tolerance_points=12, min_bars_between_touches=3,
            confirm_within_bars=4,
        )
        feed(tracker, bars)
        level = tracker.active("support")[0]
        self.assertGreaterEqual(level.rejections, 2)


class TestBreaks(unittest.TestCase):
    def test_a_decisive_close_through_breaks_the_level(self):
        bars = build(TRIPLE_REJECTION[:3] + [("break", 40_840)] + [("leg", 40_800, 4)])
        tracker = LevelTracker(pivot_strength=3, tolerance_points=12,
                               break_buffer_atr=0.25)
        feed(tracker, bars)
        self.assertEqual(tracker.active("support"), [])
        self.assertEqual(len(tracker.broken_levels), 1)
        self.assertTrue(tracker.broken_levels[0].broken)
        self.assertIsNotNone(tracker.broken_levels[0].broken_ts)

    def test_a_broken_level_records_no_further_rejections(self):
        bars = build(
            TRIPLE_REJECTION[:3] + [("break", 40_840), ("leg", 40_960, 6),
                                    ("reject", 40_901, 40_940)]
        )
        tracker = LevelTracker(pivot_strength=3, tolerance_points=12)
        feed(tracker, bars)
        broken = tracker.broken_levels[0]
        self.assertLessEqual(broken.rejections, 1)

    def test_a_shallow_dip_does_not_break_it(self):
        bars = build(TRIPLE_REJECTION)
        tracker = LevelTracker(pivot_strength=3, tolerance_points=12,
                               break_buffer_atr=0.25)
        feed(tracker, bars)
        self.assertEqual(len(tracker.active("support")), 1)


class TestResistance(unittest.TestCase):
    def test_resistance_is_the_mirror_image(self):
        bars = build([
            ("leg", 41_040, 6),
            ("reject", 41_100, 41_065),   # spike up, close back down
            ("leg", 41_015, 6),
            ("reject", 41_097, 41_060),
            ("leg", 41_025, 6),
            ("reject", 41_102, 41_055),
            ("leg", 40_980, 8),
        ])
        tracker = LevelTracker(pivot_strength=3, tolerance_points=12,
                               track="resistance")
        for index in range(len(bars)):
            tracker.observe(bars, index, atr=20.0)
        levels = tracker.active("resistance")
        self.assertEqual(len(levels), 1)
        self.assertEqual(levels[0].rejections, 3)
        self.assertGreater(levels[0].floor, levels[0].price)

    def test_tracking_one_kind_ignores_the_other(self):
        bars = build(TRIPLE_REJECTION)
        tracker = LevelTracker(pivot_strength=3, tolerance_points=12,
                               track="support")
        feed(tracker, bars)
        self.assertEqual(tracker.active("resistance"), [])


class TestQueriesAndHousekeeping(unittest.TestCase):
    def setUp(self):
        self.bars = build(TRIPLE_REJECTION)

    def test_touched_on_reports_only_the_crossing_bar(self):
        tracker = LevelTracker(pivot_strength=3, tolerance_points=12)
        hits = 0
        for index in range(len(self.bars)):
            tracker.observe(self.bars, index, atr=20.0)
            hits += len(tracker.touched_on(index, "support"))
        self.assertEqual(hits, 2, "only live rejections are news; the first is history")

    def test_nearest_below_and_above(self):
        tracker = LevelTracker(pivot_strength=3, tolerance_points=12)
        feed(tracker, self.bars)
        support = tracker.nearest_below(41_000, "support")
        self.assertIsNotNone(support)
        self.assertLess(support.price, 41_000)
        self.assertIsNone(tracker.nearest_below(40_000, "support"))

    def test_levels_age_out(self):
        tracker = LevelTracker(pivot_strength=3, tolerance_points=12,
                               max_level_age_bars=5)
        feed(tracker, self.bars)
        self.assertEqual(tracker.active("support"), [])

    def test_the_level_count_is_capped_keeping_the_best_tested(self):
        tracker = LevelTracker(pivot_strength=2, tolerance_points=1, max_levels=2)
        feed(tracker, self.bars)
        self.assertLessEqual(len(tracker.active()), 2)

    def test_bars_may_be_skipped_but_not_replayed(self):
        tracker = LevelTracker(pivot_strength=3, tolerance_points=12)
        tracker.observe(self.bars, 0, atr=20.0)
        tracker.observe(self.bars, 12, atr=20.0)   # catches up internally
        self.assertEqual(tracker._last_index, 12)
        self.assertEqual(len(tracker.active("support")), 1)
        with self.assertRaises(ValueError):
            tracker.observe(self.bars, 5, atr=20.0)

    def test_catching_up_gives_the_same_answer_as_every_bar(self):
        every = LevelTracker(pivot_strength=3, tolerance_points=12)
        feed(every, self.bars)
        skipping = LevelTracker(pivot_strength=3, tolerance_points=12)
        last = len(self.bars) - 1
        for index in range(0, last, 4):
            skipping.observe(self.bars, index, atr=20.0)
        skipping.observe(self.bars, last, atr=20.0)
        self.assertEqual(
            [round(lvl.price) for lvl in every.active("support")],
            [round(lvl.price) for lvl in skipping.active("support")],
        )

    def test_no_levels_form_before_atr_exists(self):
        tracker = LevelTracker(pivot_strength=3)   # ATR-based tolerance
        for index in range(len(self.bars)):
            tracker.observe(self.bars, index, atr=None)
        self.assertEqual(tracker.active(), [])

    def test_reset_clears_everything(self):
        tracker = LevelTracker(pivot_strength=3, tolerance_points=12)
        feed(tracker, self.bars)
        tracker.reset()
        self.assertEqual(tracker.active(), [])
        self.assertEqual(tracker._last_index, -1)

    def test_configuration_is_validated(self):
        with self.assertRaises(ValueError):
            LevelTracker(pivot_strength=0)
        with self.assertRaises(ValueError):
            LevelTracker(track="sideways")

    def test_levels_describe_themselves(self):
        tracker = LevelTracker(pivot_strength=3, tolerance_points=12)
        feed(tracker, self.bars)
        text = tracker.active("support")[0].describe()
        self.assertIn("support", text)
        self.assertIn("rejection", text)


if __name__ == "__main__":
    unittest.main()
