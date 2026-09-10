"""Core value types shared by the data, risk, backtest and journal layers.

A :class:`Trade` is deliberately self-contained -- it carries its own
``point_value`` -- so a trade loaded from the journal computes its P&L and
R-multiple identically to one produced by the backtester, with no lookups.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1

    @property
    def opposite(self) -> "Direction":
        return Direction.SHORT if self is Direction.LONG else Direction.LONG

    @classmethod
    def parse(cls, value: str | "Direction") -> "Direction":
        if isinstance(value, cls):
            return value
        key = str(value).strip().lower()
        if key in ("long", "buy", "l", "b", "1"):
            return cls.LONG
        if key in ("short", "sell", "s", "-1"):
            return cls.SHORT
        raise ValueError(f"cannot parse direction from {value!r}")


class ExitReason(str, Enum):
    STOP = "stop"
    TARGET = "target"
    SIGNAL = "signal"
    SESSION_CLOSE = "session_close"
    TIME_STOP = "time_stop"
    END_OF_DATA = "end_of_data"
    MANUAL = "manual"


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar. ``ts`` is the bar's *open* time and must be tz-aware."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise ValueError("Bar.ts must be timezone-aware")
        if self.high < self.low:
            raise ValueError(f"bar at {self.ts}: high {self.high} < low {self.low}")

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def is_up(self) -> bool:
        return self.close >= self.open

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3.0


@dataclass
class Trade:
    """A round-turn position, open or closed.

    ``stop_price`` is the *initial* protective stop. It defines 1R and is never
    rewritten when a stop is trailed -- otherwise every trade would report a
    flattering R-multiple.
    """

    symbol: str
    direction: Direction
    entry_time: datetime
    entry_price: float
    contracts: int
    point_value: float
    stop_price: float | None = None
    target_price: float | None = None
    exit_time: datetime | None = None
    exit_price: float | None = None
    commission: float = 0.0
    mae_points: float = 0.0          # worst excursion against the position
    mfe_points: float = 0.0          # best excursion in favor
    exit_reason: ExitReason | None = None
    setup: str = ""
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    trade_id: int | None = None

    @property
    def is_closed(self) -> bool:
        return self.exit_price is not None and self.exit_time is not None

    @property
    def points(self) -> float:
        """Signed points captured. Zero while the trade is still open."""
        if not self.is_closed:
            return 0.0
        return (self.exit_price - self.entry_price) * self.direction.sign

    @property
    def gross_pnl(self) -> float:
        return self.points * self.point_value * self.contracts

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.commission

    @property
    def is_winner(self) -> bool:
        return self.net_pnl > 0

    @property
    def risk_points(self) -> float | None:
        """Distance from entry to the initial stop, in points."""
        if self.stop_price is None:
            return None
        return abs(self.entry_price - self.stop_price)

    @property
    def risk_dollars(self) -> float | None:
        risk = self.risk_points
        if risk is None:
            return None
        return risk * self.point_value * self.contracts

    def _as_r(self, dollars: float) -> float | None:
        risk = self.risk_dollars
        if not risk:
            return None
        return dollars / risk

    @property
    def r_multiple(self) -> float | None:
        """Net P&L expressed in units of initial risk."""
        if not self.is_closed:
            return None
        return self._as_r(self.net_pnl)

    @property
    def mae_r(self) -> float | None:
        return self._as_r(-abs(self.mae_points) * self.point_value * self.contracts)

    @property
    def mfe_r(self) -> float | None:
        return self._as_r(abs(self.mfe_points) * self.point_value * self.contracts)

    @property
    def duration(self) -> timedelta | None:
        if not self.is_closed:
            return None
        return self.exit_time - self.entry_time

    @property
    def planned_r_multiple(self) -> float | None:
        """Reward-to-risk the trade was taken for, from stop and target."""
        if self.stop_price is None or self.target_price is None:
            return None
        risk = self.risk_points
        if not risk:
            return None
        return abs(self.target_price - self.entry_price) / risk

    def close(
        self,
        exit_time: datetime,
        exit_price: float,
        reason: ExitReason = ExitReason.MANUAL,
        commission: float | None = None,
    ) -> "Trade":
        self.exit_time = exit_time
        self.exit_price = exit_price
        self.exit_reason = reason
        if commission is not None:
            self.commission = commission
        return self

    def update_excursions(self, high: float, low: float) -> None:
        """Fold one bar's extremes into MAE/MFE. Call once per bar held."""
        if self.direction is Direction.LONG:
            adverse = self.entry_price - low
            favorable = high - self.entry_price
        else:
            adverse = high - self.entry_price
            favorable = self.entry_price - low
        self.mae_points = max(self.mae_points, adverse, 0.0)
        self.mfe_points = max(self.mfe_points, favorable, 0.0)
