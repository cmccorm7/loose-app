"""The risk engine: sizing, budgets and circuit breakers."""

import unittest
from datetime import datetime, time, timedelta

from ym.core import Direction, ExitReason, Trade
from ym.instruments import MYM, YM
from ym.risk import Lockout, RiskLimits, RiskManager, SizingMethod
from ym.sessions import exchange_tz

ET = exchange_tz()
MORNING = datetime(2026, 9, 8, 10, 0, tzinfo=ET)


def closed_trade(instrument, pnl_dollars, contracts=1, when=MORNING, stop_points=20):
    """A trade engineered to net exactly ``pnl_dollars``."""
    points = pnl_dollars / (instrument.point_value * contracts)
    trade = Trade(
        instrument.symbol, Direction.LONG, when, 41000.0, contracts,
        instrument.point_value, stop_price=41000.0 - stop_points, commission=0.0,
    )
    trade.close(
        when + timedelta(minutes=10), 41000.0 + points,
        ExitReason.TARGET if pnl_dollars > 0 else ExitReason.STOP,
    )
    return trade


class TestSizing(unittest.TestCase):
    def test_contracts_follow_the_stop_distance(self):
        manager = RiskManager(MYM, 25000, RiskLimits(risk_per_trade_pct=1.0))
        # $250 budget, 20 pt stop on MYM = $10 per contract -> 25, capped at 10.
        decision = manager.evaluate(MORNING, 41000, 40980)
        self.assertTrue(decision.approved)
        self.assertEqual(decision.contracts, 10)

    def test_wider_stop_buys_fewer_contracts(self):
        manager = RiskManager(MYM, 25000, RiskLimits(risk_per_trade_pct=0.2))
        tight = manager.evaluate(MORNING, 41000, 40990).contracts
        wide = manager.evaluate(MORNING, 41000, 40900).contracts
        self.assertGreater(tight, wide)

    def test_risk_never_exceeds_the_budget(self):
        manager = RiskManager(YM, 25000, RiskLimits(risk_per_trade_pct=0.5))
        decision = manager.evaluate(MORNING, 41000, 40985)
        self.assertLessEqual(decision.risk_dollars, 25000 * 0.005 + 1e-9)

    def test_stop_too_wide_for_one_contract_is_refused(self):
        manager = RiskManager(YM, 25000, RiskLimits(risk_per_trade_pct=0.5))
        decision = manager.evaluate(MORNING, 41000, 40900)  # 100 pts = $500
        self.assertFalse(decision.approved)
        self.assertEqual(decision.contracts, 0)
        self.assertIn("size_zero", [v.code for v in decision.blockers])

    def test_stop_too_tight_is_refused(self):
        manager = RiskManager(YM, 25000, RiskLimits(min_stop_ticks=4))
        decision = manager.evaluate(MORNING, 41000, 40998)
        self.assertIn("stop_too_tight", [v.code for v in decision.blockers])

    def test_no_stop_at_all_is_refused(self):
        manager = RiskManager(YM, 25000)
        decision = manager.evaluate(MORNING, 41000, 41000)
        self.assertIn("no_stop", [v.code for v in decision.blockers])

    def test_minimum_reward_risk_is_enforced(self):
        manager = RiskManager(MYM, 25000, RiskLimits(min_reward_risk=2.0))
        self.assertFalse(manager.evaluate(MORNING, 41000, 40980, 41020).approved)
        self.assertTrue(manager.evaluate(MORNING, 41000, 40980, 41045).approved)

    def test_fixed_dollar_sizing(self):
        limits = RiskLimits(sizing=SizingMethod.FIXED_DOLLAR, fixed_dollar_risk=100.0)
        manager = RiskManager(YM, 50000, limits)
        decision = manager.evaluate(MORNING, 41000, 40980)  # $100 per contract
        self.assertEqual(decision.contracts, 1)

    def test_fixed_contracts_sizing_ignores_equity(self):
        limits = RiskLimits(sizing=SizingMethod.FIXED_CONTRACTS, fixed_contracts=3)
        manager = RiskManager(MYM, 5000, limits)
        self.assertEqual(manager.evaluate(MORNING, 41000, 40980).contracts, 3)

    def test_margin_utilization_caps_size(self):
        instrument = MYM.with_costs(day_margin=1000.0)
        limits = RiskLimits(risk_per_trade_pct=5.0, max_margin_utilization=0.5)
        manager = RiskManager(instrument, 4000, limits)
        # 50% of $4,000 = $2,000 of margin, at $1,000 each -> 2 contracts.
        self.assertEqual(manager.evaluate(MORNING, 41000, 40980).contracts, 2)

    def test_strategy_request_can_only_reduce_size(self):
        manager = RiskManager(MYM, 25000, RiskLimits(risk_per_trade_pct=1.0))
        self.assertEqual(
            manager.evaluate(MORNING, 41000, 40980, requested_contracts=2).contracts, 2
        )


