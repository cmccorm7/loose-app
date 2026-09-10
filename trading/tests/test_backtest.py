"""The backtest engine: fills, exits, and the no-lookahead guarantee."""

import unittest
from datetime import date, datetime, timedelta

from ym.backtest import BacktestConfig, Backtester, Context, EntryType, Manage, Signal, Strategy
from ym.core import Bar, Direction, ExitReason
from ym.data import generate_bars
from ym.instruments import MYM, YM
from ym.risk import RiskLimits
from ym.sessions import exchange_tz
from ym.strategies import MovingAveragePullback, OpeningRangeBreakout

ET = exchange_tz()


def bars_from(specs, start_hour=10, start_minute=0, day=8):
    """Build 1-minute RTH bars from ``(open, high, low, close)`` tuples."""
    out = []
    when = datetime(2026, 9, day, start_hour, start_minute, tzinfo=ET)
    for index, (o, h, l, c) in enumerate(specs):
        out.append(Bar(when + timedelta(minutes=index), o, h, l, c, 100))
    return out


class SignalOnBar(Strategy):
    """Fires one signal on a chosen bar index, then records what it was shown."""

    name = "probe"

    def __init__(self, on_index, signal_factory):
        self.on_index = on_index
        self.signal_factory = signal_factory
        self.fired_at = None
        self.max_index_seen = -1

    def on_bar(self, context: Context):
        self.max_index_seen = max(self.max_index_seen, context.index)
        if context.index == self.on_index:
            self.fired_at = context.bar.ts
            return self.signal_factory(context)
        return None


def run(strategy, bars, instrument=MYM, equity=50_000, **config_kwargs):
    config = BacktestConfig(
        slippage_ticks=config_kwargs.pop("slippage_ticks", 0.0),
        **config_kwargs,
    )
    limits = RiskLimits(
        risk_per_trade_pct=2.0, max_contracts=5, max_daily_loss_pct=None,
        max_consecutive_losses=None, max_drawdown_pct=None, min_stop_ticks=1,
    )
    return Backtester(instrument, strategy, equity, limits, config).run(bars)


class TestNoLookahead(unittest.TestCase):
    def test_a_strategy_never_sees_beyond_the_current_bar(self):
        seen_lengths = []

        class Watcher(Strategy):
            name = "watcher"

            def on_bar(self, context):
                seen_lengths.append((context.index, len(context.lookback(1000))))
                return None

        bars = bars_from([(41000, 41010, 40990, 41005)] * 6)
        run(Watcher(), bars)
        for index, visible in seen_lengths:
            self.assertEqual(visible, index + 1)

    def test_entry_fills_after_the_signal_bar_not_on_it(self):
        strategy = SignalOnBar(
            1, lambda ctx: Signal(Direction.LONG, stop_points=20, target_r=2.0)
        )
        bars = bars_from([(41000, 41010, 40990, 41005)] * 6)
        result = run(strategy, bars)
        self.assertEqual(len(result.trades), 1)
        self.assertGreater(result.trades[0].entry_time, strategy.fired_at)
        self.assertEqual(result.trades[0].entry_time, bars[2].ts)


