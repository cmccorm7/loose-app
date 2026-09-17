"""YM Desk: a local hub for trading statements and their analysis."""

from .config import Paths, paths
from .services import Hub, HubError
from .store import StatementRecord, Store

__version__ = "0.1.0"

__all__ = ["Hub", "HubError", "Paths", "StatementRecord", "Store", "paths", "__version__"]
