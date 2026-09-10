"""Opening range breakout -- the canonical index-futures day trade.

Measure the high and low of the first N minutes of the cash session, then take
a stop entry when price trades outside that range. Included as a worked example
of the strategy interface, not as a recommendation: run it on your own data and
judge it yourself.
"""

from __future__ import annotations

from datetime import date, time, timedelta

from ..core import Direction, Trade
from ..backtest.strategy import Context, EntryType, Manage, Signal, Strategy


class OpeningRangeBreakout(Strategy):
    """Break of the opening range, with an R-multiple target.

    ``stop_mode`` decides where risk is placed:

    * ``"opposite"`` -- stop at the far side of the range (wide, but structural)
    * ``"fraction"`` -- stop at ``stop_fraction`` of the range width
    * ``"atr"``      -- stop at ``atr_multiple`` x ATR

    Wide stops are not free: the risk manager converts them into a smaller
    position, and will refuse the trade outright if one contract is too much.
    """

    def __init__(
        self,
        range_minutes: int = 30,
        buffer_ticks: float = 2.0,
        stop_mode: str = "fraction",
        stop_fraction: float = 0.5,
        atr_multiple: float = 1.5,
        target_r: float = 2.0,
        max_trades_per_day: int = 1,
        max_attempts_per_day: int = 2,
        cutoff: time = time(12, 0),
        breakeven_at_r: float | None = 1.0,
        allow_shorts: bool = True,
    ) -> None:
        if stop_mode not in ("opposite", "fraction", "atr"):
            raise ValueError("stop_mode must be 'opposite', 'fraction' or 'atr'")
        self.name = f"ORB{range_minutes}"
        self.range_minutes = range_minutes
        self.buffer_ticks = buffer_ticks
        self.stop_mode = stop_mode
        self.stop_fraction = stop_fraction
        self.atr_multiple = atr_multiple
        self.target_r = target_r
        self.max_trades_per_day = max_trades_per_day
        self.max_attempts_per_day = max_attempts_per_day
        self.cutoff = cutoff
        self.breakeven_at_r = breakeven_at_r
        self.allow_shorts = allow_shorts

        self.range_high: float | None = None
        self.range_low: float | None = None
        self.filled_today = 0
        self.attempts_today = 0

    def on_session_start(self, context: Context, day: date) -> None:
        self.range_high = None
        self.range_low = None
        self.filled_today = 0
        self.attempts_today = 0

    def on_trade_closed(self, context: Context, trade: Trade) -> None:
        self.filled_today += 1

    def _build_range(self, context: Context) -> None:
        """Freeze the opening range once the measurement window has elapsed."""
        session = context.session
        day = context.session_day
        window_end = session.rth_open_at(day) + timedelta(minutes=self.range_minutes)
        window = [
            bar
            for bar in context.day_bars
            if session.rth_open_at(day) <= bar.ts < window_end
        ]
        if not window:
            return
        self.range_high = max(bar.high for bar in window)
        self.range_low = min(bar.low for bar in window)

    def _stop_distance(self, context: Context, range_size: float) -> float:
        if self.stop_mode == "opposite":
            return range_size
        if self.stop_mode == "fraction":
            return max(range_size * self.stop_fraction, context.instrument.tick_size * 4)
        atr = context.atr(14) or range_size * 0.5
        return atr * self.atr_multiple

    def on_bar(self, context: Context) -> Signal | None:
        session = context.session
        bar = context.bar
        local = session.localize(bar.ts)

        elapsed = context.minutes_since_open()
        if elapsed < self.range_minutes:
            return None
        if self.range_high is None:
            self._build_range(context)
            if self.range_high is None:
                return None
        if self.filled_today >= self.max_trades_per_day:
            return None
        if self.attempts_today >= self.max_attempts_per_day:
            return None
        if local.time() >= self.cutoff:
            return None

        buffer_points = self.buffer_ticks * context.instrument.tick_size
        range_size = self.range_high - self.range_low
        if range_size <= 0:
            return None
        stop_distance = self._stop_distance(context, range_size)

        # Only arm the side price is closest to, so one bar yields one order.
        long_trigger = self.range_high + buffer_points
        short_trigger = self.range_low - buffer_points
        if bar.close >= self.range_low + range_size / 2:
            direction, trigger = Direction.LONG, long_trigger
        elif self.allow_shorts:
            direction, trigger = Direction.SHORT, short_trigger
        else:
            return None

        self.attempts_today += 1  # a resting order that never fills still counts
        return Signal(
            direction=direction,
            entry_type=EntryType.STOP,
            entry_price=trigger,
            stop_points=stop_distance,
            target_r=self.target_r,
            valid_bars=30,
            setup=f"{self.name}-{direction.value}",
            notes=f"range {self.range_low:g}-{self.range_high:g} ({range_size:g} pts)",
        )

    def manage(self, context: Context, trade: Trade) -> Manage | None:
        """Pull the stop to breakeven once the trade is up ``breakeven_at_r``."""
        if self.breakeven_at_r is None or trade.risk_points is None:
            return None
        favorable = (context.bar.close - trade.entry_price) * trade.direction.sign
        if favorable >= trade.risk_points * self.breakeven_at_r:
            return Manage(new_stop=trade.entry_price, reason="stop to breakeven")
        return None
