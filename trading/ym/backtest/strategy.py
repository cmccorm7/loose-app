"""The strategy interface.

A strategy is a small object that answers two questions, one bar at a time:

* ``on_bar`` -- given everything up to and including this bar's close, do I
  want a position? Return a :class:`Signal` or ``None``.
* ``manage`` -- I already have a position; move the stop, move the target, or
  get out?

A strategy never chooses its own size and never sees future bars. Size comes
from the :class:`~ym.risk.RiskManager`; the engine only ever hands you history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Sequence

from ..core import Bar, Direction, Trade
from ..instruments import Instrument
from ..sessions import SessionSpec


class EntryType(str, Enum):
    MARKET = "market"  # fill at the next bar's open
    STOP = "stop"      # breakout: fill when price trades through the trigger
    LIMIT = "limit"    # pullback: fill when price trades back to the limit


@dataclass
class Signal:
    """A request to enter a position.

    Give a ``stop_price`` or ``stop_points`` -- without a stop there is no risk
    to size against and the engine will reject the signal. ``target_r`` sets a
    target as a multiple of that risk, which is usually easier to reason about
    than a price.
    """

    direction: Direction
    entry_type: EntryType = EntryType.MARKET
    entry_price: float | None = None   # required for STOP and LIMIT entries
    stop_price: float | None = None
    stop_points: float | None = None
    target_price: float | None = None
    target_r: float | None = None
    max_contracts: int | None = None   # strategy-side size cap, on top of risk rules
    valid_bars: int = 1                # how long a resting order stays live
    setup: str = ""
    notes: str = ""
    tags: list[str] = field(default_factory=list)

    def resolve(self, reference_price: float) -> tuple[float, float, float | None]:
        """Turn the signal into concrete ``(entry, stop, target)`` prices."""
        entry = self.entry_price if self.entry_price is not None else reference_price
        if self.stop_price is not None:
            stop = self.stop_price
        elif self.stop_points is not None:
            stop = entry - self.direction.sign * abs(self.stop_points)
        else:
            raise ValueError("signal needs stop_price or stop_points")

        target = self.target_price
        if target is None and self.target_r is not None:
            target = entry + self.direction.sign * abs(entry - stop) * self.target_r
        return entry, stop, target


@dataclass
class Manage:
    """An adjustment to an open position, returned from :meth:`Strategy.manage`."""

    new_stop: float | None = None
    new_target: float | None = None
    exit_now: bool = False
    reason: str = ""


@dataclass
class Context:
    """Everything a strategy is allowed to see."""

    instrument: Instrument
    session: SessionSpec
    bars: Sequence[Bar]          # history through the current bar, never beyond
    index: int                   # position of the current bar in ``bars``
    equity: float
    position: Trade | None
    session_day: date
    day_bars: Sequence[Bar]      # bars so far in the current session day

    @property
    def bar(self) -> Bar:
        return self.bars[self.index]

    @property
    def now(self) -> datetime:
        return self.bar.ts

    def lookback(self, count: int) -> Sequence[Bar]:
        """The last ``count`` bars, ending with the current one."""
        start = max(0, self.index + 1 - count)
        return self.bars[start : self.index + 1]

    def has(self, count: int) -> bool:
        return self.index + 1 >= count

    def minutes_since_open(self) -> float:
        return self.session.minutes_since_rth_open(self.bar.ts)

    def highest(self, count: int) -> float:
        return max(bar.high for bar in self.lookback(count))

    def lowest(self, count: int) -> float:
        return min(bar.low for bar in self.lookback(count))

    def sma(self, count: int) -> float | None:
        window = self.lookback(count)
        if len(window) < count:
            return None
        return sum(bar.close for bar in window) / count

    def atr(self, count: int = 14) -> float | None:
        """Average true range over ``count`` bars, in index points."""
        window = self.lookback(count + 1)
        if len(window) < count + 1:
            return None
        true_ranges = []
        for previous, current in zip(window, window[1:]):
            true_ranges.append(
                max(
                    current.high - current.low,
                    abs(current.high - previous.close),
                    abs(current.low - previous.close),
                )
            )
        return sum(true_ranges) / len(true_ranges)


class Strategy:
    """Subclass this. Only ``on_bar`` is required."""

    name: str = "strategy"

    def on_start(self, context: Context) -> None:
        """Called once before the first bar."""

    def on_session_start(self, context: Context, day: date) -> None:
        """Called on the first bar of each new trading day."""

    def on_bar(self, context: Context) -> Signal | None:
        """Return a signal to enter, or ``None`` to stand aside."""
        raise NotImplementedError

    def manage(self, context: Context, trade: Trade) -> Manage | None:
        """Adjust or close an open position. Return ``None`` to leave it alone."""
        return None

    def on_trade_closed(self, context: Context, trade: Trade) -> None:
        """Called after each exit, for strategies that adapt."""
