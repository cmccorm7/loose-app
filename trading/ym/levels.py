"""Finding horizontal support and resistance, and counting how often they hold.

A support line, in the form a program can act on, is a cluster of swing lows at
roughly the same price. This module maintains those clusters bar by bar and
counts **rejections** -- the times price traded into the level and closed back
above it.

The hard constraint is no lookahead. A swing low at bar *j* is only a swing low
once you have seen ``pivot_strength`` bars after it, so a level is created at
bar ``j + pivot_strength``, never at bar *j*. When a level is created, the bars
between the pivot and now are re-scanned for rejections -- those bars are all in
the past, so that is honest, and it means the low that *formed* the line counts
as the first rejection, the way it would if you drew the line by hand.

Nothing here knows about strategies or risk; it turns bars into levels.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from .core import Bar

_LEVEL_IDS = itertools.count(1)


@dataclass
class Touch:
    """One confirmed rejection of a level."""

    ts: datetime
    bar_index: int
    extreme: float       # how far into the level price reached
    close: float
    penetrated: bool     # did it trade through the level itself, not just the band?


@dataclass
class Level:
    """A horizontal level and its history.

    ``price`` is the line itself (the mean of its member pivots). ``floor`` is
    the furthest price has actually gone beyond it -- the honest place to put a
    stop, since a stop at ``price`` would already have been hit.
    """

    price: float
    kind: str                                  # "support" or "resistance"
    created_index: int
    created_ts: datetime
    pivots: list[float] = field(default_factory=list)
    touches: list[Touch] = field(default_factory=list)
    broken: bool = False
    broken_ts: datetime | None = None
    uid: int = field(default_factory=lambda: next(_LEVEL_IDS))
    _pending_index: int | None = None
    _pending_extreme: float | None = None
    _pending_penetrated: bool = False

    @property
    def is_support(self) -> bool:
        return self.kind == "support"

    @property
    def rejections(self) -> int:
        return len(self.touches)

    @property
    def floor(self) -> float:
        """The extreme of the level: lowest low for support, highest for resistance."""
        candidates = list(self.pivots) + [touch.extreme for touch in self.touches]
        if not candidates:
            return self.price
        return min(candidates) if self.is_support else max(candidates)

    @property
    def last_touch(self) -> Touch | None:
        return self.touches[-1] if self.touches else None

    def add_pivot(self, price: float) -> None:
        self.pivots.append(price)
        self.price = sum(self.pivots) / len(self.pivots)

    def distance(self, price: float) -> float:
        return abs(price - self.price)

    def describe(self) -> str:
        return (
            f"{self.kind} {self.price:,.1f} "
            f"({self.rejections} rejection(s), floor {self.floor:,.1f})"
        )


class LevelTracker:
    """Maintains support and resistance levels as bars arrive.

    Call :meth:`observe` once per bar, in order. Tolerance is expressed as a
    fraction of ATR by default so the band scales with volatility; pass
    ``tolerance_points`` to fix it instead.
    """

    def __init__(
        self,
        pivot_strength: int = 3,
        tolerance_atr: float = 0.35,
        tolerance_points: float | None = None,
        min_bars_between_touches: int = 3,
        confirm_within_bars: int = 3,
        break_buffer_atr: float = 0.25,
        max_levels: int = 12,
        max_level_age_bars: int | None = None,
        track: str = "both",                   # "support", "resistance" or "both"
    ) -> None:
        if pivot_strength < 1:
            raise ValueError("pivot_strength must be at least 1")
        if track not in ("support", "resistance", "both"):
            raise ValueError("track must be 'support', 'resistance' or 'both'")
        self.pivot_strength = pivot_strength
        self.tolerance_atr = tolerance_atr
        self.tolerance_points = tolerance_points
        self.min_bars_between_touches = min_bars_between_touches
        self.confirm_within_bars = confirm_within_bars
        self.break_buffer_atr = break_buffer_atr
        self.max_levels = max_levels
        self.max_level_age_bars = max_level_age_bars
        self.track = track

        self.levels: list[Level] = []
        self.broken_levels: list[Level] = []
        self._last_index: int = -1

    # -- geometry ----------------------------------------------------------

    def tolerance(self, atr: float | None) -> float:
        if self.tolerance_points is not None:
            return self.tolerance_points
        if not atr:
            return 0.0
        return self.tolerance_atr * atr

    def _break_buffer(self, atr: float | None, tolerance: float) -> float:
        if not atr:
            return tolerance
        return self.break_buffer_atr * atr

    # -- pivots ------------------------------------------------------------

    def _confirmed_pivot(
        self, bars: Sequence[Bar], index: int, kind: str
    ) -> tuple[int, float] | None:
        """The pivot this bar confirms, if any. Never looks past ``index``."""
        strength = self.pivot_strength
        candidate = index - strength
        if candidate - strength < 0:
            return None
        window = bars[candidate - strength : index + 1]
        if kind == "support":
            value = bars[candidate].low
            if value != min(bar.low for bar in window):
                return None
            # Strictly lower than its immediate neighbours, so a flat base does
            # not register as several pivots.
            if not (
                value < bars[candidate - 1].low and value < bars[candidate + 1].low
            ):
                return None
        else:
            value = bars[candidate].high
            if value != max(bar.high for bar in window):
                return None
            if not (
                value > bars[candidate - 1].high and value > bars[candidate + 1].high
            ):
                return None
        return candidate, value

    # -- touches -----------------------------------------------------------

    def _apply_bar(
        self, level: Level, bar: Bar, index: int, tolerance: float, break_buffer: float
    ) -> None:
        """Fold one bar into one level: break it, or record a rejection."""
        if level.broken:
            return
        support = level.is_support

        # A decisive close beyond the level ends it.
        if support and bar.close < level.price - break_buffer:
            level.broken, level.broken_ts = True, bar.ts
            return
        if not support and bar.close > level.price + break_buffer:
            level.broken, level.broken_ts = True, bar.ts
            return

        reached = (
            bar.low <= level.price + tolerance
            if support
            else bar.high >= level.price - tolerance
        )
        closed_back = bar.close > level.price if support else bar.close < level.price
        extreme = bar.low if support else bar.high
        penetrated = extreme < level.price if support else extreme > level.price

        last = level.last_touch
        too_soon = (
            last is not None and index - last.bar_index < self.min_bars_between_touches
        )

        if reached and closed_back and not too_soon:
            level.touches.append(Touch(bar.ts, index, extreme, bar.close, penetrated))
            level._pending_index = None
            return

        if reached and not closed_back:
            # Price is in the zone but has not turned yet. Hold it open; a close
            # back on the right side within a few bars still counts.
            if level._pending_index is None:
                level._pending_index = index
                level._pending_extreme = extreme
                level._pending_penetrated = penetrated
            else:
                if support:
                    level._pending_extreme = min(level._pending_extreme, extreme)
                else:
                    level._pending_extreme = max(level._pending_extreme, extreme)
                level._pending_penetrated = level._pending_penetrated or penetrated
            return

        if level._pending_index is not None:
            within = index - level._pending_index <= self.confirm_within_bars
            if closed_back and within and not too_soon:
                level.touches.append(
                    Touch(
                        bar.ts, index, level._pending_extreme, bar.close,
                        level._pending_penetrated,
                    )
                )
                level._pending_index = None
            elif not within:
                level._pending_index = None

    def _backfill(
        self,
        level: Level,
        bars: Sequence[Bar],
        start: int,
        end: int,
        tolerance: float,
        break_buffer: float,
    ) -> None:
        """Replay already-seen bars into a newly created level."""
        for index in range(start, end):
            self._apply_bar(level, bars[index], index, tolerance, break_buffer)

    # -- the per-bar entry point ------------------------------------------

    def observe(
        self, bars: Sequence[Bar], index: int, atr: float | None = None
    ) -> None:
        """Process bar ``index``.

        Bars must arrive in order, but they need not arrive one at a time: if a
        caller skips ahead, the missed bars are processed first. This matters
        because a strategy is not asked for a signal on every bar -- the engine
        stays quiet while a position is open or an order is resting -- and a
        level tracker that missed those bars would miscount its rejections. The
        catch-up bars reuse the current ATR for their tolerance, which is an
        approximation; call ``observe`` every bar if that matters to you.
        """
        if index <= self._last_index:
            raise ValueError(
                f"bars must be observed in order; saw {index} after {self._last_index}"
            )
        skipped = range(self._last_index + 1, index)
        self._last_index = index
        for missed in skipped:
            self._process(bars, missed, atr)
        self._process(bars, index, atr)

    def _process(
        self, bars: Sequence[Bar], index: int, atr: float | None
    ) -> None:
        tolerance = self.tolerance(atr)
        if tolerance <= 0:
            return  # no scale yet (ATR still warming up)
        break_buffer = self._break_buffer(atr, tolerance)

        kinds = (
            ("support", "resistance") if self.track == "both" else (self.track,)
        )
        for kind in kinds:
            found = self._confirmed_pivot(bars, index, kind)
            if found is None:
                continue
            pivot_index, pivot_price = found
            existing = self._nearest(kind, pivot_price, tolerance)
            if existing is not None:
                existing.add_pivot(pivot_price)
            else:
                level = Level(
                    price=pivot_price,
                    kind=kind,
                    created_index=pivot_index,
                    created_ts=bars[pivot_index].ts,
                    pivots=[pivot_price],
                )
                self.levels.append(level)
                # Count the rejection that drew the line, plus anything since.
                self._backfill(
                    level, bars, pivot_index, index, tolerance, break_buffer
                )

        for level in self.levels:
            self._apply_bar(level, bars[index], index, tolerance, break_buffer)

        self._prune(index)

    def _nearest(self, kind: str, price: float, tolerance: float) -> Level | None:
        candidates = [
            level
            for level in self.levels
            if level.kind == kind and not level.broken
            and level.distance(price) <= tolerance
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda level: level.distance(price))

    def _prune(self, index: int) -> None:
        kept: list[Level] = []
        for level in self.levels:
            if level.broken:
                self.broken_levels.append(level)
                continue
            if (
                self.max_level_age_bars is not None
                and index - level.created_index > self.max_level_age_bars
            ):
                continue
            kept.append(level)

        if len(kept) > self.max_levels:
            # Keep the best-tested levels; they are the ones worth watching.
            kept.sort(key=lambda level: (-level.rejections, -level.created_index))
            kept = kept[: self.max_levels]
        self.levels = kept

    # -- queries -----------------------------------------------------------

    def active(self, kind: str | None = None) -> list[Level]:
        return [
            level
            for level in self.levels
            if not level.broken and (kind is None or level.kind == kind)
        ]

    def nearest_below(self, price: float, kind: str = "support") -> Level | None:
        candidates = [
            level for level in self.active(kind) if level.price < price
        ]
        return max(candidates, key=lambda level: level.price) if candidates else None

    def nearest_above(self, price: float, kind: str = "resistance") -> Level | None:
        candidates = [
            level for level in self.active(kind) if level.price > price
        ]
        return min(candidates, key=lambda level: level.price) if candidates else None

    def touched_on(self, index: int, kind: str | None = None) -> list[Level]:
        """Levels whose newest rejection landed on bar ``index``.

        This is what a strategy keys on -- it fires once, on the bar the
        rejection confirms, rather than on every bar afterwards.
        """
        out = []
        for level in self.active(kind):
            last = level.last_touch
            if last is not None and last.bar_index == index:
                out.append(level)
        return out

    def reset(self) -> None:
        self.levels.clear()
        self.broken_levels.clear()
        self._last_index = -1
