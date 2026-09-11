"""The support-rejection strategy: does it take the trade the idea describes,
and place risk where the idea says risk belongs?"""

import unittest
from datetime import date

from tests.test_levels import TRIPLE_REJECTION, WARMUP, build

# An ATR-scaled tolerance needs bars behind it before the first level can
# form, so every scripted path here starts with the warm-up lead-in.
SCRIPTED = WARMUP + TRIPLE_REJECTION + [("leg", 41_100, 20)]

from ym.backtest import BacktestConfig, Backtester
from ym.core import Direction
from ym.data import filter_session, generate_bars, resample
from ym.instruments import MYM
from ym.risk import RiskLimits
from ym.strategies import SupportRejection


PERMISSIVE = RiskLimits(
    risk_per_trade_pct=2.0, max_contracts=5, max_daily_loss_pct=None,
    max_consecutive_losses=None, max_drawdown_pct=None, min_stop_ticks=1,
)


def run(strategy, bars, instrument=MYM, equity=50_000, limits=None, **config):
    return Backtester(
        instrument, strategy, equity, limits or PERMISSIVE,
        BacktestConfig(
            slippage_ticks=config.pop("slippage_ticks", 0.0),
            no_new_trades_minutes_before_close=config.pop(
                "no_new_trades_minutes_before_close", 0
            ),
            flatten_minutes_before_close=config.pop(
                "flatten_minutes_before_close", None
            ),
            **config,
        ),
    ).run(bars)


def five_minute_bars(days=60, seed=11):
    return resample(
        filter_session(generate_bars(date(2026, 6, 1), days=days, seed=seed), "rth"), 5
    )


class TestTheSetup(unittest.TestCase):
    def setUp(self):
        # Extend the scripted path so the trade has room to resolve.
        self.bars = build(SCRIPTED)

    def test_it_buys_the_third_rejection(self):
        strategy = SupportRejection(
            min_rejections=3, tolerance_atr=0.6, stop_buffer_atr=0.3, target_r=2.0
        )
        result = run(strategy, self.bars)
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertIs(trade.direction, Direction.LONG)
        self.assertIn("SupportRejection", trade.setup)

    def test_the_stop_sits_below_the_level_not_on_it(self):
        strategy = SupportRejection(min_rejections=3, tolerance_atr=0.6,
                                    stop_buffer_atr=0.3)
        trade = run(strategy, self.bars).trades[0]
        level = min(strategy.tracker.active("support") + strategy.tracker.broken_levels,
                    key=lambda lvl: abs(lvl.price - 40_900))
        self.assertLess(trade.stop_price, level.floor)
        self.assertLess(trade.stop_price, level.price)

    def test_risk_is_small_relative_to_the_level_distance(self):
        strategy = SupportRejection(min_rejections=3, tolerance_atr=0.6,
                                    stop_buffer_atr=0.3)
        trade = run(strategy, self.bars).trades[0]
        self.assertIsNotNone(trade.risk_points)
        self.assertLess(trade.risk_points, 100)

    def test_two_rejections_fire_earlier_than_three(self):
        early = run(SupportRejection(min_rejections=2, tolerance_atr=0.6), self.bars)
        late = run(SupportRejection(min_rejections=3, tolerance_atr=0.6), self.bars)
        self.assertTrue(early.trades and late.trades)
        self.assertLess(early.trades[0].entry_time, late.trades[0].entry_time)

    def test_demanding_more_rejections_than_happened_takes_no_trade(self):
        result = run(SupportRejection(min_rejections=6, tolerance_atr=0.6), self.bars)
        self.assertEqual(result.trades, [])

    def test_entry_is_never_on_the_signal_bar(self):
        strategy = SupportRejection(min_rejections=3, tolerance_atr=0.6)
        result = run(strategy, self.bars)
        trade = result.trades[0]
        rejection_bar = next(
            bar for bar in self.bars if bar.ts == trade.entry_time
        )
        self.assertGreater(trade.entry_time, self.bars[0].ts)
        # The third rejection bar is the one with the 40,898 low.
        third = next(bar for bar in self.bars if bar.low == 40_898)
        self.assertGreater(trade.entry_time, third.ts)

    def test_one_trade_per_level(self):
        strategy = SupportRejection(min_rejections=2, tolerance_atr=0.6,
                                    one_trade_per_level=True)
        result = run(strategy, self.bars)
        self.assertEqual(len(result.trades), 1)

    def test_allowing_repeats_can_trade_the_level_twice(self):
        repeatable = run(
            SupportRejection(min_rejections=2, tolerance_atr=0.6,
                             one_trade_per_level=False),
            self.bars,
        )
        once = run(
            SupportRejection(min_rejections=2, tolerance_atr=0.6,
                             one_trade_per_level=True),
            self.bars,
        )
        self.assertGreaterEqual(len(repeatable.trades), len(once.trades))


