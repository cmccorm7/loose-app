"""Session and trading-day logic for CME/CBOT index futures.

Two clocks matter and they disagree:

* the **calendar day** you see on a chart, and
* the **trading day** (the "session day"), which opens at 18:00 ET the previous
  evening and closes at 17:00 ET.

Risk limits reset on the trading day, not the calendar day -- a loss taken at
20:00 ET Monday belongs to Tuesday's daily loss limit. Everything here is
timezone-aware; naive datetimes are rejected rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum

try:
    from zoneinfo import ZoneInfo
except ImportError as exc:  # pragma: no cover - Python < 3.9
    raise ImportError("Python 3.9+ with zoneinfo is required") from exc

EXCHANGE_TZ_NAME = "America/New_York"


def exchange_tz(name: str = EXCHANGE_TZ_NAME) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception as exc:  # pragma: no cover - depends on host tzdata
        raise RuntimeError(
            f"could not load timezone {name!r}. On Windows run: pip install tzdata"
        ) from exc


class Session(str, Enum):
    """Which part of the 23-hour day a timestamp falls in."""

    OVERNIGHT = "overnight"    # 18:00 ET open through the pre-open
    PREMARKET = "premarket"    # 04:00 ET to the cash open
    RTH = "rth"                # 09:30-16:00 ET, the cash session
    AFTERNOON = "afternoon"    # 16:00-17:00 ET, post-cash into the halt
    CLOSED = "closed"          # 17:00-18:00 ET daily halt, and weekends


@dataclass(frozen=True)
class SessionSpec:
    """Session boundaries, all in exchange-local time.

    Defaults describe YM: the globex day opens 18:00 ET Sunday-Thursday, the
    cash session runs 09:30-16:00 ET, and there is a daily halt 17:00-18:00 ET.
    """

    tz_name: str = EXCHANGE_TZ_NAME
    globex_open: time = time(18, 0)
    globex_close: time = time(17, 0)
    premarket_start: time = time(4, 0)
    rth_open: time = time(9, 30)
    rth_close: time = time(16, 0)

    @property
    def tz(self) -> ZoneInfo:
        return exchange_tz(self.tz_name)

    def localize(self, ts: datetime) -> datetime:
        """Convert an aware timestamp into exchange-local time."""
        if ts.tzinfo is None:
            raise ValueError(
                "naive datetime; attach a timezone (see data.loaders) before "
                "asking about sessions"
            )
        return ts.astimezone(self.tz)

    def session_day(self, ts: datetime) -> date:
        """The trading day a timestamp belongs to.

        At or after ``globex_open`` the timestamp belongs to the *next*
        calendar day's session; Friday evening rolls to Monday.
        """
        local = self.localize(ts)
        day = local.date()
        if local.time() >= self.globex_open:
            day = day + timedelta(days=1)
        while day.weekday() >= 5:  # Saturday/Sunday are not trading days
            day = day + timedelta(days=1)
        return day

    def session_of(self, ts: datetime) -> Session:
        local = self.localize(ts)
        t = local.time()
        weekday = local.weekday()

        if weekday == 5:  # Saturday: closed all day
            return Session.CLOSED
        if weekday == 6:  # Sunday: nothing until the 18:00 ET reopen
            return Session.OVERNIGHT if t >= self.globex_open else Session.CLOSED
        if weekday == 4 and t >= self.globex_close:  # Friday after the close
            return Session.CLOSED

        if t >= self.globex_open:
            return Session.OVERNIGHT
        if t >= self.globex_close:
            return Session.CLOSED
        if t >= self.rth_close:
            return Session.AFTERNOON
        if t >= self.rth_open:
            return Session.RTH
        if t >= self.premarket_start:
            return Session.PREMARKET
        return Session.OVERNIGHT

    def is_rth(self, ts: datetime) -> bool:
        return self.session_of(ts) is Session.RTH

    def is_open(self, ts: datetime) -> bool:
        return self.session_of(ts) is not Session.CLOSED

    def rth_open_at(self, day: date) -> datetime:
        return datetime.combine(day, self.rth_open, tzinfo=self.tz)

    def rth_close_at(self, day: date) -> datetime:
        return datetime.combine(day, self.rth_close, tzinfo=self.tz)

    def minutes_since_rth_open(self, ts: datetime) -> float:
        """Minutes elapsed since the cash open (negative before it)."""
        local = self.localize(ts)
        return (local - self.rth_open_at(local.date())).total_seconds() / 60.0

    def flatten_time(self, day: date, minutes_before_close: int = 5) -> datetime:
        """When an intraday strategy should be flat, ahead of the cash close."""
        return self.rth_close_at(day) - timedelta(minutes=minutes_before_close)


DEFAULT_SESSION = SessionSpec()