class TestFills(unittest.TestCase):
    def test_market_entry_fills_at_the_next_open_plus_slippage(self):
        strategy = SignalOnBar(0, lambda ctx: Signal(Direction.LONG, stop_points=30))
        bars = bars_from([
            (41000, 41010, 40990, 41005),
            (41020, 41030, 41015, 41025),
            (41025, 41035, 41020, 41030),
        ])
        result = run(strategy, bars, slippage_ticks=1.0)
        self.assertAlmostEqual(result.trades[0].entry_price, 41021.0)  # 41020 + 1 tick

    def test_stop_entry_waits_for_the_trigger(self):
        strategy = SignalOnBar(
            0,
            lambda ctx: Signal(Direction.LONG, entry_type=EntryType.STOP,
                               entry_price=41050, stop_points=30, valid_bars=10),
        )
        bars = bars_from([
            (41000, 41010, 40990, 41005),
            (41005, 41020, 41000, 41015),   # never reaches 41050
            (41015, 41060, 41010, 41055),   # trades through it
            (41055, 41060, 41050, 41058),
        ])
        result = run(strategy, bars)
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].entry_time, bars[2].ts)
        self.assertAlmostEqual(result.trades[0].entry_price, 41050.0)

    def test_stop_entry_that_gaps_fills_at_the_open(self):
        strategy = SignalOnBar(
            0,
            lambda ctx: Signal(Direction.LONG, entry_type=EntryType.STOP,
                               entry_price=41050, stop_points=40, valid_bars=10),
        )
        bars = bars_from([
            (41000, 41010, 40990, 41005),
            (41080, 41090, 41075, 41085),   # opens far above the trigger
            (41085, 41090, 41080, 41088),
        ])
        result = run(strategy, bars)
        self.assertAlmostEqual(result.trades[0].entry_price, 41080.0)

    def test_limit_entry_requires_price_to_trade_back(self):
        strategy = SignalOnBar(
            0,
            lambda ctx: Signal(Direction.LONG, entry_type=EntryType.LIMIT,
                               entry_price=40950, stop_points=30, valid_bars=2),
        )
        never = bars_from([(41000, 41010, 40990, 41005)] * 5)
        self.assertEqual(len(run(strategy, never).trades), 0)

        strategy = SignalOnBar(
            0,
            lambda ctx: Signal(Direction.LONG, entry_type=EntryType.LIMIT,
                               entry_price=40950, stop_points=30, valid_bars=5),
        )
        reaches = bars_from([
            (41000, 41010, 40990, 41005),
            (41000, 41005, 40940, 40960),
            (40960, 40990, 40955, 40985),
        ])
        result = run(strategy, reaches)
        self.assertAlmostEqual(result.trades[0].entry_price, 40950.0)

    def test_resting_orders_expire(self):
        strategy = SignalOnBar(
            0,
            lambda ctx: Signal(Direction.LONG, entry_type=EntryType.STOP,
                               entry_price=41500, stop_points=30, valid_bars=2),
        )
        bars = bars_from([(41000, 41010, 40990, 41005)] * 6)
        self.assertEqual(len(run(strategy, bars).trades), 0)