class TestConfiguration(unittest.TestCase):
    def test_first_rejection_entry_is_refused_without_an_explicit_opt_in(self):
        with self.assertRaises(ValueError) as caught:
            SupportRejection(min_rejections=1)
        self.assertIn("max_bars_since_touch", str(caught.exception))

    def test_first_rejection_entry_works_when_staleness_is_allowed(self):
        strategy = SupportRejection(min_rejections=1, max_bars_since_touch=3,
                                    pivot_strength=3)
        self.assertEqual(strategy.min_rejections, 1)

    def test_rejection_counts_one_and_two_are_not_the_same_strategy(self):
        bars = five_minute_bars(days=60)
        one = run(SupportRejection(min_rejections=1, max_bars_since_touch=3), bars)
        two = run(SupportRejection(min_rejections=2), bars)
        self.assertNotEqual(
            [t.entry_time for t in one.trades], [t.entry_time for t in two.trades],
            "min_rejections=1 must not silently alias to 2",
        )

    def test_bad_settings_are_rejected(self):
        for kwargs in (
            {"direction": "sideways"},
            {"target_mode": "hope"},
            {"entry_mode": "vibes"},
            {"min_rejections": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    SupportRejection(**kwargs)

    def test_max_rejections_skips_over_tested_levels(self):
        bars = five_minute_bars(days=60)
        capped = run(SupportRejection(min_rejections=2, max_rejections=2), bars)
        uncapped = run(SupportRejection(min_rejections=2), bars)
        self.assertLessEqual(len(capped.trades), len(uncapped.trades))
        for trade in capped.trades:
            self.assertIn("rejections=2", trade.tags)

    def test_short_direction_sells_resistance(self):
        bars = build(WARMUP + [
            ("leg", 41_040, 6),
            ("reject", 41_100, 41_065),
            ("leg", 41_015, 6),
            ("reject", 41_097, 41_060),
            ("leg", 41_025, 6),
            ("reject", 41_102, 41_055),
            ("leg", 40_900, 20),
        ])
        strategy = SupportRejection(min_rejections=3, direction="short",
                                    tolerance_atr=0.6)
        result = run(strategy, bars)
        self.assertTrue(result.trades)
        trade = result.trades[0]
        self.assertIs(trade.direction, Direction.SHORT)
        self.assertGreater(trade.stop_price, trade.entry_price)

    def test_stop_entry_pays_more_than_a_market_entry(self):
        bars = build(SCRIPTED)
        market = run(SupportRejection(min_rejections=3, tolerance_atr=0.6,
                                      entry_mode="market"), bars)
        stopped = run(SupportRejection(min_rejections=3, tolerance_atr=0.6,
                                       entry_mode="stop_above_bar",
                                       valid_bars=10), bars)
        self.assertTrue(market.trades, "market entry should have filled")
        self.assertTrue(stopped.trades, "stop entry should have filled in 10 bars")
        self.assertGreater(
            stopped.trades[0].entry_price, market.trades[0].entry_price,
            "waiting for confirmation above the bar costs you the difference",
        )

    def test_a_stop_entry_can_expire_unfilled(self):
        # The trigger sits above the whole rejection bar, which on a wide bar can
        # be a long way up. Confirmation is not free: sometimes you miss.
        bars = build(SCRIPTED)
        brief = run(SupportRejection(min_rejections=3, tolerance_atr=0.6,
                                     entry_mode="stop_above_bar", valid_bars=2), bars)
        patient = run(SupportRejection(min_rejections=3, tolerance_atr=0.6,
                                       entry_mode="stop_above_bar",
                                       valid_bars=10), bars)
        self.assertEqual(brief.trades, [])
        self.assertTrue(patient.trades)

    def test_target_at_the_opposing_level(self):
        bars = five_minute_bars(days=60)
        result = run(
            SupportRejection(min_rejections=2, target_mode="opposing_level",
                             min_target_r=1.0),
            bars,
        )
        for trade in result.trades:
            self.assertIsNotNone(trade.target_price)
            self.assertGreaterEqual(trade.planned_r_multiple, 1.0)

    def test_breakeven_rule_keeps_the_original_risk(self):
        bars = build(SCRIPTED)
        result = run(
            SupportRejection(min_rejections=3, tolerance_atr=0.6, breakeven_at_r=1.0),
            bars,
        )
        trade = result.trades[0]
        self.assertLess(trade.stop_price, trade.entry_price)

    def test_daily_trade_cap(self):
        bars = five_minute_bars(days=30)
        from collections import Counter
        from ym.sessions import DEFAULT_SESSION
        result = run(SupportRejection(min_rejections=2, max_trades_per_day=1), bars)
        per_day = Counter(
            DEFAULT_SESSION.session_day(trade.entry_time) for trade in result.trades
        )
        self.assertTrue(per_day)
        self.assertLessEqual(max(per_day.values()), 1)

    def test_reset_daily_discards_levels_at_the_session_boundary(self):
        bars = five_minute_bars(days=30)
        strategy = SupportRejection(min_rejections=3, reset_daily=True)
        run(strategy, bars)
        # Whatever it learned, a new session starts it over.
        from ym.backtest.strategy import Context
        from ym.sessions import DEFAULT_SESSION
        context = Context(MYM, DEFAULT_SESSION, bars, len(bars) - 1, 25_000, None,
                          DEFAULT_SESSION.session_day(bars[-1].ts), bars)
        strategy.on_session_start(context, DEFAULT_SESSION.session_day(bars[-1].ts))
        self.assertEqual(strategy.tracker.active(), [])
        self.assertEqual(strategy.trades_today, 0)

    def test_carrying_levels_over_changes_the_result(self):
        bars = five_minute_bars(days=30)
        carried = run(SupportRejection(min_rejections=3, reset_daily=False), bars)
        reset = run(SupportRejection(min_rejections=3, reset_daily=True), bars)
        self.assertNotEqual(
            [t.entry_time for t in carried.trades],
            [t.entry_time for t in reset.trades],
        )


class TestIntegration(unittest.TestCase):
    def test_it_runs_on_a_long_series_and_produces_analysable_trades(self):
        bars = five_minute_bars(days=60)
        result = run(
            SupportRejection(min_rejections=3), bars,
            limits=RiskLimits(risk_per_trade_pct=0.5, max_daily_loss_pct=2.0),
            slippage_ticks=1.0, flatten_minutes_before_close=5,
        )
        self.assertGreater(result.metrics.trades, 10)
        for trade in result.trades:
            self.assertIsNotNone(trade.stop_price)
            self.assertIsNotNone(trade.r_multiple)
            self.assertIsNotNone(trade.exit_reason)

    def test_results_are_deterministic(self):
        bars = five_minute_bars(days=40)
        runs = [
            run(SupportRejection(min_rejections=3), bars, slippage_ticks=1.0)
            for _ in range(2)
        ]
        self.assertEqual(
            [t.net_pnl for t in runs[0].trades], [t.net_pnl for t in runs[1].trades]
        )

    def test_risk_rules_still_bind(self):
        bars = five_minute_bars(days=60)
        result = run(
            SupportRejection(min_rejections=2), bars,
            instrument=__import__("ym").YM, equity=10_000,
            limits=RiskLimits(risk_per_trade_pct=0.1),
        )
        self.assertTrue(result.blocked, "a tiny budget should refuse these stops")


if __name__ == "__main__":
    unittest.main()
