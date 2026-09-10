"""Loading NinjaTrader and CSV exports, and reshaping bars."""

import os
import tempfile
import unittest
from datetime import date, datetime

from ym.data import (
    DataFormatError, dedupe, filter_session, generate_bars, load_bars,
    resample, sniff, summarize, write_csv,
)
from ym.core import Bar
from ym.sessions import DEFAULT_SESSION, exchange_tz

ET = exchange_tz()

NT8_EXPORT = """20260908 093000;41012;41045;41003;41038;2481
20260908 093100;41038;41050;41030;41033;1902
20260908 093200;41033;41040;41010;41015;2211
20260908 093300;41015;41025;41000;41020;1700
"""

HEADERED_CSV = """Date,Time,Open,High,Low,Close,Volume
9/8/2026,09:30:00,41012,41045,41003,41038,2481
9/8/2026,09:31:00,41038,41050,41030,41033,1902
9/8/2026,09:32:00,41033,41040,41010,41015,2211
9/8/2026,09:33:00,41015,41025,41000,41020,1700
"""


class TempFileCase(unittest.TestCase):
    def write(self, text, suffix=".txt"):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=suffix, delete=False, encoding="utf-8"
        )
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name


class TestSniffing(TempFileCase):
    def test_ninjatrader_semicolon_export(self):
        dialect = sniff(self.write(NT8_EXPORT))
        self.assertEqual(dialect.delimiter, ";")
        self.assertFalse(dialect.has_header)
        self.assertEqual(dialect.timestamp_format, "%Y%m%d %H%M%S")

    def test_headered_comma_csv_with_split_date_and_time(self):
        dialect = sniff(self.write(HEADERED_CSV, ".csv"))
        self.assertEqual(dialect.delimiter, ",")
        self.assertTrue(dialect.has_header)
        self.assertIn("date", dialect.columns)
        self.assertIn("time", dialect.columns)

    def test_empty_file_is_an_error(self):
        with self.assertRaises(DataFormatError):
            sniff(self.write(""))


class TestLoading(TempFileCase):
    def test_ninjatrader_export_loads_with_exchange_timezone(self):
        bars = load_bars(self.write(NT8_EXPORT))
        self.assertEqual(len(bars), 4)
        self.assertEqual(bars[0].ts, datetime(2026, 9, 8, 9, 30, tzinfo=ET))
        self.assertEqual(bars[0].open, 41012)
        self.assertEqual(bars[0].volume, 2481)

    def test_both_formats_produce_identical_bars(self):
        from_nt = load_bars(self.write(NT8_EXPORT))
        from_csv = load_bars(self.write(HEADERED_CSV, ".csv"))
        self.assertEqual(
            [(b.ts, b.open, b.high, b.low, b.close) for b in from_nt],
            [(b.ts, b.open, b.high, b.low, b.close) for b in from_csv],
        )

    def test_source_timezone_is_converted_not_relabelled(self):
        bars = load_bars(self.write(NT8_EXPORT), tz="America/Chicago")
        # 09:30 Central is 10:30 Eastern.
        self.assertEqual(bars[0].ts.astimezone(ET).hour, 10)

    def test_bad_rows_are_skipped_by_default(self):
        text = NT8_EXPORT + "this is not a bar\n"
        bars = load_bars(self.write(text))
        self.assertEqual(len(bars), 4)

    def test_bad_rows_can_be_fatal(self):
        text = NT8_EXPORT + "20260908 093400;bogus;41025;41000;41020;1700\n"
        with self.assertRaises(DataFormatError):
            load_bars(self.write(text), skip_bad_rows=False)

    def test_mostly_unparseable_file_fails_loudly(self):
        text = "\n".join(f"garbage;row;{n};x;y;z" for n in range(30))
        with self.assertRaises(DataFormatError):
            load_bars(self.write(text))

    def test_explicit_timestamp_format_handles_day_first_dates(self):
        text = "08/09/2026 09:30:00;41012;41045;41003;41038;2481\n" * 4
        bars = load_bars(self.write(text), timestamp_format="%d/%m/%Y %H:%M:%S")
        self.assertEqual(bars[0].ts.month, 9)

    def test_rows_are_sorted_and_deduplicated(self):
        text = (
            "20260908 093100;41038;41050;41030;41033;1902\n"
            "20260908 093000;41012;41045;41003;41038;2481\n"
            "20260908 093100;41038;41050;41030;41099;1902\n"
        )
        bars = load_bars(self.write(text))
        self.assertEqual([bar.ts.minute for bar in bars], [30, 31])
        self.assertEqual(bars[1].close, 41099)  # the later duplicate wins

    def test_round_trip_through_write_csv(self):
        original = load_bars(self.write(NT8_EXPORT))
        out = self.write("", ".csv")
        write_csv(original, out)
        self.assertEqual(
            [(b.ts, b.close) for b in load_bars(out)],
            [(b.ts, b.close) for b in original],
        )