class TestExits(unittest.TestCase):
    def _one_trade(self, later_bars, slippage=0.0, **config):
        strategy = SignalOnBar(
            0, lambda ctx: Signal(Direction.LONG, stop_price=40980, target_price=41040)
        )
        bars = bars_from([(41000, 41010, 40990, 41005)] + later_bars)
        result = run(strategy, bars, slippage_ticks=slippage, **config)
        self.assertEqual(len(result.trades), 1, "expected exactly one trade")
        return result.trades[0]

    def test_target_fills_at_the_target_price(self):
        trade = self._one_trade([
            (41000, 41000, 41000, 41000),          # entry bar, flat
            (41000, 41045, 40995, 41040),          # touches the target
        ])
        self.assertIs(trade.exit_reason, ExitReason.TARGET)
        self.assertAlmostEqual(trade.exit_price, 41040.0)

    def test_stop_fills_at_the_stop_less_slippage(self):
        trade = self._one_trade([
            (41000, 41000, 41000, 41000),
            (41000, 41005, 40975, 40985),
        ], slippage=1.0)
        self.assertIs(trade.exit_reason, ExitReason.STOP)
        self.assertAlmostEqual(trade.exit_price, 40979.0)

    def test_a_bar_containing_both_levels_is_assumed_to_stop_out(self):
        trade = self._one_trade([
            (41000, 41000, 41000, 41000),
            (41000, 41050, 40970, 41030),          # spans stop and target
        ])
        self.assertIs(trade.exit_reason, ExitReason.STOP)

    def test_optimistic_mode_takes_the_target_instead(self):
        trade = self._one_trade([
            (41000, 41000, 41000, 41000),
            (41000, 41050, 40970, 41030),
        ], pessimistic_intrabar=False)
        self.assertIs(trade.exit_reason, ExitReason.TARGET)

    def test_a_gap_through_the_stop_fills_at_the_gap_price(self):
        trade = self._one_trade([
            (41000, 41000, 41000, 41000),
            (40940, 40950, 40930, 40945),          # opens below the stop
        ])
        self.assertIs(trade.exit_reason, ExitReason.STOP)
        self.assertAlmostEqual(trade.exit_price, 40940.0)
        self.assertLess(trade.r_multiple, -1.0, "a gap should cost more than 1R")

    def test_same_bar_stop_out_is_possible(self):
        strategy = SignalOnBar(
            0, lambda ctx: Signal(Direction.LONG, stop_price=40995, target_price=41100)
        )
        bars = bars_from([
            (41000, 41010, 40990, 41005),
            (41000, 41005, 40985, 40990),          # entry and stop in one bar
            (40990, 41000, 40985, 40995),
        ])
        result = run(strategy, bars)
        self.assertEqual(result.trades[0].entry_time, bars[1].ts)
        self.assertEqual(result.trades[0].exit_time, bars[1].ts)

    def test_positions_are_flattened_before_the_cash_close(self):
        strategy = SignalOnBar(
            0, lambda ctx: Signal(Direction.LONG, stop_points=500)
        )
        bars = bars_from([(41000, 41010, 40990, 41005)] * 8,
                         start_hour=15, start_minute=50)
        # The no-new-trades window would otherwise refuse an entry this late.
        result = run(strategy, bars, flatten_minutes_before_close=5,
                     no_new_trades_minutes_before_close=0)
        self.assertEqual(len(result.trades), 1)
        self.assertIs(result.trades[0].exit_reason, ExitReason.SESSION_CLOSE)
        self.assertLess(result.trades[0].exit_time.minute, 56)

    def test_max_bars_in_trade_forces_a_time_stop(self):
        strategy = SignalOnBar(0, lambda ctx: Signal(Direction.LONG, stop_points=500))
        bars = bars_from([(41000, 41010, 40990, 41005)] * 10)
        result = run(strategy, bars, max_bars_in_trade=3)
        self.assertIs(result.trades[0].exit_reason, ExitReason.TIME_STOP)

    def test_an_open_position_is_marked_out_at_the_end_of_data(self):
        strategy = SignalOnBar(0, lambda ctx: Signal(Direction.LONG, stop_points=500))
        bars = bars_from([(41000, 41010, 40990, 41005)] * 4)
        result = run(strategy, bars, flatten_minutes_before_close=None)
        self.assertIs(result.trades[0].exit_reason, ExitReason.END_OF_DATA)

    def test_manage_can_move_the_stop_and_exit(self):
        class Trailer(SignalOnBar):
            def manage(self, context, trade):
                return Manage(exit_now=True, reason="done")

        strategy = Trailer(0, lambda ctx: Signal(Direction.LONG, stop_points=500))
        bars = bars_from([(41000, 41010, 40990, 41005)] * 5)
        result = run(strategy, bars)
        self.assertIs(result.trades[0].exit_reason, ExitReason.SIGNAL)

    def test_initial_stop_is_preserved_when_the_stop_is_trailed(self):
        class Breakeven(SignalOnBar):
            def manage(self, context, trade):
                return Manage(new_stop=trade.entry_price)

        strategy = Breakeven(
            0, lambda ctx: Signal(Direction.LONG, stop_price=40900, target_price=41500)
        )
        bars = bars_from([
            (41000, 41010, 40990, 41005),
            (41000, 41010, 40995, 41005),
            (41005, 41010, 40950, 40960),   # below the moved stop, above the original
            (40960, 40970, 40950, 40965),
        ])
        result = run(strategy, bars)
        trade = result.trades[0]
        self.assertEqual(trade.stop_price, 40900, "1R must stay the original risk")
        self.assertIs(trade.exit_reason, ExitReason.STOP)
        self.assertGreater(trade.exit_price, 40900)


