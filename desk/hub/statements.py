"""Working out what an uploaded file is, and what importing it would do.

Statements are messy and they are not all the same shape. The rule here is that
nothing is written to the journal until you have seen what the parser made of
the file: upload produces a **preview**, and importing is a separate, explicit
step. A misread statement is far easier to reject than to unpick afterwards.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .config import bootstrap_engine_path

bootstrap_engine_path()

from ym.core import Trade                             # noqa: E402
from ym.data import DataFormatError, load_bars, summarize  # noqa: E402
from ym.journal import parse_trade_csv                # noqa: E402
from ym.sessions import DEFAULT_SESSION               # noqa: E402

TRADE_KIND = "trades"
BAR_KIND = "bars"
UNKNOWN_KIND = "unknown"

# Header names that identify a file without having to parse it.
_TRADE_MARKERS = {
    "entry time", "entry_time", "market pos", "market pos.", "market position",
    "entry price", "entry_price", "exit price", "exit_price",
}
_BAR_MARKERS = {"open", "high", "low", "close", "o", "h", "l", "c"}
_NINJA_MARKERS = {"market pos.", "market pos", "trade number", "cum. profit"}


class StatementError(ValueError):
    """The file cannot be read as anything the hub understands."""


@dataclass
class Preview:
    """What the hub believes a file contains. Shown before anything is stored."""

    kind: str
    format_label: str
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    warnings: list[str] = field(default_factory=list)
    sample: list[dict] = field(default_factory=list)
    period: dict = field(default_factory=dict)
    symbols: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "format_label": self.format_label,
            "columns": self.columns,
            "row_count": self.row_count,
            "warnings": self.warnings,
            "sample": self.sample,
            "period": self.period,
            "symbols": self.symbols,
            "notes": self.notes,
            "summary": self.summary,
        }


def file_digest(path: str | Path) -> str:
    """SHA-256 of the file, used to notice a statement uploaded twice."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _header(path: Path) -> tuple[list[str], str]:
    """First non-empty line, split on whichever delimiter dominates it."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            if line.strip():
                first = line.rstrip("\r\n")
                break
        else:
            raise StatementError("the file is empty")
    delimiter = max((";", ",", "\t", "|"), key=first.count)
    if first.count(delimiter) == 0:
        delimiter = ","
    cells = [cell.strip().strip('"').lower() for cell in first.split(delimiter)]
    return cells, delimiter


def detect_kind(path: str | Path) -> tuple[str, str]:
    """Classify a file as trades, bars or unknown. Returns ``(kind, label)``."""
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        raise StatementError(
            "PDF statements are not supported yet. Export your trades as CSV "
            "instead -- in NinjaTrader that is Control Center > Trade "
            "Performance > Trades tab > right-click > Export."
        )
    cells, _ = _header(path)
    header_set = set(cells)

    if header_set & _NINJA_MARKERS:
        return TRADE_KIND, "NinjaTrader trade export"
    if header_set & _TRADE_MARKERS:
        return TRADE_KIND, "trade list"
    if len(header_set & _BAR_MARKERS) >= 3:
        return BAR_KIND, "OHLCV bar data"

    # No header at all: NinjaTrader's historical export is bare numbers.
    numeric_first_row = sum(
        1 for cell in cells if cell.replace(".", "").replace("-", "").isdigit()
    )
    if numeric_first_row >= 4:
        return BAR_KIND, "NinjaTrader bar export (no header)"
    return UNKNOWN_KIND, "unrecognized"


def _trade_row(trade: Trade) -> dict:
    return {
        "entry_time": trade.entry_time.isoformat(),
        "exit_time": trade.exit_time.isoformat() if trade.exit_time else None,
        "symbol": trade.symbol,
        "direction": trade.direction.value,
        "contracts": trade.contracts,
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "stop_price": trade.stop_price,
        "net_pnl": round(trade.net_pnl, 2) if trade.is_closed else None,
        "r_multiple": (
            None if trade.r_multiple is None else round(trade.r_multiple, 3)
        ),
        "setup": trade.setup,
    }


def parse_trades(
    path: str | Path,
    symbol: str | None = None,
    tz: str | None = None,
    excursion_unit: str = "currency",
    default_stop_points: float | None = None,
) -> tuple[list[Trade], list[str]]:
    try:
        return parse_trade_csv(
            path,
            symbol=symbol,
            tz=tz,
            excursion_unit=excursion_unit,
            default_stop_points=default_stop_points,
            session=DEFAULT_SESSION,
        )
    except ValueError as exc:
        raise StatementError(str(exc)) from exc


def preview(
    path: str | Path,
    symbol: str | None = None,
    tz: str | None = None,
    excursion_unit: str = "currency",
    default_stop_points: float | None = None,
    sample_size: int = 8,
) -> Preview:
    """Parse a file and describe it, without storing anything."""
    path = Path(path)
    kind, label = detect_kind(path)
    columns, _ = _header(path)

    if kind == BAR_KIND:
        try:
            bars = load_bars(path, tz=tz or "America/New_York")
        except DataFormatError as exc:
            raise StatementError(str(exc)) from exc
        return Preview(
            kind=BAR_KIND,
            format_label=label,
            columns=columns,
            row_count=len(bars),
            period={
                "first": bars[0].ts.isoformat(),
                "last": bars[-1].ts.isoformat(),
            },
            summary=summarize(bars),
            notes=[
                "Bar data is market history, not a statement -- it is used for "
                "backtesting rather than added to your journal."
            ],
        )

    if kind == UNKNOWN_KIND:
        raise StatementError(
            f"could not tell what this file is. Its first row reads {columns[:8]}. "
            "A trade list needs at least an entry time, a direction and an entry "
            "price."
        )

    trades, warnings = parse_trades(
        path, symbol, tz, excursion_unit, default_stop_points
    )
    if not trades:
        raise StatementError(
            "no readable trades in the file"
            + (f" (first problems: {warnings[:3]})" if warnings else "")
        )

    closed = [trade for trade in trades if trade.is_closed]
    with_stops = [trade for trade in trades if trade.stop_price is not None]
    notes: list[str] = []
    if len(with_stops) < len(trades):
        notes.append(
            f"{len(trades) - len(with_stops)} of {len(trades)} trades record no "
            "stop, so they will have no R-multiple. Set a default stop distance "
            "to fill them in, or add stops per trade later."
        )
    if len(closed) < len(trades):
        notes.append(f"{len(trades) - len(closed)} trades are still open.")

    ordered = sorted(trades, key=lambda trade: trade.entry_time)
    return Preview(
        kind=TRADE_KIND,
        format_label=label,
        columns=columns,
        row_count=len(trades),
        warnings=warnings,
        sample=[_trade_row(trade) for trade in ordered[:sample_size]],
        period={
            "first": ordered[0].entry_time.isoformat(),
            "last": ordered[-1].entry_time.isoformat(),
            "days": len(
                {DEFAULT_SESSION.session_day(trade.entry_time) for trade in trades}
            ),
        },
        symbols=dict(Counter(trade.symbol for trade in trades)),
        notes=notes,
        summary=(
            f"{len(trades)} trades, {len(closed)} closed, "
            f"{len(with_stops)} with a recorded stop"
        ),
    )
