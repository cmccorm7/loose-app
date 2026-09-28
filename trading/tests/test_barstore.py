"""Bar storage: the thing that lets a trade be read against what price did."""

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta

from ym.barstore import BarStore, infer_timeframe
from ym.core import Bar
from ym.data import filter_session, generate_bars
from ym.sessions import exchange_tz

ET = exchange_tz()


def minute_bars(days=5, seed=3):
    return filter_session(generate_bars(date(2026, 9, 1), days=days, seed=seed), "rth")


class StoreCase(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        os.unlink(handle.name)
        self.path = handle.name
        self.addCleanup(
            lambda: os.path.exists(self.path) and os.unlink(self.path)
        )
        self.store = BarStore(self.path)
        self.addCleanup(self.store.close)
        self.bars = minute_bars()


class TestTimeframeInference(unittest.TestCase):
    def test_one_minute_bars(self):
        self.assertEqual(infer_timeframe(minute_bars(days=1)), 1)

    def test_five_minute_bars(self):
        from ym.data import resample
        self.assertEqual(infer_timeframe(resample(minute_bars(days=1), 5)), 5)

    def test_a_single_bar_defaults_to_one_minute(self):
        self.assertEqual(infer_timeframe(minute_bars(days=1)[:1]), 1)

    def test_gaps_do_not_confuse_it(self):
        # An overnight gap is one huge spacing among hundreds of 1-minute ones.
        self.assertEqual(infer_timeframe(minute_bars(days=3)), 1)


class TestStorage(StoreCase):
    def test_bars_round_trip(self):
        self.store.add("MYM", self.bars)
        back = self.store.bars("MYM", 1)
        self.assertEqual(len(back), len(self.bars))
        self.assertEqual(back[0].ts, self.bars[0].ts)
        self.assertAlmostEqual(back[0].close, self.bars[0].close)

    def test_timestamps_keep_their_timezone(self):
        self.store.add("MYM", self.bars[:10])
        self.assertIsNotNone(self.store.bars("MYM", 1)[0].ts.tzinfo)

    def test_importing_the_same_bars_twice_does_not_duplicate(self):
        self.store.add("MYM", self.bars)
        self.store.add("MYM", self.bars)
        self.assertEqual(self.store.count("MYM"), len(self.bars))

    def test_re_importing_updates_a_corrected_bar(self):
        self.store.add("MYM", self.bars[:5])
        fixed = Bar(self.bars[0].ts, 1, 2, 0.5, 1.5, 99)
        self.store.add("MYM", [fixed])
        self.assertEqual(self.store.bars("MYM", 1)[0].close, 1.5)

    def test_symbols_are_kept_apart(self):
        self.store.add("MYM", self.bars[:10])
        self.store.add("YM", self.bars[:20])
        self.assertEqual(self.store.count("MYM"), 10)
        self.assertEqual(self.store.count("YM"), 20)
        self.assertEqual(self.store.symbols(), ["MYM", "YM"])

    def test_symbols_are_normalised(self):
        self.store.add("mym", self.bars[:5])
        self.assertEqual(self.store.symbols(), ["MYM"])
        self.assertEqual(self.store.count("mym"), 5)

    def test_empty_input_is_a_no_op(self):
        self.assertEqual(self.store.add("MYM", []), 0)

    def test_deleting_a_symbol(self):
        self.store.add("MYM", self.bars[:10])
        self.store.add("YM", self.bars[:10])
        self.assertEqual(self.store.delete("MYM"), 10)
        self.assertEqual(self.store.symbols(), ["YM"])

    def test_the_store_survives_reopening(self):
        self.store.add("MYM", self.bars[:10])
        self.store.close()
        with BarStore(self.path) as reopened:
            self.assertEqual(reopened.count("MYM"), 10)


class TestReading(StoreCase):
    def setUp(self):
        super().setUp()
        self.store.add("MYM", self.bars)

    def test_resampling_happens_on_the_way_out(self):
        five = self.store.bars("MYM", 5)
        self.assertGreater(len(five), 0)
        self.assertLess(len(five), len(self.bars))
        self.assertEqual(self.store.native_timeframes("MYM"), [1])

    def test_a_window_is_inclusive_of_its_edges(self):
        start = datetime(2026, 9, 1, 10, 0, tzinfo=ET)
        end = datetime(2026, 9, 1, 10, 30, tzinfo=ET)
        window = self.store.bars("MYM", 1, start, end)
        self.assertEqual(window[0].ts, start)
        self.assertEqual(window[-1].ts, end)

    def test_padding_widens_the_window_both_ways(self):
        start = datetime(2026, 9, 1, 10, 0, tzinfo=ET)
        end = datetime(2026, 9, 1, 10, 30, tzinfo=ET)
        tight = self.store.bars("MYM", 1, start, end)
        padded = self.store.bars("MYM", 1, start, end, pad_minutes=15)
        self.assertEqual(len(padded), len(tight) + 30)
        self.assertLess(padded[0].ts, start)
        self.assertGreater(padded[-1].ts, end)

    def test_an_unknown_symbol_returns_nothing(self):
        self.assertEqual(self.store.bars("ES", 1), [])

    def test_a_timeframe_finer_than_stored_returns_nothing(self):
        from ym.data import resample
        self.store.delete("MYM")
        self.store.add("MYM", resample(self.bars, 5))
        self.assertEqual(self.store.bars("MYM", 1), [])
        self.assertGreater(len(self.store.bars("MYM", 5)), 0)
        self.assertGreater(len(self.store.bars("MYM", 15)), 0)


class TestCoverage(StoreCase):
    def setUp(self):
        super().setUp()
        self.store.add("MYM", self.bars)

    def test_coverage_reports_the_span(self):
        found = self.store.coverage("MYM")
        self.assertEqual(found["bars"], len(self.bars))
        self.assertEqual(found["first"], self.bars[0].ts)
        self.assertEqual(found["last"], self.bars[-1].ts)
        self.assertEqual(found["timeframes"], [1])

    def test_coverage_of_an_unknown_symbol_is_none(self):
        self.assertIsNone(self.store.coverage("ES"))

    def test_covered_days_counts_trading_days(self):
        days = self.store.covered_days("MYM")
        self.assertEqual(len(days), 5)
        self.assertTrue(all(day.weekday() < 5 for day in days))

    def test_covers_answers_whether_a_trade_can_be_analysed(self):
        traded = self.bars[100].ts
        self.assertTrue(self.store.covers("MYM", traded))
        weekend = datetime(2026, 9, 5, 10, 0, tzinfo=ET)
        self.assertFalse(self.store.covers("MYM", weekend))

    def test_covers_allows_a_small_tolerance(self):
        moment = self.bars[100].ts + timedelta(seconds=90)
        self.assertTrue(self.store.covers("MYM", moment, within_minutes=5))
        self.assertFalse(
            self.store.covers("MYM", moment + timedelta(hours=6), within_minutes=5)
        )


if __name__ == "__main__":
    unittest.main()
