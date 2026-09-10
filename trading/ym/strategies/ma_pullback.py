"""Trend pullback: buy dips in an uptrend, sell rallies in a downtrend.

A second worked example with a different shape from the breakout -- limit
entries rather than stop entries, and an ATR-based stop. Again: an example of
how to express an idea, not a recommendation.
"""

from __future__ import annotations

from ..core import Direction, Trade
from ..backtest.strategy import Context, EntryType, Manage, Signal, Strategy


class MovingAveragePullback(Strategy):
    def __init__(
        self,
        fast: int = 20,
        slow: int = 50,
        pullback_atr: float = 0.5,
        stop_atr: float = 1.25,
        target_r: float = 1.75,
        trail_atr: float | None = None,
    ) -> None:
        self.name = f"MAPullback{fast}/{slow}"
        self.fast = fast
        self.slow = slow
        self.pullback_atr = pullback_atr
        self.stop_atr = stop_atr
        self.target_r = target_r
        self.trail_atr = trail_atr

    def on_bar(self, context: Context) -> Signal | None:
        fast = context.sma(self.fast)
        slow = context.sma(self.slow)
        atr = context.atr(14)
        if fast is None or slow is None or not atr:
            return None

        close = context.bar.close
        if fast > slow and close > slow:
            direction = Direction.LONG
            limit = close - self.pullback_atr * atr
        elif fast < slow and close < slow:
            direction = Direction.SHORT
            limit = close + self.pullback_atr * atr
        else:
            return None

        return Signal(
            direction=direction,
            entry_type=EntryType.LIMIT,
            entry_price=context.instrument.round_to_tick(limit),
            stop_points=self.stop_atr * atr,
            target_r=self.target_r,
            valid_bars=5,
            setup=f"{self.name}-{direction.value}",
        )

    def manage(self, context: Context, trade: Trade) -> Manage | None:
        if not self.trail_atr:
            return None
        atr = context.atr(14)
        if not atr:
            return None
        if trade.direction is Direction.LONG:
            return Manage(new_stop=context.bar.close - self.trail_atr * atr)
        return Manage(new_stop=context.bar.close + self.trail_atr * atr)
