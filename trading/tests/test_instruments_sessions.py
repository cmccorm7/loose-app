"""Contract specs and the two clocks (calendar day vs trading day)."""

import unittest
from datetime import date, datetime

from ym.core import Bar
from ym.instruments import MYM, YM, get_instrument
from ym.sessions import DEFAULT_SESSION, Session, exchange_tz

ET = exchange_tz()


class TestInstruments(unittest.TestCase):
    def test_tick_values(self):
        self.assertEqual(YM.tick_value, 5.0)
        self.assertEqual(MYM.tick_value, 0.5)
        self.assertEqual(MYM.point_value * 10, YM.point_value)

    def test_points_to_dollars_scales_with_contracts(self):
        self.assertEqual(YM.points_to_dollars(50, 2), 500.0)
        self.assertEqual(MYM.points_to_dollars(50, 2), 50.0)

    def test_dollars_to_points_round_trips(self):
        self.assertAlmostEqual(YM.dollars_to_points(YM.points_to_dollars(37, 3), 3), 37)

    def test_rounding_modes(self):
        self.assertEqual(YM.round_to_tick(41234.4), 41234.0)
        self.assertEqual(YM.round_to_tick(41234.6), 41235.0)
        self.assertEqual(YM.round_to_tick(41234.2, "up"), 41235.0)
        self.assertEqual(YM.round_to_tick(41234.8, "down"), 41234.0)

    def test_symbol_lookup_tolerates_contract_month(self):
        self.assertIs(get_instrument("YMZ5"), YM)
        self.assertIs(get_instrument("mym 12-26"[:3]), MYM)
        with self.assertRaises(KeyError):
            get_instrument("ES")

    def test_with_costs_does_not_mutate_the_shared_spec(self):
        custom = YM.with_costs(commission_per_side=1.25)
        self.assertEqual(custom.commission_per_side, 1.25)
        self.assertEqual(YM.commission_per_side, 2.00)


class TestSessions(unittest.TestCase):
    def test_evening_trades_belong_to_the_next_trading_day(self):
        self.assertEqual(
            DEFAULT_SESSION.session_day(datetime(2026, 9, 8, 20, 30, tzinfo=ET)),
            date(2026, 9, 9),
        )

    def test_daytime_trades_belong_to_the_same_day(self):
        self.assertEqual(
            DEFAULT_SESSION.session_day(datetime(2026, 9, 8, 10, 0, tzinfo=ET)),
            date(2026, 9, 8),
        )

    def test_friday_evening_rolls_to_monday(self):
        self.assertEqual(
            DEFAULT_SESSION.session_day(datetime(2026, 9, 11, 19, 0, tzinfo=ET)),
            date(2026, 9, 14),
        )

    def test_session_classification(self):
        cases = {
            datetime(2026, 9, 8, 10, 0, tzinfo=ET): Session.RTH,
            datetime(2026, 9, 8, 5, 0, tzinfo=ET): Session.PREMARKET,
            datetime(2026, 9, 8, 16, 30, tzinfo=ET): Session.AFTERNOON,
            datetime(2026, 9, 8, 17, 30, tzinfo=ET): Session.CLOSED,
            datetime(2026, 9, 8, 19, 0, tzinfo=ET): Session.OVERNIGHT,
            datetime(2026, 9, 12, 12, 0, tzinfo=ET): Session.CLOSED,  # Saturday
        }
        for when, expected in cases.items():
            with self.subTest(when=when):
                self.assertIs(DEFAULT_SESSION.session_of(when), expected)

    def test_naive_datetimes_are_rejected_not_guessed(self):
        with self.assertRaises(ValueError):
            DEFAULT_SESSION.session_day(datetime(2026, 9, 8, 10, 0))
        with self.assertRaises(ValueError):
            Bar(datetime(2026, 9, 8, 10, 0), 1, 2, 0.5, 1.5)

    def test_minutes_since_open(self):
        self.assertAlmostEqual(
            DEFAULT_SESSION.minutes_since_rth_open(
                datetime(2026, 9, 8, 10, 0, tzinfo=ET)
            ),
            30.0,
        )

    def test_flatten_time_precedes_the_cash_close(self):
        self.assertEqual(
            DEFAULT_SESSION.flatten_time(date(2026, 9, 8), 5),
            datetime(2026, 9, 8, 15, 55, tzinfo=ET),
        )


if __name__ == "__main__":
    unittest.main()
