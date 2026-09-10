"""Example strategies. Treat them as templates, not trade recommendations."""

from .ma_pullback import MovingAveragePullback
from .opening_range import OpeningRangeBreakout

REGISTRY = {
    "orb": OpeningRangeBreakout,
    "ma_pullback": MovingAveragePullback,
}

__all__ = ["MovingAveragePullback", "OpeningRangeBreakout", "REGISTRY"]
