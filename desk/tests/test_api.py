"""The HTTP layer: routes, argument handling and the cross-origin guard."""

import unittest

from fastapi.testclient import TestClient

from tests.helpers import BAR_EXPORT, SMALL_STATEMENT, HubCase, ninjatrader_csv

from hub.app import create_app


class ApiCase(HubCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(hub=self.hub))

    def upload(self, content: bytes = SMALL_STATEMENT, name: str = "nt.csv"):
        return self.client.post(
            "/api/statements", files={"file": (name, content, "text/csv")}
        )


class TestRoutes(ApiCase):
    def test_health_reports_where_the_data_lives(self):
        body = self.client.get("/api/health").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["data_dir"], str(self.home))

    def test_the_page_is_served(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("YM Desk", response.text)

    def test_overview_on_an_empty_journal(self):
        body = self.client.get("/api/overview").json()
        self.assertFalse(body["has_data"])
        self.assertEqual(body["trade_count"], 0)

    def test_settings_round_trip_over_http(self):
        response = self.client.put("/api/settings", json={"equity": 40_000})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/settings").json()["equity"], 40_000)

    def test_an_unknown_setting_is_a_400_with_a_message(self):
        response = self.client.put("/api/settings", json={"nonsense": 1})
        self.assertEqual(response.status_code, 400)
        self.assertIn("unknown setting", response.json()["detail"])

    def test_instruments_are_listed(self):
        body = self.client.get("/api/instruments").json()
        self.assertEqual(len(body["instruments"]), 2)

    def test_upload_then_import_then_analyse(self):
        uploaded = self.upload(ninjatrader_csv(days=40))
        self.assertEqual(uploaded.status_code, 200)
        statement_id = uploaded.json()["statement"]["id"]

        imported = self.client.post(
            f"/api/statements/{statement_id}/import",
            json={"default_stop_points": 25},
        )
        self.assertEqual(imported.status_code, 200)
        self.assertGreater(imported.json()["imported"], 50)

        self.assertTrue(self.client.get("/api/overview").json()["has_data"])
        self.assertTrue(self.client.get("/api/equity").json()["curve"])
        self.assertTrue(self.client.get("/api/breakdown?by=hour").json()["buckets"])
        self.assertTrue(self.client.get("/api/trades?limit=5").json()["trades"])
        self.assertTrue(self.client.get("/api/behavior").json()["findings"])

    def test_an_unparseable_upload_returns_200_with_an_error_field(self):
        # The upload succeeded; it is the *content* that could not be read, and
        # the UI needs the record back so it can show the failure.
        response = self.upload(b"name,colour\nwidget,red\n", "junk.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.json()["error"])
        self.assertIsNone(response.json()["preview"])

    def test_importing_bar_data_is_a_400(self):
        statement_id = self.upload(BAR_EXPORT, "bars.txt").json()["statement"]["id"]
        response = self.client.post(f"/api/statements/{statement_id}/import", json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("market data", response.json()["detail"])

    def test_an_unknown_import_option_is_refused(self):
        statement_id = self.upload().json()["statement"]["id"]
        response = self.client.post(
            f"/api/statements/{statement_id}/import", json={"sneaky": True}
        )
        self.assertEqual(response.status_code, 400)

    def test_delete_removes_the_statement_and_its_trades(self):
        statement_id = self.upload().json()["statement"]["id"]
        self.client.post(f"/api/statements/{statement_id}/import", json={})
        response = self.client.delete(f"/api/statements/{statement_id}")
        self.assertEqual(response.json()["trades_removed"], 2)
        self.assertEqual(self.client.get("/api/statements").json()["statements"], [])

    def test_delete_can_keep_the_trades(self):
        statement_id = self.upload().json()["statement"]["id"]
        self.client.post(f"/api/statements/{statement_id}/import", json={})
        self.client.delete(f"/api/statements/{statement_id}?remove_trades=false")
        self.assertEqual(self.client.get("/api/overview").json()["trade_count"], 2)

    def test_sizing_over_http(self):
        body = self.client.post(
            "/api/size", json={"entry": 41_000, "stop": 40_980, "target": 41_040}
        ).json()
        self.assertTrue(body["approved"])
        self.assertAlmostEqual(body["reward_risk"], 2.0)

    def test_sizing_without_the_required_numbers_is_a_400(self):
        self.assertEqual(self.client.post("/api/size", json={}).status_code, 400)
        self.assertEqual(
            self.client.post("/api/size", json={"entry": "abc", "stop": 1}).status_code,
            400,
        )

    def test_an_unknown_breakdown_is_a_400(self):
        response = self.client.get("/api/breakdown?by=nonsense")
        self.assertEqual(response.status_code, 400)

    def test_the_trade_limit_is_bounded(self):
        self.assertEqual(self.client.get("/api/trades?limit=0").status_code, 422)
        self.assertEqual(self.client.get("/api/trades?limit=99999").status_code, 422)


class TestCrossOriginGuard(ApiCase):
    """The server is localhost-only, but another tab can still post to it."""

    def test_a_cross_site_write_is_refused(self):
        response = self.client.post(
            "/api/settings", json={"equity": 1},
            headers={"origin": "https://evil.example"},
        )
        self.assertEqual(response.status_code, 403)

    def test_a_cross_site_delete_is_refused(self):
        statement_id = self.upload().json()["statement"]["id"]
        response = self.client.delete(
            f"/api/statements/{statement_id}",
            headers={"origin": "https://evil.example"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.client.get("/api/statements").json()["statements"]), 1)

    def test_a_same_origin_write_is_allowed(self):
        response = self.client.put(
            "/api/settings", json={"equity": 30_000},
            headers={"origin": "http://127.0.0.1:8787"},
        )
        self.assertEqual(response.status_code, 200)

    def test_reads_are_never_blocked(self):
        response = self.client.get(
            "/api/overview", headers={"origin": "https://evil.example"}
        )
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
