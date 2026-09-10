"""Contract specifications for the CBOT Dow futures complex.

Everything downstream (sizing, fills, P&L) converts price movement to dollars
through an :class:`Instrument`, so there is exactly one place to correct if a
spec ever changes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Instrument:
    """A futures contract spec.

    ``point_value`` is dollars per 1.00 index point. ``tick_size`` is the
    minimum price increment, so one tick is worth ``point_value * tick_size``.

    ``commission_per_side`` and ``day_margin`` are broker-specific and change
    often -- they are defaults, not facts. Override them for your account.
    """

    symbol: str
    name: str
    point_value: float
    tick_size: float
    commission_per_side: float = 0.0
    exchange_tz: str = "America/New_York"
    currency: str = "USD"
    day_margin: float = 0.0
    initial_margin: float = 0.0

    @property
    def tick_value(self) -> float:
        """Dollars per tick, per contract."""
        return self.point_value * self.tick_size

    def points_to_dollars(self, points: float, contracts: int = 1) -> float:
        return points * self.point_value * contracts

    def dollars_to_points(self, dollars: float, contracts: int = 1) -> float:
        if contracts <= 0:
            raise ValueError("contracts must be positive")
        return dollars / (self.point_value * contracts)

    def ticks_to_points(self, ticks: float) -> float:
        return ticks * self.tick_size

    def points_to_ticks(self, points: float) -> float:
        return points / self.tick_size

    def round_to_tick(self, price: float, mode: str = "nearest") -> float:
        """Snap ``price`` to a valid tick.

        ``mode`` is ``"nearest"``, ``"up"`` or ``"down"``. Use ``up``/``down``
        when you need the conservative side of a stop or target.
        """
        ratio = price / self.tick_size
        if mode == "nearest":
            ticks = math.floor(ratio + 0.5)
        elif mode == "up":
            ticks = math.ceil(ratio - 1e-9)
        elif mode == "down":
            ticks = math.floor(ratio + 1e-9)
        else:
            raise ValueError(f"unknown rounding mode: {mode!r}")
        return round(ticks * self.tick_size, 10)

    def round_turn_commission(self, contracts: int = 1) -> float:
        return self.commission_per_side * 2 * contracts

    def with_costs(
        self,
        commission_per_side: float | None = None,
        day_margin: float | None = None,
    ) -> "Instrument":
        """Return a copy carrying your broker's numbers."""
        changes = {}
        if commission_per_side is not None:
            changes["commission_per_side"] = commission_per_side
        if day_margin is not None:
            changes["day_margin"] = day_margin
        return replace(self, **changes)


# E-mini Dow. $5 per index point, 1.00 point minimum tick => $5.00 per tick.
YM = Instrument(
    symbol="YM",
    name="E-mini Dow ($5) Futures",
    point_value=5.0,
    tick_size=1.0,
    commission_per_side=2.00,
    day_margin=1000.0,
    initial_margin=9000.0,
)

# Micro E-mini Dow. One tenth of YM: $0.50 per point, $0.50 per tick.
MYM = Instrument(
    symbol="MYM",
    name="Micro E-mini Dow ($0.50) Futures",
    point_value=0.5,
    tick_size=1.0,
    commission_per_side=0.50,
    day_margin=100.0,
    initial_margin=900.0,
)

REGISTRY: dict[str, Instrument] = {inst.symbol: inst for inst in (YM, MYM)}


def get_instrument(symbol: str) -> Instrument:
    """Look up a contract by symbol, tolerating a month/year suffix (``YMZ5``)."""
    key = symbol.strip().upper()
    if key in REGISTRY:
        return REGISTRY[key]
    for length in (4, 3, 2):
        if key[:length] in REGISTRY:
            return REGISTRY[key[:length]]
    raise KeyError(
        f"unknown instrument {symbol!r}; known symbols: {sorted(REGISTRY)}"
    )
