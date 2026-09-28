"""Reading each trade against what price was actually doing.

A journal records what you did. Joined to the bars around it, the same trade
can answer the questions that matter about *how* you traded:

* **Trend** -- were you with the 5-minute structure, or against it?
* **Location** -- at a level that had held, somewhere in the middle, or chasing
  a move that had already run?
* **Exit** -- stopped out, taken early, or ridden?
* **Stop size** -- was your stop structural, or smaller than the bars?

That last one deserves saying plainly. A stop is only meaningful next to how
far price travels: twenty points under a floor that has held three times is a
statement about being wrong, while twenty points in open air is a coin flip on
noise. :class:`TradeContext` records both the raw facts and the tags derived
from them, so the tag rule can change without re-reading a single bar.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Sequence

from .core import Bar, Direction, ExitReason, Trade
from .levels import Level, LevelTracker, find_swings
from .sessions import DEFAULT_SESSION, SessionSpec

TREND_UP = "up"
TREND_DOWN = "down"
TREND_RANGE = "range"
TREND_UNKNOWN = "unknown"

AT_LEVEL = "at_level"
MID_RANGE = "mid_range"
CHASING = "chasing"
LOCATION_UNKNOWN = "unknown"

EXIT_STOPPED = "stopped"
EXIT_EARLY = "early"
EXIT_RODE = "rode"
EXIT_SCRATCH = "scratch"
EXIT_UNKNOWN = "unknown"


@dataclass(frozen=True)
class TaggingRules:
    """How the tags are derived from the facts. Change these, not the facts.

    ``location_mode`` picks which definition of "at a level" applies:

    ``"stop_distance"``
        The level is close enough that your stop sits beyond it -- so the stop
        means "the level broke" rather than "price wobbled". This is the
        definition that ties location to whether your risk was structural.
    ``"atr_zone"``
        The level is within ``level_zone_atr`` x ATR, regardless of your stop.
        Looser, and matches how the automated strategy picks its entries.
    """

    trend_timeframe: int = 5            # minutes, for reading structure
    swing_strength: int = 2             # bars either side of a 5-min swing
    atr_period: int = 14

    location_mode: str = "stop_distance"
    level_zone_atr: float = 0.35        # used by "atr_zone"
    min_level_rejections: int = 2       # how many holds make it a level
    chase_atr: float = 1.5              # run from the last swing that counts as chasing

    forward_window_minutes: int = 60    # how long to watch after an exit
    early_exit_multiple: float = 2.0    # offered >= 2x what you took => early
    scratch_atr: float = 0.10           # smaller than this is not a real win

    # Clock time, not bar count: with RTH-only data an early-session trade has
    # almost nothing behind it, so this has to reach back past a weekend.
    lookback_minutes: int = 3 * 24 * 60
    max_level_age_bars: int = 240       # ~3 sessions of 5-minute bars

    def __post_init__(self) -> None:
        if self.location_mode not in ("stop_distance", "atr_zone"):
            raise ValueError(
                "location_mode must be 'stop_distance' or 'atr_zone'"
            )


@dataclass
class TradeContext:
    """One trade, read against the bars. Facts first, tags derived from them."""

    trade_id: int | None
    symbol: str
    direction: str
    entry_time: datetime
    session_day: date

    has_bars: bool = False
    note: str = ""                       # why not, when has_bars is False

    # --- volatility, and what the stop is worth next to it ---------------
    atr_points: float | None = None
    stop_points: float | None = None
    stop_in_atr: float | None = None
    median_bar_range: float | None = None     # typical 5-min range that day
    stop_vs_bar: float | None = None          # stop / that range

    # --- structure --------------------------------------------------------
    trend: str = TREND_UNKNOWN
    with_trend: bool | None = None

    # --- the level evidence ----------------------------------------------
    level_price: float | None = None
    level_rejections: int = 0
    level_distance: float | None = None       # points from entry to the level
    stop_beyond_level: bool = False
    # The held level on the *other* side: the floor beneath a short, the
    # ceiling above a long. Needed to see a trade taken against a level that is
    # holding, and to know what the move has to get through.
    opposing_level_price: float | None = None
    opposing_level_rejections: int = 0
    opposing_level_distance: float | None = None
    run_from_swing: float | None = None       # how far it had already moved
    run_from_swing_atr: float | None = None
    location: str = LOCATION_UNKNOWN

    # --- what the trade actually did -------------------------------------
    realized_points: float = 0.0
    mae_points: float | None = None
    mfe_points: float | None = None
    forward_points: float | None = None       # more that was offered after exit
    capture_ratio: float | None = None        # share of the whole move you took
    capture_basis: str = ""                   # "forward" or "in_trade"
    exit_quality: str = EXIT_UNKNOWN

    # --- where in the day -------------------------------------------------
    day_range: float | None = None
    position_in_day_range: float | None = None   # 0 = day's low, 1 = day's high

    def tags(self) -> list[str]:
        """The tags, in the form the journal and breakdowns understand."""
        out = []
        if self.trend != TREND_UNKNOWN:
            out.append(f"trend:{self.trend}")
        if self.with_trend is not None:
            out.append("with-trend" if self.with_trend else "against-trend")
        if self.location != LOCATION_UNKNOWN:
            out.append(f"location:{self.location}")
        if self.exit_quality != EXIT_UNKNOWN:
            out.append(f"exit:{self.exit_quality}")
        return out

    def to_dict(self) -> dict:
        data = asdict(self)
        data["entry_time"] = self.entry_time.isoformat()
        data["session_day"] = str(self.session_day)
        data["tags"] = self.tags()
        return data


# --------------------------------------------------------------------------
# the pieces
# --------------------------------------------------------------------------

def average_true_range(bars: Sequence[Bar], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    window = bars[-(period + 1):]
    ranges = [
        max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        for previous, current in zip(window, window[1:])
    ]
    return sum(ranges) / len(ranges) if ranges else None


def read_trend(bars: Sequence[Bar], strength: int = 2) -> str:
    """Up, down or range, from the last two 5-minute swing highs and lows.

    This is the structure read the trader described: higher high *and* higher
    low to call it an uptrend. Anything less agreement is a range, which is an
    answer rather than a failure -- most of the session is a range.
    """
    highs, lows = find_swings(bars, strength)
    if len(highs) < 2 or len(lows) < 2:
        return TREND_UNKNOWN
    higher_high = highs[-1][1] > highs[-2][1]
    higher_low = lows[-1][1] > lows[-2][1]
    lower_high = highs[-1][1] < highs[-2][1]
    lower_low = lows[-1][1] < lows[-2][1]
    if higher_high and higher_low:
        return TREND_UP
    if lower_high and lower_low:
        return TREND_DOWN
    return TREND_RANGE


def run_since_last_swing(
    bars: Sequence[Bar], direction: Direction, entry_price: float, strength: int = 2
) -> float | None:
    """How far price had already travelled your way before you got in.

    Measured from the most recent swing in the opposite direction: for a long,
    the last swing low. A large run with no pullback is the shape of chasing.
    """
    highs, lows = find_swings(bars, strength)
    pivots = lows if direction is Direction.LONG else highs
    if not pivots:
        return None
    anchor = pivots[-1][1]
    run = (entry_price - anchor) if direction is Direction.LONG else (anchor - entry_price)
    return max(0.0, run)


def relevant_level(
    tracker: LevelTracker, direction: Direction, entry_price: float,
    min_rejections: int,
) -> Level | None:
    """The held level your risk would be placed against.

    For a long that is the nearest support at or below the entry; for a short,
    the nearest resistance at or above it.
    """
    kind = "support" if direction is Direction.LONG else "resistance"
    candidates = [
        level for level in tracker.active(kind)
        if level.rejections >= min_rejections
        and (level.price <= entry_price if direction is Direction.LONG
             else level.price >= entry_price)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda level: abs(entry_price - level.price))


def classify_location(
    context: TradeContext, rules: TaggingRules, direction: Direction
) -> str:
    """Turn the level facts into a location tag, per the configured rule."""
    has_level = (
        context.level_distance is not None
        and context.level_rejections >= rules.min_level_rejections
    )
    if has_level:
        if rules.location_mode == "stop_distance":
            # Close enough that the stop sits beyond the level, so being
            # stopped means the level gave way rather than price wobbled.
            near = (
                context.stop_points is not None
                and context.level_distance <= context.stop_points
                and context.stop_beyond_level
            )
        else:
            zone = rules.level_zone_atr * (context.atr_points or 0.0)
            near = zone > 0 and context.level_distance <= zone
        if near:
            return AT_LEVEL

    if (
        context.run_from_swing_atr is not None
        and context.run_from_swing_atr >= rules.chase_atr
    ):
        return CHASING
    return MID_RANGE


def excursions(
    bars: Sequence[Bar], direction: Direction, entry_price: float
) -> tuple[float, float]:
    """Worst and best points reached while the position was open."""
    if not bars:
        return 0.0, 0.0
    highest = max(bar.high for bar in bars)
    lowest = min(bar.low for bar in bars)
    if direction is Direction.LONG:
        return max(0.0, entry_price - lowest), max(0.0, highest - entry_price)
    return max(0.0, highest - entry_price), max(0.0, entry_price - lowest)


def classify_exit(
    context: TradeContext, trade: Trade, rules: TaggingRules,
    forward_bars: Sequence[Bar],
) -> tuple[str, float | None, float | None, str]:
    """Was the exit a stop, an early grab, or a ride?

    Returns ``(tag, forward_points, capture_ratio, basis)``. The forward window
    is what makes 'early' mean something: it asks what the move went on to
    offer after you were already out. Where there are no bars after the exit --
    the end of an import, usually -- it falls back to the trade's own best
    price, which answers a narrower question and says so in ``basis``.
    """
    direction = trade.direction
    realized = context.realized_points
    scratch_floor = rules.scratch_atr * (context.atr_points or 0.0)

    if realized <= 0:
        stopped = trade.exit_reason is ExitReason.STOP or (
            trade.stop_price is not None
            and trade.exit_price is not None
            and abs(trade.exit_price - trade.stop_price) <= max(1e-9, scratch_floor)
        )
        return (EXIT_STOPPED if stopped else EXIT_SCRATCH if realized == 0 else EXIT_STOPPED,
                None, None, "")

    if realized < scratch_floor:
        return EXIT_SCRATCH, None, None, ""

    if not forward_bars:
        # No view of what happened next; judge against the trade's own best.
        peak = context.mfe_points or realized
        capture = realized / peak if peak > 0 else None
        tag = (
            EXIT_EARLY if peak >= rules.early_exit_multiple * realized else EXIT_RODE
        )
        return tag, None, capture, "in_trade"

    best = (
        max(bar.high for bar in forward_bars) if direction is Direction.LONG
        else min(bar.low for bar in forward_bars)
    )
    exit_price = trade.exit_price
    forward = max(
        0.0,
        (best - exit_price) if direction is Direction.LONG else (exit_price - best),
    )
    total = realized + forward
    capture = realized / total if total > 0 else None
    if total >= rules.early_exit_multiple * realized:
        return EXIT_EARLY, forward, capture, "forward"
    return EXIT_RODE, forward, capture, "forward"


# --------------------------------------------------------------------------
# putting it together
# --------------------------------------------------------------------------

def annotate(
    trade: Trade,
    minute_bars: Sequence[Bar],
    rules: TaggingRules | None = None,
    session: SessionSpec = DEFAULT_SESSION,
) -> TradeContext:
    """Read one trade against a window of 1-minute bars around it.

    The window must start well before the entry (structure needs history) and
    run past the exit (the forward window needs the future). :func:`annotate_all`
    fetches windows of the right shape for you.
    """
    rules = rules or TaggingRules()
    context = TradeContext(
        trade_id=trade.trade_id,
        symbol=trade.symbol,
        direction=trade.direction.value,
        entry_time=trade.entry_time,
        session_day=session.session_day(trade.entry_time),
    )
    if not minute_bars:
        context.note = "no bar data covering this trade"
        return context

    before = [bar for bar in minute_bars if bar.ts <= trade.entry_time]
    if len(before) < rules.atr_period + 1:
        context.note = "not enough bars before the entry to read context"
        return context

    context.has_bars = True
    from .data.loaders import resample

    coarse = resample(before, rules.trend_timeframe)
    context.atr_points = average_true_range(coarse, rules.atr_period)

    # --- the stop, next to how far price travels ------------------------
    if trade.stop_price is not None:
        context.stop_points = abs(trade.entry_price - trade.stop_price)
        if context.atr_points:
            context.stop_in_atr = context.stop_points / context.atr_points

    day_bars = [
        bar for bar in minute_bars
        if session.session_day(bar.ts) == context.session_day
    ]
    day_coarse = resample(day_bars, rules.trend_timeframe) if day_bars else []
    if day_coarse:
        context.median_bar_range = statistics.median(bar.range for bar in day_coarse)
        if context.stop_points and context.median_bar_range:
            context.stop_vs_bar = context.stop_points / context.median_bar_range
    if day_bars:
        high = max(bar.high for bar in day_bars)
        low = min(bar.low for bar in day_bars)
        context.day_range = high - low
        if context.day_range > 0:
            context.position_in_day_range = (trade.entry_price - low) / context.day_range

    # --- structure -------------------------------------------------------
    context.trend = read_trend(coarse, rules.swing_strength)
    if context.trend in (TREND_UP, TREND_DOWN):
        wanted = TREND_UP if trade.direction is Direction.LONG else TREND_DOWN
        context.with_trend = context.trend == wanted

    # --- levels ----------------------------------------------------------
    # One tolerance for the whole window, from the ATR we already measured.
    # Letting the tracker scale tolerance bar by bar would leave it blind until
    # ATR warmed up -- 75 minutes on 5-minute bars -- so every trade early in a
    # session would come back with no level and no location.
    tolerance = rules.level_zone_atr * (context.atr_points or 0.0)
    tracker = LevelTracker(
        pivot_strength=rules.swing_strength,
        tolerance_points=tolerance if tolerance > 0 else None,
        tolerance_atr=rules.level_zone_atr,
        min_bars_between_touches=rules.swing_strength,
        max_level_age_bars=rules.max_level_age_bars,
    )
    for index in range(len(coarse)):
        tracker.observe(coarse, index, context.atr_points)

    level = relevant_level(
        tracker, trade.direction, trade.entry_price, rules.min_level_rejections
    )
    if level is not None:
        context.level_price = level.price
        context.level_rejections = level.rejections
        context.level_distance = abs(trade.entry_price - level.price)
        if trade.stop_price is not None:
            context.stop_beyond_level = (
                trade.stop_price < level.floor
                if trade.direction is Direction.LONG
                else trade.stop_price > level.floor
            )

    opposing = relevant_level(
        tracker, trade.direction.opposite, trade.entry_price,
        rules.min_level_rejections,
    )
    if opposing is not None:
        context.opposing_level_price = opposing.price
        context.opposing_level_rejections = opposing.rejections
        context.opposing_level_distance = abs(trade.entry_price - opposing.price)

    context.run_from_swing = run_since_last_swing(
        coarse, trade.direction, trade.entry_price, rules.swing_strength
    )
    if context.run_from_swing is not None and context.atr_points:
        context.run_from_swing_atr = context.run_from_swing / context.atr_points
    context.location = classify_location(context, rules, trade.direction)

    # --- what the trade did ----------------------------------------------
    if trade.is_closed:
        context.realized_points = trade.points
        held = [
            bar for bar in minute_bars
            if trade.entry_time <= bar.ts <= trade.exit_time
        ]
        mae, mfe = excursions(held, trade.direction, trade.entry_price)
        context.mae_points, context.mfe_points = mae, mfe

        window_end = trade.exit_time + timedelta(minutes=rules.forward_window_minutes)
        forward_bars = [
            bar for bar in minute_bars if trade.exit_time < bar.ts <= window_end
        ]
        quality, forward, capture, basis = classify_exit(
            context, trade, rules, forward_bars
        )
        context.exit_quality = quality
        context.forward_points = forward
        context.capture_ratio = capture
        context.capture_basis = basis

    return context


def annotate_all(
    trades: Sequence[Trade],
    store,
    rules: TaggingRules | None = None,
    session: SessionSpec = DEFAULT_SESSION,
) -> list[TradeContext]:
    """Read every trade against a :class:`~ym.barstore.BarStore`.

    Bars are fetched once per symbol and trading day rather than once per
    trade, because a night's trades share the same window.
    """
    rules = rules or TaggingRules()
    grouped: dict[tuple[str, date], list[Trade]] = {}
    for trade in trades:
        key = (trade.symbol, session.session_day(trade.entry_time))
        grouped.setdefault(key, []).append(trade)

    results: list[TradeContext] = []
    for (symbol, day), group in sorted(grouped.items(), key=lambda item: item[0][1]):
        first = min(trade.entry_time for trade in group)
        last = max(
            (trade.exit_time or trade.entry_time) for trade in group
        )
        window = store.bars(
            symbol, minutes=1,
            start=first - timedelta(minutes=rules.lookback_minutes),
            end=last + timedelta(minutes=rules.forward_window_minutes + 5),
        )
        for trade in group:
            results.append(annotate(trade, window, rules, session))
    results.sort(key=lambda item: item.entry_time)
    return results


def apply_tags(trades: Sequence[Trade], contexts: Sequence[TradeContext]) -> int:
    """Copy the derived tags onto the trades themselves, in place.

    Existing tags are kept; only the ones this module owns are replaced, so a
    re-run does not pile up duplicates or discard anything you wrote yourself.
    """
    owned = ("trend:", "location:", "exit:", "with-trend", "against-trend")
    by_id = {context.trade_id: context for context in contexts if context.trade_id}
    changed = 0
    for trade in trades:
        context = by_id.get(trade.trade_id)
        if context is None or not context.has_bars:
            continue
        kept = [tag for tag in trade.tags if not tag.startswith(owned)]
        trade.tags = kept + context.tags()
        changed += 1
    return changed
