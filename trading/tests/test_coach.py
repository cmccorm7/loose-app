"""The in-session coach: the right nudge at the right moment, and silence
otherwise."""

import unittest
from datetime import date, datetime, time, timedelta

from ym.behavior import analyze
from ym.coach import BehaviorProfile, Coach
from ym.core import Direction, ExitReason, Trade
from ym.data.trader import Habits, simulate_trader_history
from ym.instruments import MYM
from ym.risk import RiskLimits, RiskManager
from ym.sessions import exchange_tz

ET = exchange_tz()
DAY = date(2026, 9, 8)
FADES_AFTER_TWO = Habits(
    early_edge_r=0.4, late_edge_r=-0.5, edge_decays_after=2,
    revenge_probability=0.65, stop_overshoot=0.45,
)


def booked(hour, minute, points, contracts=2):
    entry = datetime.combine(DAY, time(hour, minute), tzinfo=ET)
    trade = Trade("MYM", Direction.LONG, entry, 41_000, contracts, 0.5,
                  stop_price=40_975, commission=2.0, setup="ORB")
    trade.close(entry + timedelta(minutes=12), 41_000 + points,
                ExitReason.TARGET if points > 0 else ExitReason.STOP)
    return trade


class CoachCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.history = simulate_trader_history(
            days=60, habits=FADES_AFTER_TWO, seed=42
        )
        cls.report = analyze(cls.history)
        cls.coach = Coach.from_trades(cls.history)

    def session(self, limits=None, trades=()):
        manager = RiskManager(MYM, 25_000, limits or RiskLimits(risk_per_trade_pct=0.5))
        for trade in trades:
            manager.register_trade(trade)
        return manager

    def codes(self, nudges):
        return [nudge.message for nudge in nudges]


class TestProfile(CoachCase):
    def test_the_profile_extracts_the_actionable_patterns(self):
        profile = BehaviorProfile.from_report(self.report)
        self.assertEqual(profile.unit, "R")
        self.assertEqual(profile.decay_ordinal, 2)
        self.assertIsNotNone(profile.fade_hour)
        self.assertIsNotNone(profile.post_loss_window)
        self.assertTrue(profile.sizes_up_after_loss)
        self.assertTrue(profile.by_hour)
        self.assertTrue(profile.by_ordinal)

    def test_an_empty_profile_is_harmless(self):
        coach = Coach()
        manager = self.session()
        nudges = coach.check(datetime.combine(DAY, time(10, 0), tzinfo=ET), manager)
        self.assertEqual(nudges, [])