class TestReshaping(unittest.TestCase):
    def setUp(self):
        self.bars = [
            Bar(datetime(2026, 9, 8, 9, 30 + n, tzinfo=ET),
                41000 + n, 41010 + n, 40990 + n, 41005 + n, 100)
            for n in range(10)
        ]

    def test_resample_aggregates_ohlc_correctly(self):
        five = resample(self.bars, 5)
        self.assertEqual(len(five), 2)
        self.assertEqual(five[0].open, self.bars[0].open)
        self.assertEqual(five[0].close, self.bars[4].close)
        self.assertEqual(five[0].high, max(bar.high for bar in self.bars[:5]))
        self.assertEqual(five[0].low, min(bar.low for bar in self.bars[:5]))
        self.assertEqual(five[0].volume, 500)

    def test_resample_rejects_nonsense(self):
        with self.assertRaises(ValueError):
            resample(self.bars, 0)

    def test_dedupe_keeps_the_last(self):
        doubled = self.bars + [self.bars[-1]]
        self.assertEqual(len(dedupe(doubled)), len(self.bars))

    def test_session_filter(self):
        overnight = Bar(datetime(2026, 9, 8, 20, 0, tzinfo=ET), 1, 2, 0.5, 1.5)
        mixed = self.bars + [overnight]
        self.assertEqual(len(filter_session(mixed, "rth")), len(self.bars))
        self.assertEqual(len(filter_session(mixed, "overnight")), 1)
        self.assertEqual(len(filter_session(mixed, "all")), len(mixed))

    def test_summarize_mentions_coverage(self):
        text = summarize(self.bars)
        self.assertIn("bars", text)
        self.assertIn("spacing", text)
        self.assertEqual(summarize([]), "no bars")


class TestSynthetic(unittest.TestCase):
    def test_generated_bars_are_well_formed(self):
        bars = generate_bars(date(2026, 6, 1), days=3, seed=1)
        self.assertGreater(len(bars), 0)
        for bar in bars:
            self.assertLessEqual(bar.low, bar.high)
            self.assertLessEqual(bar.low, min(bar.open, bar.close))
            self.assertGreaterEqual(bar.high, max(bar.open, bar.close))
            self.assertIsNotNone(bar.ts.tzinfo)

    def test_generation_is_reproducible(self):
        first = generate_bars(date(2026, 6, 1), days=2, seed=5)
        second = generate_bars(date(2026, 6, 1), days=2, seed=5)
        self.assertEqual([b.close for b in first], [b.close for b in second])

    def test_only_weekdays_are_generated(self):
        bars = generate_bars(date(2026, 6, 1), days=10, seed=3)
        self.assertTrue(all(bar.ts.weekday() < 5 for bar in bars))

    def test_daily_range_is_in_a_plausible_band_for_ym(self):
        import statistics
        bars = filter_session(generate_bars(date(2026, 6, 1), days=20, seed=11), "rth")
        by_day = {}
        for bar in bars:
            by_day.setdefault(DEFAULT_SESSION.session_day(bar.ts), []).append(bar)
        ranges = [
            max(b.high for b in day) - min(b.low for b in day)
            for day in by_day.values()
        ]
        # YM near 41,000 typically ranges a few hundred points in the cash session.
        self.assertTrue(250 < statistics.fmean(ranges) < 700, statistics.fmean(ranges))


if __name__ == "__main__":
    unittest.main()