class TestDailyBudget(unittest.TestCase):
    def test_remaining_budget_shrinks_with_losses(self):
        manager = RiskManager(MYM, 25000, RiskLimits(max_daily_loss_pct=2.0))
        manager.roll_to(MORNING)
        self.assertAlmostEqual(manager.remaining_daily_risk(), 500.0)
        manager.register_trade(closed_trade(MYM, -200))
        self.assertAlmostEqual(manager.remaining_daily_risk(), 300.0)

    def test_remaining_budget_caps_the_next_trade(self):
        limits = RiskLimits(risk_per_trade_pct=2.0, max_daily_loss_pct=2.0,
                            max_consecutive_losses=None)
        manager = RiskManager(MYM, 25000, limits)
        manager.register_trade(closed_trade(MYM, -450))
        decision = manager.evaluate(MORNING, 41000, 40980)
        self.assertTrue(decision.approved)
        self.assertLessEqual(decision.risk_dollars, 50.0 + 1e-9)
        self.assertTrue(any("daily loss allowance" in note for note in decision.notes))

    def test_profits_do_not_enlarge_the_daily_budget(self):
        manager = RiskManager(MYM, 25000, RiskLimits(max_daily_loss_pct=2.0))
        manager.register_trade(closed_trade(MYM, 1000))
        self.assertAlmostEqual(manager.remaining_daily_risk(), 500.0)

    def test_tighter_of_dollar_and_percent_limits_applies(self):
        limits = RiskLimits(max_daily_loss_pct=2.0, max_daily_loss=150.0)
        manager = RiskManager(MYM, 25000, limits)
        manager.roll_to(MORNING)
        self.assertAlmostEqual(manager.daily_loss_limit(), 150.0)


