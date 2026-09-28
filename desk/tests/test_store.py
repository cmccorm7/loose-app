"""Settings and the statement manifest: plain JSON that must survive restarts."""

import json
import unittest

from tests.helpers import HubCase

from hub.config import DEFAULT_SETTINGS, paths
from hub.store import StatementRecord, Store


class TestStore(HubCase):
    def setUp(self):
        super().setUp()
        self.store = Store(paths(self.home))

    def record(self, statement_id="abc123", **overrides):
        fields = dict(
            id=statement_id, filename="nt.csv", stored_as=f"{statement_id}__nt.csv",
            kind="trades", uploaded_at="2026-09-08T10:00:00+00:00", size_bytes=100,
        )
        fields.update(overrides)
        return StatementRecord(**fields)

    def test_defaults_are_returned_before_anything_is_saved(self):
        self.assertEqual(self.store.settings(), DEFAULT_SETTINGS)

    def test_settings_persist(self):
        self.store.update_settings({"equity": 12_345})
        self.assertEqual(Store(paths(self.home)).settings()["equity"], 12_345)

    def test_a_corrupt_settings_file_does_not_stop_the_app(self):
        self.store.paths.settings.write_text("{ not json")
        self.assertEqual(self.store.settings(), DEFAULT_SETTINGS)

    def test_a_corrupt_manifest_does_not_stop_the_app(self):
        self.store.paths.manifest.write_text("[[[")
        self.assertEqual(self.store.statements(), [])

    def test_saving_is_idempotent_on_id(self):
        self.store.save_statement(self.record())
        self.store.save_statement(self.record(status="imported", rows_imported=7))
        records = self.store.statements()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].rows_imported, 7)

    def test_statements_come_back_newest_first(self):
        self.store.save_statement(self.record("old", uploaded_at="2026-01-01T00:00:00+00:00"))
        self.store.save_statement(self.record("new", uploaded_at="2026-09-01T00:00:00+00:00"))
        self.assertEqual([item.id for item in self.store.statements()], ["new", "old"])

    def test_removing_a_statement(self):
        self.store.save_statement(self.record())
        self.assertIsNotNone(self.store.remove_statement("abc123"))
        self.assertEqual(self.store.statements(), [])
        self.assertIsNone(self.store.remove_statement("abc123"))

    def test_the_source_tag_identifies_the_upload(self):
        self.assertEqual(self.record("xyz").source_tag, "statement:xyz")

    def test_unknown_fields_in_the_manifest_are_ignored(self):
        # A manifest written by a newer version must not crash an older one.
        self.store.paths.manifest.write_text(json.dumps([{
            **self.record().to_dict(), "future_field": "surprise",
        }]))
        self.assertEqual(len(self.store.statements()), 1)

    def test_writes_are_atomic(self):
        self.store.save_statement(self.record())
        leftovers = list(self.home.glob("*.tmp"))
        self.assertEqual(leftovers, [])


class TestPaths(HubCase):
    def test_the_data_directory_is_created(self):
        self.assertTrue(self.home.is_dir())
        self.assertTrue((self.home / "uploads").is_dir())

    def test_the_environment_variable_is_honoured(self):
        import os
        from pathlib import Path
        target = self.home / "elsewhere"
        os.environ["YM_DESK_HOME"] = str(target)
        self.addCleanup(os.environ.pop, "YM_DESK_HOME", None)
        self.assertEqual(paths().home, Path(target).resolve())


if __name__ == "__main__":
    unittest.main()


class TestSettingValidation(HubCase):
    """A bad value caught here names the setting; caught later it surfaces as a
    failure deep in analysis, a long way from the mistake."""

    def test_a_choice_setting_rejects_anything_else(self):
        from hub.services import HubError
        with self.assertRaises(HubError) as caught:
            self.hub.update_settings({"location_mode": "vibes"})
        self.assertIn("location_mode", str(caught.exception))

    def test_both_valid_location_rules_are_accepted(self):
        for mode in ("stop_distance", "atr_zone"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    self.hub.update_settings({"location_mode": mode})["location_mode"],
                    mode,
                )

    def test_numbers_must_be_numbers(self):
        from hub.services import HubError
        with self.assertRaises(HubError):
            self.hub.update_settings({"equity": "lots"})

    def test_numbers_must_be_positive(self):
        from hub.services import HubError
        for change in ({"equity": -5}, {"risk_per_trade_pct": 0}):
            with self.subTest(change=change):
                with self.assertRaises(HubError):
                    self.hub.update_settings(change)

    def test_clearing_an_optional_number_is_allowed(self):
        self.hub.update_settings({"fixed_dollar_risk": 10.0})
        self.assertIsNone(
            self.hub.update_settings({"fixed_dollar_risk": None})["fixed_dollar_risk"]
        )

    def test_a_mistyped_timezone_is_caught(self):
        # A typo here silently shifts every session boundary.
        from hub.services import HubError
        with self.assertRaises(HubError):
            self.hub.update_settings({"timezone": "America/New_Yrok"})

    def test_a_real_timezone_is_accepted(self):
        self.assertEqual(
            self.hub.update_settings({"timezone": "America/Chicago"})["timezone"],
            "America/Chicago",
        )

    def test_a_refused_change_leaves_the_settings_alone(self):
        from hub.services import HubError
        before = self.hub.settings()
        with self.assertRaises(HubError):
            self.hub.update_settings({"equity": 9_000, "location_mode": "vibes"})
        self.assertEqual(self.hub.settings(), before)
