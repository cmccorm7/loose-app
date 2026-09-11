"""Example strategies. Treat them as templates, not trade recommendations."""

from .ma_pullback import MovingAveragePullback
from .opening_range import OpeningRangeBreakout
from .support_rejection import SupportRejection

REGISTRY = {
    "orb": OpeningRangeBreakout,
    "ma_pullback": MovingAveragePullback,
    "support_rejection": SupportRejection,
}

__all__ = [
    "MovingAveragePullback",
    "OpeningRangeBreakout",
    "SupportRejection",
    "REGISTRY",
]
