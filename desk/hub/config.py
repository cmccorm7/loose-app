"""Where the app lives on disk, and how it finds the trading engine.

Everything the hub owns sits under one directory (``~/.ym-desk`` by default,
or wherever ``YM_DESK_HOME`` points). That means your journal, your uploaded
statements and your settings are one folder you can back up or delete.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TRADING_ROOT = REPO_ROOT / "trading"


def bootstrap_engine_path() -> None:
    """Put the ``ym`` package on the import path.

    The trading engine is a sibling directory rather than an installed package,
    so the hub adds it explicitly. Doing this here means no module has to care.
    """
    path = str(TRADING_ROOT)
    if TRADING_ROOT.is_dir() and path not in sys.path:
        sys.path.insert(0, path)


bootstrap_engine_path()


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def journal(self) -> Path:
        return self.home / "journal.db"

    @property
    def uploads(self) -> Path:
        return self.home / "uploads"

    @property
    def manifest(self) -> Path:
        return self.home / "statements.json"

    @property
    def settings(self) -> Path:
        return self.home / "settings.json"

    def ensure(self) -> "Paths":
        self.home.mkdir(parents=True, exist_ok=True)
        self.uploads.mkdir(parents=True, exist_ok=True)
        return self


def paths(home: str | os.PathLike | None = None) -> Paths:
    """Resolve the data directory: argument, then ``YM_DESK_HOME``, then default."""
    if home is not None:
        root = Path(home)
    else:
        root = Path(os.environ.get("YM_DESK_HOME", Path.home() / ".ym-desk"))
    return Paths(root.expanduser().resolve()).ensure()


DEFAULT_SETTINGS = {
    "symbol": "MYM",
    "equity": 25_000.0,
    "timezone": "America/New_York",
    "risk_per_trade_pct": 0.5,
    "max_daily_loss_pct": 2.0,
    "max_drawdown_pct": 10.0,
    # Statements rarely record your stop. This is the fallback used to give
    # imported trades an R-multiple; None means leave R unavailable.
    "default_stop_points": None,
    # Nothing is sent to a model provider without an explicit approval step.
    "ai_share_mode": "ask",
}
