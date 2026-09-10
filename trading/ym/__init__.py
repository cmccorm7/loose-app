"""Risk management, backtesting and behavioral review for Dow e-mini futures.

The entry points most code needs::

    from ym import YM, MYM, RiskLimits, RiskManager
    from ym.backtest import Backtester, BacktestConfig, Signal, Strategy
    from ym.journal import Journal
    from ym.behavior import analyze
    from ym.coach import Coach

Nothing in this package connects to a broker or places an order. It sizes
positions, enforces limits, measures results and reviews habits; execution stays
in your hands and your platform.
"""

from .core import Bar, Direction, ExitReason, Trade
from .instruments import MYM, YM, Instrument, get_instrument
from .metrics import Metrics, compute_metrics
from .risk import RiskDecision, RiskLimits, RiskManager, SizingMethod
from .sessions import DEFAULT_SESSION, Session, SessionSpec

__version__ = "0.1.0"

__all__ = [
    "Bar",
    "DEFAULT_SESSION",
    "Direction",
    "ExitReason",
    "Instrument",
    "MYM",
    "Metrics",
    "RiskDecision",
    "RiskLimits",
    "RiskManager",
    "Session",
    "SessionSpec",
    "SizingMethod",
    "Trade",
    "YM",
    "compute_metrics",
    "get_instrument",
    "__version__",
]