class TestBreakers(unittest.TestCase):
    def test_daily_loss_limit_locks_the_day(self):
        limits = RiskLimits(max_daily_loss_pct=2.0, max_consecutive_losses=None)
        manager = RiskManager(MYM, 25000, limits)
        tripped = manager.register_trade(closed_trade(MYM, -500))
        self.assertIn("daily_loss_limit", [v.code for v in tripped])
        self.assertIs(manager.state.lockout, Lockout.DAILY_LOSS)
        self.assertFalse(manager.evaluate(MORNING, 41000, 40980).approved)

    def test_lockout_clears_on_the_next_trading_day(self):
        limits = RiskLimits(max_daily_loss_pct=2.0, max_consecutive_losses=None)
        manager = RiskManager(MYM, 25000, limits)
        manager.register_trade(closed_trade(MYM, -500))
        self.assertIs(manager.state.lockout, Lockout.DAILY_LOSS)
        manager.roll_to(MORNING + timedelta(days=1))
        self.assertIs(manager.state.lockout, Lockout.NONE)
        self.assertEqual(manager.state.day_pnl, 0.0)

    def test_drawdown_lock_survives_the_day_boundary(self):
        limits = RiskLimits(max_drawdown_pct=5.0, max_daily_loss_pct=None,
                            max_consecutive_losses=None)
        manager = RiskManager(MYM, 10000, limits)
        manager.register_trade(closed_trade(MYM, -600))
        self.assertIs(manager.state.lockout, Lockout.DRAWDOWN)
        manager.roll_to(MORNING + timedelta(days=3))
        self.assertIs(manager.state.lockout, Lockout.DRAWDOWN)

    def test_consecutive_losses_trigger_a_timed_cooldown(self):
        limits = RiskLimits(max_consecutive_losses=2, cooldown_minutes=30,
                            max_daily_loss_pct=None)
        manager = RiskManager(MYM, 25000, limits)
        manager.register_trade(closed_trade(MYM, -50, when=MORNING))
        manager.register_trade(closed_trade(MYM, -50, when=MORNING + timedelta(minutes=20)))
        self.assertIs(manager.state.lockout, Lockout.COOLDOWN)
        still_locked = MORNING + timedelta(minutes=40)
        self.assertFalse(manager.evaluate(still_locked, 41000, 40980).approved)
        expired = MORNING + timedelta(minutes=70)
        self.assertTrue(manager.evaluate(expired, 41000, 40980).approved)

    def test_a_win_resets_the_losing_streak(self):
        limits = RiskLimits(max_consecutive_losses=3, max_daily_loss_pct=None)
        manager = RiskManager(MYM, 25000, limits)
        manager.register_trade(closed_trade(MYM, -50))
        manager.register_trade(closed_trade(MYM, 60))
        self.assertEqual(manager.state.consecutive_losses, 0)

    def test_lock_day_action_holds_past_the_cooldown(self):
        limits = RiskLimits(max_consecutive_losses=2, consecutive_loss_action="lock_day",
                            max_daily_loss_pct=None)
        manager = RiskManager(MYM, 25000, limits)
        manager.register_trade(closed_trade(MYM, -50))
        manager.register_trade(closed_trade(MYM, -50))
        self.assertIs(manager.state.lockout, Lockout.CONSECUTIVE_LOSSES)
        later = MORNING + timedelta(hours=4)
        self.assertFalse(manager.evaluate(later, 41000, 40980).approved)

    def test_trade_count_limit(self):
        limits = RiskLimits(max_trades_per_day=2, max_daily_loss_pct=None,
                            max_consecutive_losses=None)
        manager = RiskManager(MYM, 25000, limits)
        manager.register_trade(closed_trade(MYM, 10))
        manager.register_trade(closed_trade(MYM, 10))
        self.assertIs(manager.state.lockout, Lockout.TRADE_COUNT)

    def test_profit_target_ends_the_session(self):
        limits = RiskLimits(daily_profit_target=300.0, max_daily_loss_pct=None)
        manager = RiskManager(MYM, 25000, limits)
        tripped = manager.register_trade(closed_trade(MYM, 350))
        self.assertIn("daily_profit_target", [v.code for v in tripped])
        self.assertIs(manager.state.lockout, Lockout.PROFIT_TARGET)

    def test_giveback_limit_banks_a_green_day(self):
        limits = RiskLimits(max_daily_giveback_pct=50.0, max_daily_loss_pct=None,
                            max_consecutive_losses=None)
        manager = RiskManager(MYM, 25000, limits)
        manager.register_trade(closed_trade(MYM, 200))
        self.assertIs(manager.state.lockout, Lockout.NONE)
        manager.register_trade(closed_trade(MYM, -60))   # 30% of the peak
        self.assertIs(manager.state.lockout, Lockout.NONE)
        manager.register_trade(closed_trade(MYM, -50))   # now 55%
        self.assertIs(manager.state.lockout, Lockout.GIVEBACK)
        self.assertEqual(manager.state.day_peak_pnl, 200.0)

    def test_giveback_limit_ignores_a_day_that_was_never_green(self):
        limits = RiskLimits(max_daily_giveback_pct=50.0, max_daily_loss_pct=None,
                            max_consecutive_losses=None)
        manager = RiskManager(MYM, 25000, limits)
        manager.register_trade(closed_trade(MYM, -100))
        self.assertIs(manager.state.lockout, Lockout.NONE)

    def test_hard_stop_time_blocks_new_entries(self):
        limits = RiskLimits(hard_stop_time=time(12, 0))
        manager = RiskManager(MYM, 25000, limits)
        self.assertTrue(manager.evaluate(MORNING, 41000, 40980).approved)
        afternoon = datetime(2026, 9, 8, 13, 30, tzinfo=ET)
        decision = manager.evaluate(afternoon, 41000, 40980)
        self.assertFalse(decision.approved)
        self.assertIn("hard_stop_time", [v.code for v in decision.blockers])

    def test_weekly_loss_limit(self):
        limits = RiskLimits(max_weekly_loss_pct=3.0, max_daily_loss_pct=None,
                            max_consecutive_losses=None, max_drawdown_pct=None)
        manager = RiskManager(MYM, 10000, limits)
        manager.register_trade(closed_trade(MYM, -200, when=MORNING))
        manager.register_trade(
            closed_trade(MYM, -150, when=MORNING + timedelta(days=1))
        )
        self.assertIs(manager.state.lockout, Lockout.WEEKLY_LOSS)

    def test_only_closed_trades_can_be_registered(self):
        manager = RiskManager(MYM, 25000)
        open_trade = Trade("MYM", Direction.LONG, MORNING, 41000, 1, 0.5,
                           stop_price=40980)
        with self.assertRaises(ValueError):
            manager.register_trade(open_trade)

    def test_status_reports_the_lockout(self):
        limits = RiskLimits(max_daily_loss_pct=2.0, max_consecutive_losses=None)
        manager = RiskManager(MYM, 25000, limits)
        manager.register_trade(closed_trade(MYM, -500))
        status = manager.status()
        self.assertEqual(status["lockout"], "daily_loss_limit")
        self.assertIn("LOCKED OUT", manager.format_status())


if __name__ == "__main__":
    unittest.main()
