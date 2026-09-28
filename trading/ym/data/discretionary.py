"""A simulated discretionary trader, for exercising the context analysis.

The behavioural simulator in :mod:`ym.data.trader` invents trades out of thin
air, which is fine for the journal-only detectors. The context detectors need
trades that line up with real bars -- a trade "at a held level" has to actually
be at one -- so this one takes entries off a bar series and lets the backtest
engine fill them.

The habits are knobs so a detector can be checked against the behaviour it
claims to find, and checked for silence when that behaviour is absent.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from ..backtest.strategy import Context, EntryType, Signal, Strategy
from ..core import Direction, Trade
from ..levels import LevelTracker


@dataclass
class DiscretionaryHabits:
    """How the simulated trader behaves. Zero the probabilities for a clean one."""

    stop_points: float = 20.0          # the fixed stop he actually uses
    target_points: float = 40.0        # the profit he actually takes
    take_early: float = 0.75           # chance of grabbing that fixed target
    runner_target_points: float = 160.0  # what "riding it" looks like instead

    at_level: float = 0.5              # chance of waiting for a held level
    chase: float = 0.3                 # chance of buying a move already running
    fight_level: float = 0.2           # chance of shorting above a holding floor

    trades_per_day: int = 3
    min_bars_between: int = 6


class DiscretionaryTrader(Strategy):
    """Takes entries the way the handoff describes them, good habits and bad."""

    name = "discretionary"

    def __init__(self, habits: DiscretionaryHabits | None = None, seed: int = 1):
        self.habits = habits or DiscretionaryHabits()
        self.rng = random.Random(seed)
        self.tracker = LevelTracker(
            pivot_strength=2, tolerance_atr=0.35, min_bars_between_touches=2,
            max_level_age_bars=240,
        )
        self.taken_today = 0
        self.last_entry_index = -999

    def on_session_start(self, context: Context, day) -> None:
        self.taken_today = 0

    def on_trade_closed(self, context: Context, trade: Trade) -> None:
        self.taken_today += 1

    def _sync(self, context: Context) -> float | None:
        atr = context.atr(14)
        self.tracker.observe(context.bars, context.index, atr)
        return atr

    def manage(self, context: Context, trade: Trade):
        self._sync(context)
        return None

    def _signal(self, direction: Direction, entry: float, target_points: float,
                setup: str) -> Signal:
        stop = self.habits.stop_points
        return Signal(
            direction=direction,
            entry_type=EntryType.MARKET,
            stop_points=stop,
            target_price=entry + direction.sign * target_points,
            valid_bars=1,
            setup=setup,
        )

    def on_bar(self, context: Context) -> Signal | None:
        atr = self._sync(context)
        habits = self.habits
        if not atr or self.taken_today >= habits.trades_per_day:
            return None
        if context.index - self.last_entry_index < habits.min_bars_between:
            return None

        price = context.bar.close
        support = self.tracker.nearest_below(price, "support")
        held = support is not None and support.rejections >= 2
        target = (
            habits.target_points if self.rng.random() < habits.take_early
            else habits.runner_target_points
        )

        # Choose what he is looking for *first*, then see whether it is there.
        # Cumulative bands with fall-through would let a trader configured never
        # to short take shorts whenever his real setup was absent.
        total = habits.at_level + habits.chase + habits.fight_level
        if total <= 0:
            return None
        roll = self.rng.random() * max(1.0, total)
        if roll < habits.at_level:
            intent = "at-level"
        elif roll < habits.at_level + habits.chase:
            intent = "chase"
        elif roll < total:
            intent = "fade-above-floor"
        else:
            return None

        if intent == "at-level":
            # The setup he says he wants: risk sits beyond a level that held.
            if held and price - support.price <= habits.stop_points:
                self.last_entry_index = context.index
                return self._signal(Direction.LONG, price, target, intent)
            return None

        if intent == "chase":
            # Buying a move that has already travelled far from its swing.
            if price - context.lowest(12) >= 1.6 * atr:
                self.last_entry_index = context.index
                return self._signal(Direction.LONG, price, target, intent)
            return None

        # The bias creep: short while a floor below keeps refusing to break.
        if held and price > support.price:
            self.last_entry_index = context.index
            return self._signal(Direction.SHORT, price, target, intent)
        return None
