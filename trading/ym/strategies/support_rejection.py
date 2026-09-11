"""Buy the Nth rejection of a support line (or sell the Nth of resistance).

The idea: a level that has held several times is defended, so the next test of
it is a buy with a stop just beneath -- tight risk, because if the level gives
way the thesis is dead immediately.

The counter-argument is worth knowing before you read any backtest of it: each
test of a level also *consumes* the resting bids defending it. A level that has
bounced four times has had four rounds of buyers absorbed, and the fifth test
has less underneath it than the first did. Whether rejections strengthen or
weaken a level is exactly the sort of thing a backtest can answer and intuition
cannot, which is why ``min_rejections`` is a parameter rather than a constant.
Sweep it (``python -m ym sweep``) instead of assuming three is the magic number.

``min_rejections`` counts **all** confirmed rejections of the line, including
the swing low that drew it. So ``min_rejections=3`` means: a low, then two more
tests that held, and you buy the third.
"""

from __future__ import annotations

from datetime import date

from ..backtest.strategy import Context, EntryType, Manage, Signal, Strategy
from ..core import Direction, Trade
from ..levels import Level, LevelTracker


class SupportRejection(Strategy):
    """Enter on the Nth rejection of a horizontal level.

    Parameters worth thinking about before the rest:

    ``min_rejections``
        How many times the level must have held, the forming low included.
    ``max_rejections``
        Skip levels tested more than this. Set it to find out whether old
        levels are worse than fresh ones -- the absorption argument above.
    ``tolerance_atr``
        Half-width of the band that counts as "at the level", in ATR. Too tight
        and you miss tests; too loose and unrelated lows cluster together.
        Because it scales with ATR, no level can form until ATR has warmed up --
        the first ``atr_period`` bars of a series are blind. Pass
        ``tolerance_points`` instead for a fixed band with no warm-up.
    ``stop_buffer_atr``
        How far below the level's floor the stop sits. The floor is the lowest
        price the level has actually seen, so a stop at the line itself would
        already have been hit.
    ``max_bars_since_touch``
        How stale a rejection may be when you act on it. Zero means the
        rejection bar is the current bar.
    """

    def __init__(
        self,
        min_rejections: int = 3,
        max_rejections: int | None = None,
        direction: str = "long",
        pivot_strength: int = 3,
        tolerance_atr: float = 0.35,
        tolerance_points: float | None = None,
        min_bars_between_touches: int = 3,
        break_buffer_atr: float = 0.25,
        max_level_age_bars: int | None = 240,
        stop_buffer_atr: float = 0.35,
        target_r: float = 2.0,
        target_mode: str = "r",              # "r" or "opposing_level"
        min_target_r: float = 1.0,
        entry_mode: str = "market",          # "market" or "stop_above_bar"
        entry_buffer_ticks: float = 1.0,
        max_bars_since_touch: int = 0,
        valid_bars: int = 3,
        max_trades_per_day: int | None = None,
        one_trade_per_level: bool = True,
        breakeven_at_r: float | None = None,
        reset_daily: bool = False,
        atr_period: int = 14,
    ) -> None:
        if direction not in ("long", "short"):
            raise ValueError("direction must be 'long' or 'short'")
        if target_mode not in ("r", "opposing_level"):
            raise ValueError("target_mode must be 'r' or 'opposing_level'")
        if entry_mode not in ("market", "stop_above_bar"):
            raise ValueError("entry_mode must be 'market' or 'stop_above_bar'")
        if min_rejections < 1:
            raise ValueError("min_rejections must be at least 1")
        # The low that draws the line is only confirmed `pivot_strength` bars
        # after it happens, so a first-rejection entry is always that stale. Say
        # so rather than quietly behaving like min_rejections=2, which would make
        # a parameter sweep compare 1 against 2 and find them identical.
        if min_rejections == 1 and max_bars_since_touch < pivot_strength:
            raise ValueError(
                f"min_rejections=1 means buying the swing low that forms the "
                f"level, but that low is only confirmed {pivot_strength} bars "
                f"later. Pass max_bars_since_touch={pivot_strength} to accept an "
                f"entry that late, or use min_rejections=2 for the first retest."
            )

        self.direction = Direction.LONG if direction == "long" else Direction.SHORT
        self.level_kind = "support" if direction == "long" else "resistance"
        self.opposing_kind = "resistance" if direction == "long" else "support"
        self.name = f"SupportRejection{min_rejections}" if direction == "long" \
            else f"ResistanceRejection{min_rejections}"

        self.min_rejections = min_rejections
        self.max_rejections = max_rejections
        self.stop_buffer_atr = stop_buffer_atr
        self.target_r = target_r
        self.target_mode = target_mode
        self.min_target_r = min_target_r
        self.entry_mode = entry_mode
        self.entry_buffer_ticks = entry_buffer_ticks
        self.max_bars_since_touch = max_bars_since_touch
        self.valid_bars = valid_bars
        self.max_trades_per_day = max_trades_per_day
        self.one_trade_per_level = one_trade_per_level
        self.breakeven_at_r = breakeven_at_r
        self.reset_daily = reset_daily
        self.atr_period = atr_period

        self.tracker = LevelTracker(
            pivot_strength=pivot_strength,
            tolerance_atr=tolerance_atr,
            tolerance_points=tolerance_points,
            min_bars_between_touches=min_bars_between_touches,
            break_buffer_atr=break_buffer_atr,
            max_level_age_bars=max_level_age_bars,
            track="both" if target_mode == "opposing_level" else self.level_kind,
        )
        self._seen_rejections: dict[int, int] = {}
        self._traded_levels: set[int] = set()
        self.trades_today = 0

    # -- bookkeeping -------------------------------------------------------

    def on_session_start(self, context: Context, day: date) -> None:
        self.trades_today = 0
        if self.reset_daily:
            self.tracker.reset()
            self._seen_rejections.clear()
            self._traded_levels.clear()

    def on_trade_closed(self, context: Context, trade: Trade) -> None:
        self.trades_today += 1

    def _sync(self, context: Context) -> float | None:
        """Keep the tracker level with the bar stream and return the current ATR.

        Called from both ``on_bar`` and ``manage`` because the engine calls only
        one of them per bar.
        """
        atr = context.atr(self.atr_period)
        self.tracker.observe(context.bars, context.index, atr)
        return atr

    # -- the signal --------------------------------------------------------

    def _newly_qualified(self, context: Context) -> Level | None:
        """A level whose rejection count reached the threshold on this bar."""
        qualified = None
        for level in self.tracker.active(self.level_kind):
            previous = self._seen_rejections.get(level.uid, 0)
            current = level.rejections
            self._seen_rejections[level.uid] = current
            if current <= previous or current < self.min_rejections:
                continue
            if self.max_rejections is not None and current > self.max_rejections:
                continue
            if self.one_trade_per_level and level.uid in self._traded_levels:
                continue
            last = level.last_touch
            if last is None:
                continue
            # The crossing must be news. A backfilled rejection (the forming
            # low) is `pivot_strength` bars old by the time the level exists,
            # which is what max_bars_since_touch allows for.
            if context.index - last.bar_index > self.max_bars_since_touch:
                continue
            # Prefer the best-tested level if several qualify at once.
            if qualified is None or current > qualified.rejections:
                qualified = level
        return qualified

    def _target_price(
        self, context: Context, entry: float, stop: float
    ) -> float | None:
        risk = abs(entry - stop)
        if not risk:
            return None
        if self.target_mode == "r":
            return entry + self.direction.sign * risk * self.target_r

        opposing = (
            self.tracker.nearest_above(entry, self.opposing_kind)
            if self.direction is Direction.LONG
            else self.tracker.nearest_below(entry, self.opposing_kind)
        )
        if opposing is None:
            return entry + self.direction.sign * risk * self.target_r
        reward = abs(opposing.price - entry)
        if reward / risk < self.min_target_r:
            return None  # nothing worth reaching for; stand aside
        return opposing.price

    def on_bar(self, context: Context) -> Signal | None:
        atr = self._sync(context)
        if not atr:
            return None
        if (
            self.max_trades_per_day is not None
            and self.trades_today >= self.max_trades_per_day
        ):
            return None

        level = self._newly_qualified(context)
        if level is None:
            return None

        bar = context.bar
        tick = context.instrument.tick_size
        buffer_points = self.stop_buffer_atr * atr

        if self.direction is Direction.LONG:
            stop = context.instrument.round_to_tick(level.floor - buffer_points, "down")
            reference = bar.close
            trigger = context.instrument.round_to_tick(
                bar.high + self.entry_buffer_ticks * tick, "up"
            )
        else:
            stop = context.instrument.round_to_tick(level.floor + buffer_points, "up")
            reference = bar.close
            trigger = context.instrument.round_to_tick(
                bar.low - self.entry_buffer_ticks * tick, "down"
            )

        entry = reference if self.entry_mode == "market" else trigger
        # A close already through the level is not a rejection to buy into.
        if self.direction is Direction.LONG and entry <= stop:
            return None
        if self.direction is Direction.SHORT and entry >= stop:
            return None

        target = self._target_price(context, entry, stop)
        if target is None:
            return None

        self._traded_levels.add(level.uid)
        return Signal(
            direction=self.direction,
            entry_type=(
                EntryType.MARKET if self.entry_mode == "market" else EntryType.STOP
            ),
            entry_price=None if self.entry_mode == "market" else trigger,
            stop_price=stop,
            target_price=target,
            valid_bars=self.valid_bars,
            setup=f"{self.name}",
            notes=(
                f"{level.kind} {level.price:,.1f} held {level.rejections}x "
                f"(floor {level.floor:,.1f}, level age "
                f"{context.index - level.created_index} bars)"
            ),
            tags=[f"rejections={level.rejections}"],
        )

    def manage(self, context: Context, trade: Trade) -> Manage | None:
        self._sync(context)
        if self.breakeven_at_r is None or not trade.risk_points:
            return None
        favorable = (context.bar.close - trade.entry_price) * trade.direction.sign
        if favorable >= trade.risk_points * self.breakeven_at_r:
            return Manage(new_stop=trade.entry_price, reason="stop to breakeven")
        return None
