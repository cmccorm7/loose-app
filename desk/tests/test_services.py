"""The capability layer. These are the functions the assistant's tools will
wrap, so their contracts matter more than the routes above them."""

import json
import unittest

from tests.helpers import BAR_EXPORT, SMALL_STATEMENT, HubCase, ninjatrader_csv

from hub.services import HubError


class TestUploadAndImport(HubCase):
    def test_upload_previews_but_stores_nothing(self):
        result = self.hub.upload_statement("nt.csv", SMALL_STATEMENT)
        self.assertIsNone(result["error"])
        self.assertEqual(result["preview"]["row_count"], 2)
        self.assertEqual(result["statement"]["status"], "pending")
        self.assertEqual(self.hub.overview()["trade_count"], 0)

    def test_import_is_a_separate_step(self):
        uploaded = self.hub.upload_statement("nt.csv", SMALL_STATEMENT)
        result = self.hub.import_statement(uploaded["statement"]["id"])
        self.assertEqual(result["imported"], 2)
        self.assertEqual(self.hub.overview()["trade_count"], 2)

    def test_an_empty_upload_is_refused(self):
        with self.assertRaises(HubError):
            self.hub.upload_statement("nt.csv", b"")

    def test_an_oversized_upload_is_refused(self):
        from hub.services import MAX_UPLOAD_BYTES
        with self.assertRaises(HubError) as caught:
            self.hub.upload_statement("big.csv", b"x" * (MAX_UPLOAD_BYTES + 1))
        self.assertIn("limit", str(caught.exception))

    def test_an_unreadable_file_is_recorded_as_failed_not_raised(self):
        result = self.hub.upload_statement("junk.csv", b"a,b,c\n1,2,3\n")
        self.assertIsNotNone(result["error"])
        self.assertEqual(result["statement"]["status"], "failed")
        self.assertEqual(self.hub.list_statements()[0]["status"], "failed")

    def test_importing_twice_is_refused(self):
        uploaded = self.hub.upload_statement("nt.csv", SMALL_STATEMENT)
        self.hub.import_statement(uploaded["statement"]["id"])
        with self.assertRaises(HubError) as caught:
            self.hub.import_statement(uploaded["statement"]["id"])
        self.assertIn("already imported", str(caught.exception))

    def test_a_duplicate_file_is_flagged_before_it_double_counts(self):
        self.import_statement(SMALL_STATEMENT, "first.csv")
        again = self.hub.upload_statement("second.csv", SMALL_STATEMENT)
        self.assertTrue(
            any("double-count" in note for note in again["preview"]["notes"]),
            again["preview"]["notes"],
        )

    def test_bar_data_cannot_be_imported_into_the_journal(self):
        uploaded = self.hub.upload_statement("bars.txt", BAR_EXPORT)
        with self.assertRaises(HubError) as caught:
            self.hub.import_statement(uploaded["statement"]["id"])
        self.assertIn("market data", str(caught.exception))

    def test_importing_an_unknown_id_is_refused(self):
        with self.assertRaises(HubError):
            self.hub.import_statement("nope")

    def test_a_default_stop_gives_imported_trades_an_r_multiple(self):
        self.import_statement(SMALL_STATEMENT, default_stop_points=25)
        trade = self.hub.trades()["trades"][0]
        self.assertIsNotNone(trade["r_multiple"])
        self.assertIsNotNone(trade["stop_price"])

    def test_without_a_default_stop_there_is_no_r(self):
        self.import_statement(SMALL_STATEMENT)
        self.assertIsNone(self.hub.trades()["trades"][0]["r_multiple"])


class TestReversibility(HubCase):
    def test_deleting_a_statement_removes_exactly_its_trades(self):
        self.import_statement(SMALL_STATEMENT, "one.csv")
        big = self.hub.upload_statement("two.csv", ninjatrader_csv(days=20))
        self.hub.import_statement(big["statement"]["id"])
        before = self.hub.overview()["trade_count"]

        result = self.hub.delete_statement(big["statement"]["id"])
        self.assertEqual(result["trades_removed"], before - 2)
        self.assertEqual(self.hub.overview()["trade_count"], 2)

    def test_the_file_is_removed_from_disk_too(self):
        uploaded = self.hub.upload_statement("nt.csv", SMALL_STATEMENT)
        stored = self.hub.paths.uploads / uploaded["statement"]["stored_as"]
        self.assertTrue(stored.exists())
        self.hub.delete_statement(uploaded["statement"]["id"])
        self.assertFalse(stored.exists())

    def test_trades_can_be_kept_when_the_statement_is_removed(self):
        self.import_statement(SMALL_STATEMENT)
        statement_id = self.hub.list_statements()[0]["id"]
        self.hub.delete_statement(statement_id, remove_trades=False)
        self.assertEqual(self.hub.overview()["trade_count"], 2)
        self.assertEqual(self.hub.list_statements(), [])

    def test_deleting_an_unknown_statement_is_refused(self):
        with self.assertRaises(HubError):
            self.hub.delete_statement("nope")

    def test_every_imported_trade_is_tagged_with_its_source(self):
        self.import_statement(SMALL_STATEMENT)
        statement_id = self.hub.list_statements()[0]["id"]
        self.assertEqual(
            self.hub.overview()["sources"], {f"statement:{statement_id}": 2}
        )


