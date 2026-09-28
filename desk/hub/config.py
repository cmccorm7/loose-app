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

    If the engine is missing, say so in words. The alternative is
    ``ModuleNotFoundError: No module named 'ym'`` several frames later, which
    does not tell you that you copied half a project.
    """
    if not (TRADING_ROOT / "ym").is_dir():
        raise RuntimeError(
            f"The trading engine is missing. YM Desk expects to find it at:\n"
            f"  {TRADING_ROOT}\n\n"
            f"That folder is part of the same project as this one. If you copied "
            f"or downloaded only the 'desk' folder, fetch the whole project -- "
            f"'desk' and 'trading' have to sit side by side."
        )
    path = str(TRADING_ROOT)
    if path not in sys.path:
        sys.path.insert(0, path)


bootstrap_engine_path()


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def journal(self) -> Path:
        return self.home / "journal.db"

    @property
    def bars(self) -> Path:
        return self.home / "bars.db"

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
    # Which definition of "at a held level" the tags use. "stop_distance" asks
    # whether your stop sits beyond the level, so the tag answers whether risk
    # was structural; "atr_zone" only asks whether the level is nearby.
    "location_mode": "stop_distance",
    # Fixed dollars risked per trade, for accounts sized in dollars rather than
    # percentages. None falls back to risk_per_trade_pct.
    "fixed_dollar_risk": None,
    # Nothing is sent to a model provider without an explicit approval step.
    "ai_share_mode": "ask",
}


# What each setting is allowed to be. A wrong value caught here names the
# setting; the same value caught later surfaces as a failure deep in analysis,
# a long way from the mistake.
CHOICES = {
    "location_mode": ("stop_distance", "atr_zone"),
    "ai_share_mode": ("ask", "summaries", "full"),
}

POSITIVE = ("equity", "risk_per_trade_pct", "fixed_dollar_risk",
            "default_stop_points", "max_daily_loss_pct", "max_drawdown_pct")


def validate_settings(changes: dict) -> None:
    """Raise ValueError on a setting that cannot mean anything."""
    for key, allowed in CHOICES.items():
        if key in changes and changes[key] not in allowed:
            raise ValueError(
                f"{key} must be one of {list(allowed)}, not {changes[key]!r}"
            )
    for key in POSITIVE:
        value = changes.get(key)
        if value is None:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{key} must be a number, not {value!r}")
        if value <= 0:
            raise ValueError(f"{key} must be greater than zero, not {value}")
    if "timezone" in changes:
        from .config import exchange_timezone_check
        exchange_timezone_check(changes["timezone"])


def exchange_timezone_check(name: str) -> None:
    """A timezone typo silently shifts every session boundary, so check it."""
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(str(name))
    except Exception as exc:
        raise ValueError(f"unknown timezone {name!r}") from exc
