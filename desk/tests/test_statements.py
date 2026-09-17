"""Detection and preview: the app must say what a file is before touching it."""

import unittest
from pathlib import Path

from tests.helpers import BAR_EXPORT, SMALL_STATEMENT, HubCase, ninjatrader_csv

from hub.statements import (
    BAR_KIND, StatementError, TRADE_KIND, detect_kind, file_digest, preview,
)


class TestDetection(HubCase):
    def write(self, content: bytes, name: str = "file.csv") -> Path:
        path = self.home / name
        path.write_bytes(content)
        return path

    def test_a_ninjatrader_export_is_recognised(self):
        kind, label = detect_kind(self.write(SMALL_STATEMENT))
        self.assertEqual(kind, TRADE_KIND)
        self.assertIn("NinjaTrader", label)

    def test_a_generic_trade_list_is_recognised(self):
        path = self.write(
            b"datetime,direction,entry_price,exit_price\n"
            b"2026-09-08T09:45,long,41000,41025\n"
        )
        self.assertEqual(detect_kind(path)[0], TRADE_KIND)

    def test_headerless_bar_data_is_recognised(self):
        self.assertEqual(detect_kind(self.write(BAR_EXPORT, "bars.txt"))[0], BAR_KIND)

    def test_headered_bar_data_is_recognised(self):
        path = self.write(
            b"Date,Time,Open,High,Low,Close,Volume\n"
            b"9/8/2026,09:30:00,41012,41045,41003,41038,2481\n"
        )
        self.assertEqual(detect_kind(path)[0], BAR_KIND)

    def test_an_unrelated_csv_is_not_guessed_at(self):
        path = self.write(b"name,colour,size\nwidget,red,3\n")
        with self.assertRaises(StatementError) as caught:
            preview(path)
        self.assertIn("could not tell what this file is", str(caught.exception))

    def test_a_pdf_is_refused_with_instructions(self):
        path = self.write(b"%PDF-1.4 whatever", "statement.pdf")
        with self.assertRaises(StatementError) as caught:
            detect_kind(path)
        self.assertIn("Trade Performance", str(caught.exception))

    def test_an_empty_file_is_refused(self):
        with self.assertRaises(StatementError):
            detect_kind(self.write(b""))

    def test_the_digest_identifies_identical_files(self):
        first = self.write(SMALL_STATEMENT, "a.csv")
        second = self.write(SMALL_STATEMENT, "b.csv")
        different = self.write(SMALL_STATEMENT + b"x", "c.csv")
        self.assertEqual(file_digest(first), file_digest(second))
        self.assertNotEqual(file_digest(first), file_digest(different))


class TestPreview(HubCase):
    def write(self, content: bytes, name: str = "file.csv") -> Path:
        path = self.home / name
        path.write_bytes(content)
        return path

    def test_preview_describes_the_trades_without_storing_them(self):
        found = preview(self.write(SMALL_STATEMENT))
        self.assertEqual(found.kind, TRADE_KIND)
        self.assertEqual(found.row_count, 2)
        self.assertEqual(found.symbols, {"MYM": 2})
        self.assertEqual(self.hub.overview()["trade_count"], 0)

    def test_preview_flags_missing_stops(self):
        found = preview(self.write(SMALL_STATEMENT))
        self.assertTrue(any("no stop" in note for note in found.notes))

    def test_a_default_stop_removes_that_warning(self):
        found = preview(self.write(SMALL_STATEMENT), default_stop_points=25)
        self.assertFalse(any("no stop" in note for note in found.notes))
        self.assertIsNotNone(found.sample[0]["r_multiple"])

    def test_unreadable_rows_become_warnings_not_failures(self):
        found = preview(self.write(
            SMALL_STATEMENT
            + b"MYM;Sideways;2;41010;40990;not-a-date;9/8/2026 11:12;Huh;1.00\n"
        ))
        self.assertEqual(found.row_count, 2)
        self.assertEqual(len(found.warnings), 1)

    def test_a_file_of_only_bad_rows_is_refused(self):
        with self.assertRaises(StatementError):
            preview(self.write(
                b"Instrument;Market pos.;Quantity;Entry price;Entry time\n"
                b"MYM;Sideways;2;nope;not-a-date\n"
            ))

    def test_bar_data_previews_as_market_history(self):
        found = preview(self.write(BAR_EXPORT, "bars.txt"))
        self.assertEqual(found.kind, BAR_KIND)
        self.assertEqual(found.row_count, 20)
        self.assertTrue(any("not a statement" in note for note in found.notes))

    def test_the_sample_is_capped(self):
        found = preview(self.write(ninjatrader_csv(days=30)), sample_size=5)
        self.assertEqual(len(found.sample), 5)
        self.assertGreater(found.row_count, 5)

    def test_preview_is_json_able(self):
        import json
        json.dumps(preview(self.write(SMALL_STATEMENT)).to_dict())


if __name__ == "__main__":
    unittest.main()
