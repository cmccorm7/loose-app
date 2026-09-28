"""Storage for market bars, so trades can be read against what price did.

A journal says what you did. It cannot say whether the level you bought was
holding, whether you were with the trend, or what the move offered after you
left -- those need the bars. This is where they live.

Bars are stored at whatever timeframe you import (usually 1-minute) and
resampled on the way out, so one import serves every timeframe coarser than it.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

from .core import Bar
from .data.loaders import resample
from .sessions import DEFAULT_SESSION, SessionSpec

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bar_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bars (
    symbol    TEXT    NOT NULL,
    minutes   INTEGER NOT NULL,
    epoch     INTEGER NOT NULL,
    open      REAL    NOT NULL,
    high      REAL    NOT NULL,
    low       REAL    NOT NULL,
    close     REAL    NOT NULL,
    volume    REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, minutes, epoch)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_bars_lookup ON bars(symbol, minutes, epoch);
"""


class BarStore:
    """A SQLite store of OHLCV bars, keyed by symbol, timeframe and time."""

    def __init__(
        self, path: str | Path = "bars.db", session: SessionSpec = DEFAULT_SESSION
    ) -> None:
        self.path = str(path)
        self.session = session
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(_SCHEMA)
        self.connection.execute(
            "INSERT OR IGNORE INTO bar_meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.connection.commit()

    def __enter__(self) -> "BarStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    # -- writing -----------------------------------------------------------

    def add(self, symbol: str, bars: Sequence[Bar], minutes: int | None = None) -> int:
        """Store bars, replacing any already held for the same timestamps.

        ``minutes`` is inferred from the spacing when not given, so importing a
        NinjaTrader export does not require saying what timeframe it is.
        """
        if not bars:
            return 0
        symbol = symbol.upper()
        minutes = minutes or infer_timeframe(bars)
        rows = [
            (
                symbol, minutes, int(bar.ts.timestamp()),
                bar.open, bar.high, bar.low, bar.close, bar.volume,
            )
            for bar in bars
        ]
        self.connection.executemany(
            "INSERT OR REPLACE INTO bars "
            "(symbol, minutes, epoch, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        self.connection.commit()
        return len(rows)

    def delete(self, symbol: str, minutes: int | None = None) -> int:
        clauses = ["symbol = ?"]
        params: list = [symbol.upper()]
        if minutes is not None:
            clauses.append("minutes = ?")
            params.append(minutes)
        cursor = self.connection.execute(
            f"DELETE FROM bars WHERE {' AND '.join(clauses)}", params
        )
        self.connection.commit()
        return cursor.rowcount

    # -- reading -----------------------------------------------------------

    def _row_to_bar(self, row: sqlite3.Row) -> Bar:
        return Bar(
            ts=datetime.fromtimestamp(row["epoch"], tz=self.session.tz),
            open=row["open"], high=row["high"], low=row["low"],
            close=row["close"], volume=row["volume"],
        )

    def native_timeframes(self, symbol: str) -> list[int]:
        """Stored timeframes for a symbol, finest first."""
        rows = self.connection.execute(
            "SELECT DISTINCT minutes FROM bars WHERE symbol = ? ORDER BY minutes",
            (symbol.upper(),),
        ).fetchall()
        return [row[0] for row in rows]

    def _source_timeframe(self, symbol: str, minutes: int) -> int | None:
        """The finest stored timeframe that can produce ``minutes``."""
        available = self.native_timeframes(symbol)
        if minutes in available:
            return minutes
        divisors = [item for item in available if minutes % item == 0]
        return min(divisors) if divisors else None

    def bars(
        self,
        symbol: str,
        minutes: int = 1,
        start: datetime | None = None,
        end: datetime | None = None,
        pad_minutes: int = 0,
    ) -> list[Bar]:
        """Bars for a symbol and timeframe, optionally within a time window.

        ``pad_minutes`` widens the window on both sides, which is what callers
        analysing a trade want: context before the entry and after the exit.
        """
        source = self._source_timeframe(symbol, minutes)
        if source is None:
            return []

        clauses = ["symbol = ?", "minutes = ?"]
        params: list = [symbol.upper(), source]
        if start is not None:
            clauses.append("epoch >= ?")
            params.append(int((start - timedelta(minutes=pad_minutes)).timestamp()))
        if end is not None:
            clauses.append("epoch <= ?")
            params.append(int((end + timedelta(minutes=pad_minutes)).timestamp()))

        rows = self.connection.execute(
            f"SELECT * FROM bars WHERE {' AND '.join(clauses)} ORDER BY epoch",
            params,
        ).fetchall()
        found = [self._row_to_bar(row) for row in rows]
        return found if source == minutes else resample(found, minutes)

    # -- what have we got --------------------------------------------------

    def symbols(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT DISTINCT symbol FROM bars ORDER BY symbol"
        ).fetchall()
        return [row[0] for row in rows]

    def count(self, symbol: str | None = None) -> int:
        if symbol is None:
            return self.connection.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
        return self.connection.execute(
            "SELECT COUNT(*) FROM bars WHERE symbol = ?", (symbol.upper(),)
        ).fetchone()[0]

    def coverage(self, symbol: str, minutes: int | None = None) -> dict | None:
        """First bar, last bar and count -- what this store can actually answer."""
        clauses = ["symbol = ?"]
        params: list = [symbol.upper()]
        if minutes is not None:
            clauses.append("minutes = ?")
            params.append(minutes)
        row = self.connection.execute(
            f"SELECT MIN(epoch) AS first, MAX(epoch) AS last, COUNT(*) AS n "
            f"FROM bars WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
        if row is None or row["n"] == 0:
            return None
        return {
            "symbol": symbol.upper(),
            "timeframes": self.native_timeframes(symbol),
            "first": datetime.fromtimestamp(row["first"], tz=self.session.tz),
            "last": datetime.fromtimestamp(row["last"], tz=self.session.tz),
            "bars": row["n"],
        }

    def covered_days(self, symbol: str) -> set[date]:
        """Trading days with at least one bar. Used to say what can be analysed."""
        rows = self.connection.execute(
            "SELECT DISTINCT epoch FROM bars WHERE symbol = ?", (symbol.upper(),)
        ).fetchall()
        return {
            self.session.session_day(
                datetime.fromtimestamp(row["epoch"], tz=self.session.tz)
            )
            for row in rows
        }

    def covers(self, symbol: str, moment: datetime, within_minutes: int = 5) -> bool:
        """Is there a bar close to this time? Answers 'can I analyse this trade'."""
        window = int(within_minutes * 60)
        target = int(moment.timestamp())
        row = self.connection.execute(
            "SELECT 1 FROM bars WHERE symbol = ? AND epoch BETWEEN ? AND ? LIMIT 1",
            (symbol.upper(), target - window, target + window),
        ).fetchone()
        return row is not None


def infer_timeframe(bars: Sequence[Bar]) -> int:
    """The most common spacing between bars, in minutes. Falls back to 1."""
    if len(bars) < 2:
        return 1
    gaps: dict[int, int] = defaultdict(int)
    for previous, current in zip(bars, bars[1:]):
        minutes = round((current.ts - previous.ts).total_seconds() / 60)
        if minutes > 0:
            gaps[minutes] += 1
    if not gaps:
        return 1
    return max(gaps.items(), key=lambda item: item[1])[0]