class TestRiskIntegration(unittest.TestCase):
    def test_signals_the_risk_rules_refuse_are_recorded_not_taken(self):
        strategy = SignalOnBar(0, lambda ctx: Signal(Direction.LONG, stop_points=400))
        bars = bars_from([(41000, 41010, 40990, 41005)] * 5)
        config = BacktestConfig(slippage_ticks=0.0)
        limits = RiskLimits(risk_per_trade_pct=0.1)  # $25 budget; far too small
        result = Backtester(YM, strategy, 25_000, limits, config).run(bars)
        self.assertEqual(len(result.trades), 0)
        self.assertEqual(len(result.blocked), 1)
        self.assertIn("size_zero", result.blocked_summary())

    def test_a_daily_loss_lockout_stops_further_entries(self):
        bars = generate_bars(date(2026, 6, 1), days=10, seed=4)
        limits = RiskLimits(risk_per_trade_pct=2.0, max_daily_loss_pct=0.5,
                            max_consecutive_losses=None)
        result = Backtester(
            MYM, MovingAveragePullback(), 25_000, limits,
            BacktestConfig(slippage_ticks=1.0),
        ).run(bars)
        codes = result.blocked_summary()
        self.assertTrue(
            any("daily_loss" in code for code in codes),
            f"expected a daily-loss block, saw {codes}",
        )

    def test_commission_is_charged_as_a_round_turn(self):
        strategy = SignalOnBar(0, lambda ctx: Signal(Direction.LONG, stop_points=30))
        bars = bars_from([(41000, 41010, 40990, 41005)] * 5)
        instrument = MYM.with_costs(commission_per_side=0.75)
        config = BacktestConfig(slippage_ticks=0.0)
        limits = RiskLimits(risk_per_trade_pct=1.0, max_contracts=2, min_stop_ticks=1)
        result = Backtester(instrument, strategy, 25_000, limits, config).run(bars)
        trade = result.trades[0]
        self.assertAlmostEqual(trade.commission, 0.75 * 2 * trade.contracts)

    def test_entries_stop_before_the_close(self):
        strategy = SignalOnBar(1, lambda ctx: Signal(Direction.LONG, stop_points=30))
        bars = bars_from([(41000, 41010, 40990, 41005)] * 6,
                         start_hour=15, start_minute=40)
        result = run(strategy, bars, no_new_trades_minutes_before_close=30)
        self.assertEqual(len(result.trades), 0)


class TestStrategies(unittest.TestCase):
    def test_opening_range_breakout_runs_and_respects_its_daily_cap(self):
        bars = generate_bars(date(2026, 6, 1), days=15, seed=11)
        result = Backtester(
            MYM, OpeningRangeBreakout(range_minutes=30, max_trades_per_day=1),
            25_000, RiskLimits(risk_per_trade_pct=1.0),
            BacktestConfig(slippage_ticks=1.0),
        ).run(bars)
        from collections import Counter
        from ym.sessions import DEFAULT_SESSION
        per_day = Counter(
            DEFAULT_SESSION.session_day(trade.entry_time) for trade in result.trades
        )
        self.assertTrue(per_day, "expected the breakout to trade at least once")
        self.assertLessEqual(max(per_day.values()), 1)

    def test_every_trade_carries_the_fields_analysis_needs(self):
        bars = generate_bars(date(2026, 6, 1), days=15, seed=11)
        result = Backtester(
            MYM, OpeningRangeBreakout(), 25_000, RiskLimits(risk_per_trade_pct=1.0),
            BacktestConfig(),
        ).run(bars)
        for trade in result.trades:
            self.assertIsNotNone(trade.stop_price)
            self.assertIsNotNone(trade.r_multiple)
            self.assertIsNotNone(trade.exit_reason)
            self.assertGreaterEqual(trade.mfe_points, 0)
            self.assertGreaterEqual(trade.mae_points, 0)

    def test_results_are_deterministic(self):
        bars = generate_bars(date(2026, 6, 1), days=10, seed=2)
        runs = [
            Backtester(MYM, OpeningRangeBreakout(), 25_000,
                       RiskLimits(risk_per_trade_pct=1.0), BacktestConfig()).run(bars)
            for _ in range(2)
        ]
        self.assertEqual(
            [t.net_pnl for t in runs[0].trades], [t.net_pnl for t in runs[1].trades]
        )

    def test_empty_input_is_rejected(self):
        with self.assertRaises(ValueError):
            run(SignalOnBar(0, lambda ctx: None), [])


if __name__ == "__main__":
    unittest.main()
