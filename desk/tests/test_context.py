"""Bars in the app, and the tags that come from them."""

import unittest

from tests.helpers import (
    HubCase, bar_export, minute_bars, trade_export, trades_against,
)

from hub.services import HubError


class ContextCase(HubCase):
    def setUp(self):
        super().setUp()
        self.bars = minute_bars(days=20)
        self.trades = trades_against(self.bars)
        self.hub.update_settings({"symbol": "MYM", "equity": 2_000})

    def load_bars(self, symbol="MYM"):
        uploaded = self.hub.upload_statement("MYM_1min.txt", bar_export(self.bars))
        self.assertIsNone(uploaded["error"], uploaded["error"])
        return self.hub.import_statement(uploaded["statement"]["id"], symbol=symbol)

    def load_trades(self, stop=20.0):
        uploaded = self.hub.upload_statement("trades.csv", trade_export(self.trades))
        self.assertIsNone(uploaded["error"], uploaded["error"])
        return self.hub.import_statement(
            uploaded["statement"]["id"], default_stop_points=stop
        )


class TestBarImport(ContextCase):
    def test_a_bar_export_is_stored_rather_than_refused(self):
        result = self.load_bars()
        self.assertEqual(result["kind"], "bars")
        self.assertEqual(result["imported"], len(self.bars))
        self.assertEqual(result["symbol"], "MYM")

    def test_bars_do_not_land_in_the_journal(self):
        self.load_bars()
        self.assertEqual(self.hub.overview()["trade_count"], 0)

    def test_coverage_reports_what_was_stored(self):
        self.load_bars()
        entry = self.hub.bars_coverage()["symbols"][0]
        self.assertEqual(entry["symbol"], "MYM")
        self.assertEqual(entry["bars"], len(self.bars))
        self.assertEqual(entry["days"], 20)

    def test_the_symbol_comes_from_the_caller(self):
        # Bar exports do not name their instrument; guessing would misprice
        # every trade, so the choice has to be made explicitly.
        self.load_bars(symbol="YM")
        self.assertEqual(self.hub.bars_coverage()["symbols"][0]["symbol"], "YM")

    def test_deleting_a_bar_import_removes_its_bars(self):
        self.load_bars()
        statement_id = self.hub.list_statements()[0]["id"]
        result = self.hub.delete_statement(statement_id)
        self.assertGreater(result["bars_removed"], 0)
        self.assertEqual(self.hub.bars_coverage()["symbols"], [])

    def test_no_bars_means_no_coverage_rather_than_an_error(self):
        coverage = self.hub.bars_coverage()
        self.assertEqual(coverage["symbols"], [])
        self.assertEqual(coverage["total_bars"], 0)


class TestTagging(ContextCase):
    def test_importing_trades_after_bars_tags_them(self):
        self.load_bars()
        result = self.load_trades()
        self.assertEqual(result["retagged"]["tagged"], result["imported"])
        self.assertEqual(result["retagged"]["untagged"], 0)

    def test_importing_trades_first_still_tags_once_the_bars_arrive(self):
        first = self.load_trades()
        self.assertEqual(first["retagged"]["tagged"], 0, "nothing to tag against yet")
        self.load_bars()
        self.assertGreater(self.hub.bars_coverage()["trades_with_context"], 0)

    def test_tags_are_written_onto_the_journal(self):
        self.load_bars()
        self.load_trades()
        tags = self.hub.trades(limit=5)["trades"][0]["tags"]
        self.assertTrue(any(tag.startswith("location:") for tag in tags), tags)
        self.assertTrue(any(tag.startswith("trend:") for tag in tags), tags)

    def test_retagging_is_idempotent(self):
        self.load_bars()
        self.load_trades()
        before = self.hub.trades(limit=5)["trades"][0]["tags"]
        self.hub.retag()
        self.assertEqual(self.hub.trades(limit=5)["trades"][0]["tags"], before)

    def test_every_trade_gets_one_of_the_three_locations(self):
        self.load_bars()
        self.load_trades()
        contexts = self.hub.trade_contexts()["contexts"]
        locations = {row["location"] for row in contexts if row["has_bars"]}
        self.assertTrue(locations)
        self.assertTrue(locations <= {"at_level", "mid_range", "chasing"})

    def test_a_looser_rule_cannot_find_fewer_levels(self):
        self.load_bars()
        self.load_trades()
        tight = self.hub.trade_contexts()["contexts"]
        self.hub.update_settings({"location_mode": "atr_zone"})
        loose = self.hub.trade_contexts()["contexts"]
        self.assertEqual(self.hub.trade_contexts()["location_mode"], "atr_zone")
        self.assertGreaterEqual(
            sum(1 for row in loose if row["location"] == "at_level"),
            sum(1 for row in tight if row["location"] == "at_level"),
        )

    def test_the_measurements_do_not_move_with_the_rule(self):
        # Facts are stored; the tag is a view over them. Switching the rule
        # must not change what was measured.
        self.load_bars()
        self.load_trades()
        tight = self.hub.trade_contexts()["contexts"]
        self.hub.update_settings({"location_mode": "atr_zone"})
        loose = self.hub.trade_contexts()["contexts"]
        self.assertEqual(
            [row["level_rejections"] for row in tight],
            [row["level_rejections"] for row in loose],
        )
        self.assertEqual(
            [row["stop_points"] for row in tight],
            [row["stop_points"] for row in loose],
        )

    def test_an_invalid_location_rule_is_refused(self):
        with self.assertRaises(HubError):
            self.hub.update_settings({"location_mode": "vibes"})


class TestContextReport(ContextCase):
    def setUp(self):
        super().setUp()
        self.load_bars()
        self.load_trades()

    def test_the_report_finds_the_simulated_habits(self):
        report = self.hub.market_context()
        codes = {finding["code"] for finding in report["findings"]}
        self.assertIn("stop_vs_volatility", codes)
        self.assertGreater(report["analysed"], 10)

    def test_the_headline_numbers_are_present(self):
        self.assertIn("stop vs a 5-min bar", self.hub.market_context()["headline"])

    def test_the_report_is_json_able(self):
        import json
        json.dumps(self.hub.market_context())
        json.dumps(self.hub.trade_contexts())

    def test_trade_contexts_come_back_oldest_first(self):
        rows = self.hub.trade_contexts()["contexts"]
        self.assertEqual([r["entry_time"] for r in rows],
                         sorted(r["entry_time"] for r in rows))

    def test_a_thin_sample_reports_why_rather_than_guessing(self):
        report = self.hub.market_context(min_trades=10_000)
        self.assertEqual(report["findings"], [])
        self.assertTrue(any("below the" in note for note in report["notes"]))


class TestContextRoutes(ContextCase):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from hub.app import create_app
        self.client = TestClient(create_app(hub=self.hub))

    def test_every_context_route_answers(self):
        self.load_bars()
        self.load_trades()
        for path in ("/api/bars", "/api/context", "/api/context/trades"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_retag_is_a_post(self):
        self.load_bars()
        self.load_trades()
        self.assertEqual(
            self.client.post("/api/retag").json()["tagged"], len(self.trades)
        )

    def test_the_routes_work_before_anything_is_imported(self):
        self.assertEqual(self.client.get("/api/bars").json()["symbols"], [])
        self.assertEqual(self.client.get("/api/context").json()["findings"], [])

    def test_the_trade_context_limit_is_bounded(self):
        self.assertEqual(
            self.client.get("/api/context/trades?limit=0").status_code, 422
        )


if __name__ == "__main__":
    unittest.main()
