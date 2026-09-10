"""Smoke tests for the command line surface: every command runs, exits sanely,
and prints what it claims to."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from ym.cli import main, parse_clock


class CliCase(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.db = str(Path(self.workspace) / "journal.db")

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    def run_cli(self, *argv, expect=0):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = main(list(argv))
        output = buffer.getvalue()
        self.assertEqual(code, expect, f"`{' '.join(argv)}` said:\n{output}")
        return output

    def path(self, name):
        return str(Path(self.workspace) / name)


class TestClockParsing(unittest.TestCase):
    def test_accepted_formats(self):
        from datetime import time
        self.assertEqual(parse_clock("12:00"), time(12, 0))
        self.assertEqual(parse_clock("1200"), time(12, 0))
        self.assertEqual(parse_clock("15"), time(15, 0))
        self.assertIsNone(parse_clock(None))

    def test_nonsense_is_rejected(self):
        import argparse
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_clock("lunchtime")


class TestSize(CliCase):
    def test_an_allowed_trade_exits_zero(self):
        output = self.run_cli(
            "size", "--symbol", "YM", "--equity", "25000",
            "--entry", "41000", "--stop", "40985",
        )
        self.assertIn("APPROVED", output)
        self.assertIn("1R", output)

    def test_a_refused_trade_exits_one(self):
        output = self.run_cli(
            "size", "--symbol", "YM", "--equity", "25000",
            "--entry", "41000", "--stop", "40900", expect=1,
        )
        self.assertIn("REJECTED", output)

    def test_micro_contract_allows_more_size(self):
        output = self.run_cli(
            "size", "--symbol", "MYM", "--equity", "25000",
            "--entry", "41000", "--stop", "40985",
        )
        self.assertIn("APPROVED", output)


class TestData(CliCase):
    def test_sample_then_info_then_convert(self):
        sample = self.path("sample.csv")
        self.run_cli("data", "sample", "--out", sample, "--days", "3")
        self.assertTrue(os.path.exists(sample))

        info = self.run_cli("data", "info", sample)
        self.assertIn("bars", info)
        self.assertIn("delimiter", info)

        resampled = self.path("five.csv")
        self.run_cli("data", "convert", sample, "--out", resampled, "--minutes", "5")
        self.assertTrue(os.path.exists(resampled))
        with open(sample) as one, open(resampled) as five:
            self.assertGreater(len(one.readlines()), len(five.readlines()))

    def test_missing_file_is_an_error_not_a_traceback(self):
        self.run_cli("data", "info", self.path("nope.csv"), expect=2)


class TestBacktest(CliCase):
    def test_runs_on_synthetic_data_and_warns_about_it(self):
        output = self.run_cli(
            "backtest", "--strategy", "orb", "--symbol", "MYM",
            "--days", "10", "--equity", "25000",
        )
        self.assertIn("synthetic", output)
        self.assertIn("Trades", output)

    def test_strategy_parameters_are_passed_through(self):
        output = self.run_cli(
            "backtest", "--strategy", "orb", "--symbol", "MYM", "--days", "10",
            "-p", "range_minutes=15", "-p", "target_r=3.0",
        )
        self.assertIn("ORB15", output)

    def test_trades_can_be_written_and_journalled(self):
        out = self.path("trades.csv")
        output = self.run_cli(
            "backtest", "--strategy", "orb", "--symbol", "MYM", "--days", "20",
            "--out", out, "--to-journal", "--db", self.db,
        )
        self.assertTrue(os.path.exists(out))
        self.assertIn("recorded", output)

    def test_an_unknown_strategy_is_rejected_by_the_parser(self):
        with self.assertRaises(SystemExit):
            main(["backtest", "--strategy", "wishful"])


class TestJournalAndReview(CliCase):
    def seed(self, days=60):
        return self.run_cli("--db", self.db, "journal", "seed", "--days", str(days))

    def test_seed_list_stats(self):
        self.assertIn("seeded", self.seed())
        listing = self.run_cli("--db", self.db, "journal", "list", "--limit", "5")
        self.assertIn("setup", listing)
        stats = self.run_cli(
            "--db", self.db, "journal", "stats", "--equity", "25000",
            "--by", "hour", "--by", "weekday",
        )
        self.assertIn("Journal performance", stats)
        self.assertIn("Hour", stats)
        self.assertIn("Weekday", stats)

    def test_add_then_set_then_list(self):
        self.run_cli(
            "--db", self.db, "journal", "add", "--symbol", "MYM",
            "--direction", "long", "--entry-time", "2026-09-08T09:45",
            "--entry", "41000", "--contracts", "2", "--stop", "40975",
            "--exit-time", "2026-09-08T10:05", "--exit", "41025",
            "--setup", "ORB", "--reason", "target",
        )
        listing = self.run_cli("--db", self.db, "journal", "list")
        self.assertIn("ORB", listing)
        self.run_cli("--db", self.db, "journal", "set", "1", "--notes", "good one")
        self.run_cli("--db", self.db, "journal", "set", "1", "--unplanned")

    def test_add_without_a_stop_reports_no_r(self):
        output = self.run_cli(
            "--db", self.db, "journal", "add", "--symbol", "MYM",
            "--direction", "short", "--entry-time", "2026-09-08T09:45",
            "--entry", "41000", "--exit-time", "2026-09-08T10:05", "--exit", "40975",
        )
        self.assertIn("no stop", output)

    def test_export(self):
        self.seed(days=10)
        out = self.path("export.csv")
        self.run_cli("--db", self.db, "journal", "export", "--out", out)
        self.assertTrue(os.path.exists(out))

    def test_review_finds_the_seeded_habits_and_suggests_guardrails(self):
        self.seed()
        output = self.run_cli("--db", self.db, "review", "--apply")
        self.assertIn("Behavioral review", output)
        self.assertIn("Suggested guardrails", output)
        self.assertIn("max_trades_per_day", output)
        self.assertIn("RiskLimits with those guardrails applied", output)

    def test_review_on_an_empty_journal_exits_one(self):
        output = self.run_cli("--db", self.db, "review", expect=1)
        self.assertIn("no closed trades", output)

    def test_coach_reports_state_and_guidance(self):
        self.seed()
        output = self.run_cli(
            "--db", self.db, "coach", "--symbol", "MYM", "--equity", "25000",
            "--at", "2026-07-24T13:20", "--max-trades", "2", "--hard-stop", "13:00",
        )
        self.assertIn("Equity", output)
        self.assertIn("session day", output)

    def test_coach_on_an_empty_journal_exits_one(self):
        output = self.run_cli(
            "--db", self.db, "coach", "--symbol", "MYM", "--equity", "25000",
            expect=1,
        )
        self.assertIn("no trades yet", output)

    def test_import_of_a_ninjatrader_export(self):
        path = self.path("nt.csv")
        Path(path).write_text(
            "Instrument;Market pos.;Quantity;Entry price;Exit price;Entry time;"
            "Exit time;Entry name;Commission\n"
            "MYM 12-26;Long;2;41000;41025;9/8/2026 9:45:00;9/8/2026 10:05:00;ORB;1.00\n",
            encoding="utf-8",
        )
        output = self.run_cli(
            "--db", self.db, "journal", "import", path, "--default-stop-points", "25",
        )
        self.assertIn("imported 1", output)


class TestDemo(CliCase):
    def test_the_demo_walks_all_four_layers(self):
        output = self.run_cli("demo")
        for heading in ("1. RISK", "2. BACKTEST", "3. BEHAVIOR", "4. COACH"):
            self.assertIn(heading, output)
        self.assertIn("APPROVED", output)
        self.assertIn("Behavioral review", output)


if __name__ == "__main__":
    unittest.main()