class TestNudges(CoachCase):
    def test_nothing_is_said_before_the_open_on_a_clean_slate(self):
        manager = self.session()
        nudges = self.coach.check(datetime.combine(DAY, time(9, 20), tzinfo=ET), manager)
        self.assertEqual(nudges, [], self.codes(nudges))

    def test_the_trade_after_the_decay_point_is_flagged(self):
        trades = [booked(9, 45, 50), booked(10, 40, 34)]
        manager = self.session(trades=trades)
        nudges = self.coach.check(
            datetime.combine(DAY, time(10, 55), tzinfo=ET), manager, trades
        )
        messages = " ".join(self.codes(nudges))
        self.assertIn("trade 3", messages)
        self.assertTrue(any(nudge.urgency == "stop" for nudge in nudges))

    def test_the_last_allowed_trade_gets_a_gentler_note(self):
        trades = [booked(9, 45, 50)]
        manager = self.session(trades=trades)
        nudges = self.coach.check(
            datetime.combine(DAY, time(10, 10), tzinfo=ET), manager, trades
        )
        messages = " ".join(self.codes(nudges))
        self.assertIn("last planned trade", messages)

    def test_the_afternoon_is_flagged(self):
        manager = self.session()
        nudges = self.coach.check(
            datetime.combine(DAY, time(14, 30), tzinfo=ET), manager
        )
        self.assertTrue(
            any("has lost money historically" in nudge.message for nudge in nudges)
        )

    def test_re_entering_just_after_a_loss_is_flagged(self):
        trades = [booked(9, 45, -26)]
        manager = self.session(trades=trades)
        just_after = trades[0].exit_time + timedelta(minutes=3)
        nudges = self.coach.check(just_after, manager, trades)
        messages = " ".join(self.codes(nudges))
        self.assertIn("took a loss", messages)

    def test_waiting_out_the_window_clears_that_nudge(self):
        trades = [booked(9, 45, -26)]
        manager = self.session(trades=trades)
        profile = self.coach.profile
        later = trades[0].exit_time + timedelta(
            minutes=(profile.post_loss_window or 15) + 10
        )
        messages = " ".join(self.codes(self.coach.check(later, manager, trades)))
        self.assertNotIn("took a loss", messages)

    def test_sizing_up_after_a_loss_is_called_out(self):
        trades = [booked(9, 45, -26)]
        manager = self.session(trades=trades)
        nudges = self.coach.check(
            datetime.combine(DAY, time(11, 30), tzinfo=ET), manager, trades
        )
        self.assertTrue(any("sizing up" in nudge.message for nudge in nudges))

    def test_giving_back_a_green_day_is_called_out(self):
        limits = RiskLimits(risk_per_trade_pct=0.5, max_daily_giveback_pct=50.0,
                            max_consecutive_losses=None)
        trades = [booked(9, 45, 200), booked(10, 30, -80)]
        manager = self.session(limits, trades)
        nudges = self.coach.check(
            datetime.combine(DAY, time(10, 50), tzinfo=ET), manager, trades
        )
        self.assertTrue(any("off today's" in nudge.message for nudge in nudges))

    def test_a_hard_lockout_is_reported_first(self):
        limits = RiskLimits(risk_per_trade_pct=0.5, max_trades_per_day=1,
                            max_consecutive_losses=None)
        trades = [booked(9, 45, 50)]
        manager = self.session(limits, trades)
        nudges = self.coach.check(
            datetime.combine(DAY, time(10, 30), tzinfo=ET), manager, trades
        )
        self.assertEqual(nudges[0].urgency, "stop")
        self.assertIn("locked out", nudges[0].message)

    def test_the_approaching_hard_stop_is_announced(self):
        limits = RiskLimits(risk_per_trade_pct=0.5, hard_stop_time=time(12, 0))
        manager = self.session(limits)
        nudges = self.coach.check(
            datetime.combine(DAY, time(11, 50), tzinfo=ET), manager
        )
        self.assertTrue(any("hard stop" in nudge.message for nudge in nudges))

    def test_one_more_loss_ending_the_day_is_announced(self):
        limits = RiskLimits(risk_per_trade_pct=0.5, max_consecutive_losses=2,
                            max_daily_loss_pct=None)
        trades = [booked(9, 45, -26)]
        manager = self.session(limits, trades)
        nudges = self.coach.check(
            datetime.combine(DAY, time(10, 10), tzinfo=ET), manager, trades
        )
        self.assertTrue(any("ends your day" in nudge.message for nudge in nudges))

    def test_a_shrinking_loss_budget_is_announced(self):
        limits = RiskLimits(risk_per_trade_pct=2.0, max_daily_loss_pct=2.0,
                            max_consecutive_losses=None)
        # -$400 of a $500 daily limit: some budget left, but less than a
        # full-size trade -- which is exactly when the nudge should fire.
        trades = [booked(9, 45, -400)]
        manager = self.session(limits, trades)
        self.assertGreater(manager.remaining_daily_risk(), 0)
        nudges = self.coach.check(
            datetime.combine(DAY, time(10, 10), tzinfo=ET), manager, trades
        )
        self.assertTrue(any("loss budget" in nudge.message for nudge in nudges))

    def test_nudges_are_sorted_by_urgency(self):
        trades = [booked(9, 45, 50), booked(10, 40, 34), booked(12, 40, -26)]
        manager = self.session(trades=trades)
        nudges = self.coach.check(
            datetime.combine(DAY, time(13, 5), tzinfo=ET), manager, trades
        )
        order = {"stop": 0, "caution": 1, "note": 2}
        urgencies = [order[nudge.urgency] for nudge in nudges]
        self.assertEqual(urgencies, sorted(urgencies))

    def test_brief_includes_the_account_state_and_the_nudges(self):
        trades = [booked(9, 45, 50), booked(10, 40, 34)]
        manager = self.session(trades=trades)
        text = self.coach.brief(
            datetime.combine(DAY, time(13, 5), tzinfo=ET), manager, trades
        )
        self.assertIn("Equity", text)
        self.assertIn("Coach:", text)

    def test_brief_says_so_when_there_is_nothing_to_say(self):
        text = Coach().brief(datetime.combine(DAY, time(10, 0), tzinfo=ET),
                             self.session())
        self.assertIn("Nothing flagged", text)


if __name__ == "__main__":
    unittest.main()