class TestAnalysis(HubCase):
    @classmethod
    def setUpClass(cls):
        cls.statement = ninjatrader_csv(days=60)

    def setUp(self):
        super().setUp()
        self.import_statement(self.statement, "nt.csv", default_stop_points=25)

    def test_overview_reports_the_headline_numbers(self):
        overview = self.hub.overview()
        self.assertTrue(overview["has_data"])
        self.assertGreater(overview["trade_count"], 100)
        self.assertIsNotNone(overview["metrics"]["expectancy_r"])
        self.assertEqual(overview["imported_statements"], 1)

    def test_overview_is_json_able(self):
        json.dumps(self.hub.overview())

    def test_infinity_never_reaches_json(self):
        # A profit factor with no losing trades is infinite, which JSON cannot
        # represent; it must come back as a note instead.
        from hub.services import _metrics_dict

        class Fake:
            def as_dict(self):
                return {"profit_factor": float("inf")}

        data = _metrics_dict(Fake())
        self.assertIsNone(data["profit_factor"])
        json.dumps(data)

    def test_equity_curve_starts_at_the_configured_equity(self):
        curve = self.hub.equity_curve()
        self.assertEqual(curve["curve"][0]["equity"], curve["starting_equity"])
        self.assertGreater(len(curve["daily"]), 10)

    def test_breakdown_buckets_and_lists_its_options(self):
        data = self.hub.breakdown("hour")
        self.assertEqual(data["by"], "hour")
        self.assertTrue(data["buckets"])
        self.assertIn("setup", data["available"])
        self.assertEqual(
            sum(bucket["trades"] for bucket in data["buckets"]),
            self.hub.overview()["trade_count"],
        )

    def test_an_unknown_grouping_is_refused(self):
        with self.assertRaises(HubError):
            self.hub.breakdown("phase_of_moon")

    def test_trades_come_back_newest_last_and_capped(self):
        page = self.hub.trades(limit=10)
        self.assertEqual(page["shown"], 10)
        self.assertGreater(page["total"], 10)
        times = [trade["entry_time"] for trade in page["trades"]]
        self.assertEqual(times, sorted(times))

    def test_behavior_review_finds_the_planted_habits(self):
        review = self.hub.behavior_review()
        codes = {finding["code"] for finding in review["findings"]}
        self.assertIn("ordinal_decay", codes)
        self.assertIn("time_of_day_decay", codes)
        self.assertTrue(review["guardrails"])

    def test_behavior_guardrails_are_json_able(self):
        # hard_stop_time is a time object in the engine; it must serialise.
        review = self.hub.behavior_review()
        json.dumps(review)
        if "hard_stop_time" in review["guardrails"]:
            self.assertIsInstance(review["guardrails"]["hard_stop_time"], str)

    def test_filters_narrow_the_result(self):
        everything = self.hub.performance()
        narrowed = self.hub.performance(symbol="YM")
        self.assertGreater(everything["trade_count"], 0)
        self.assertEqual(narrowed["trade_count"], 0)


class TestSettingsAndSizing(HubCase):
    def test_settings_round_trip(self):
        updated = self.hub.update_settings({"equity": 50_000, "symbol": "YM"})
        self.assertEqual(updated["equity"], 50_000)
        self.assertEqual(self.hub.settings()["symbol"], "YM")

    def test_unknown_settings_are_refused(self):
        with self.assertRaises(HubError):
            self.hub.update_settings({"moon_phase": "waxing"})

    def test_settings_survive_a_new_hub_instance(self):
        self.hub.update_settings({"equity": 31_000})
        from hub.config import paths
        from hub.services import Hub
        self.assertEqual(Hub(paths(self.home)).settings()["equity"], 31_000)

    def test_sizing_follows_the_stop(self):
        tight = self.hub.position_size(41_000, 40_990)
        wide = self.hub.position_size(41_000, 40_900)
        self.assertTrue(tight["approved"])
        self.assertGreater(tight["contracts"], wide["contracts"])

    def test_a_stop_too_wide_to_size_is_refused_with_a_reason(self):
        self.hub.update_settings({"symbol": "YM", "equity": 5_000})
        result = self.hub.position_size(41_000, 40_800)
        self.assertFalse(result["approved"])
        self.assertTrue(result["blockers"])

    def test_sizing_reports_reward_to_risk(self):
        result = self.hub.position_size(41_000, 40_980, target=41_040)
        self.assertAlmostEqual(result["reward_risk"], 2.0)

    def test_an_unknown_symbol_is_refused(self):
        with self.assertRaises(HubError):
            self.hub.position_size(41_000, 40_980, symbol="TSLA")

    def test_instruments_are_listed_for_the_ui(self):
        symbols = {item["symbol"] for item in self.hub.instruments()}
        self.assertEqual(symbols, {"YM", "MYM"})


if __name__ == "__main__":
    unittest.main()
