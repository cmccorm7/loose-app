"""Synthetic YM bars, so the system is testable before you export anything.

The generator is a random walk with a U-shaped intraday volatility profile and
a per-day drift, which is enough to exercise the plumbing and the risk rules.
It is **not** a market simulator: results from synthetic data say nothing about
whether a strategy works. Use it for tests and demos, then re-run on your own
NinjaTrader exports before believing anything.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from ..core import Bar
from ..sessions import DEFAULT_SESSION, SessionSpec

# Rough shape of index-futures volatility through the cash session: busy at the
# open, quiet at lunch, busy again into the close.
_INTRADAY_PROFILE = (
    (9.5, 2.4), (10.0, 1.9), (10.5, 1.5), (11.0, 1.2), (12.0, 0.8),
    (13.0, 0.7), (14.0, 0.9), (15.0, 1.4), (15.5, 1.8), (16.0, 2.0),
)


def _raw_factor(hour_float: float) -> float:
    previous_hour, previous_factor = _INTRADAY_PROFILE[0]
    for point_hour, factor in _INTRADAY_PROFILE:
        if hour_float <= point_hour:
            span = point_hour - previous_hour
            weight = 0.0 if span <= 0 else (hour_float - previous_hour) / span
            return previous_factor + weight * (factor - previous_factor)
        previous_hour, previous_factor = point_hour, factor
    return previous_factor


def _profile_rms() -> float:
    """RMS of the raw profile across the cash session.

    Dividing by this keeps the shape while leaving total session variance equal
    to ``daily_vol_pct``. Without it, scaling minute vol by a peaked profile
    silently inflates the daily range well past the number you asked for.
    """
    samples = [_raw_factor(570 / 60.0 + i / 60.0) for i in range(0, 391)]
    return (sum(value ** 2 for value in samples) / len(samples)) ** 0.5


_PROFILE_RMS = _profile_rms()


def _vol_factor(hour_float: float, session: SessionSpec) -> float:
    open_hour = session.rth_open.hour + session.rth_open.minute / 60.0
    close_hour = session.rth_close.hour + session.rth_close.minute / 60.0
    if hour_float < open_hour or hour_float > close_hour:
        return 0.35  # overnight and premarket are much thinner
    return _raw_factor(hour_float) / _PROFILE_RMS


def generate_bars(
    start: date,
    days: int = 20,
    minutes: int = 1,
    start_price: float = 41000.0,
    daily_vol_pct: float = 0.75,
    trend_strength: float = 0.35,
    include_overnight: bool = False,
    seed: int | None = 7,
    session: SessionSpec = DEFAULT_SESSION,
    tick_size: float = 1.0,
) -> list[Bar]:
    """Generate ``days`` trading days of bars starting on ``start``.

    ``trend_strength`` is how strongly each day drifts (0 = pure noise).
    """
    rng = random.Random(seed)
    tz = session.tz
    bars: list[Bar] = []
    price = start_price
    day = start
    generated_days = 0

    first_minute = 0 if include_overnight else session.premarket_start.hour * 60
    last_minute = 24 * 60 if include_overnight else session.rth_close.hour * 60

    while generated_days < days:
        if day.weekday() >= 5:
            day += timedelta(days=1)
            continue

        # One drift for the whole day, so days trend instead of pure chop.
        drift_per_bar = (
            rng.gauss(0, 1)
            * trend_strength
            * price
            * daily_vol_pct
            / 100.0
            / max(1, (last_minute - first_minute) // minutes)
        )

        for minute_of_day in range(first_minute, last_minute, minutes):
            ts = datetime.combine(day, datetime.min.time(), tzinfo=tz) + timedelta(
                minutes=minute_of_day
            )
            if not session.is_open(ts):
                continue
            hour_float = minute_of_day / 60.0
            factor = _vol_factor(hour_float, session)
            sigma = (
                price
                * daily_vol_pct
                / 100.0
                / (390 ** 0.5)
                * factor
                * (minutes ** 0.5)
            )

            open_price = price
            close_price = open_price + drift_per_bar + rng.gauss(0, sigma)
            wick_up = abs(rng.gauss(0, sigma * 0.45))
            wick_down = abs(rng.gauss(0, sigma * 0.45))
            high = max(open_price, close_price) + wick_up
            low = min(open_price, close_price) - wick_down
            volume = max(1.0, round(rng.gauss(900 * factor, 250 * factor)))

            snap = lambda value: round(round(value / tick_size) * tick_size, 10)
            bar = Bar(
                ts=ts,
                open=snap(open_price),
                high=snap(high),
                low=snap(low),
                close=snap(close_price),
                volume=volume,
            )
            bars.append(bar)
            price = bar.close

        generated_days += 1
        day += timedelta(days=1)
        price += rng.gauss(0, price * daily_vol_pct / 100.0 * 0.3)  # overnight gap

    return bars
