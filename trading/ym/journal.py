"""The trade journal: a SQLite store for trades you actually took.

Why SQLite and not a spreadsheet: the journal is the input to the behavioral
analysis in :mod:`ym.behavior`, which needs to ask questions like "what is my
expectancy on the fourth trade of the day, within 20 minutes of a loss". That
wants a queryable store, and sqlite3 ships with Python.

Two fields do the heavy lifting and are easy to skip -- don't:

* ``stop_price`` -- without the initial stop there is no 1R, and half the
  analysis (expectancy in R, whether you honor your stops, whether you cut
  winners short) cannot be computed.
* ``planned`` -- whether the trade was the setup you intended or something you
  talked yourself into. This is the single most useful column in the database.
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from .core import Direction, ExitReason, Trade
from .instruments import get_instrument
from .metrics import Metrics, compute_metrics, format_breakdown
from .sessions import DEFAULT_SESSION, SessionSpec, exchange_tz

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,
    direction    TEXT    NOT NULL,
    entry_time   TEXT    NOT NULL,
    entry_price  REAL    NOT NULL,
    contracts    INTEGER NOT NULL,
    point_value  REAL    NOT NULL,
    stop_price   REAL,
    target_price REAL,
    exit_time    TEXT,
    exit_price   REAL,
    commission   REAL    NOT NULL DEFAULT 0,
    mae_points   REAL    NOT NULL DEFAULT 0,
    mfe_points   REAL    NOT NULL DEFAULT 0,
    exit_reason  TEXT,
    setup        TEXT    NOT NULL DEFAULT '',
    tags         TEXT    NOT NULL DEFAULT '',
    notes        TEXT    NOT NULL DEFAULT '',
    session_day  TEXT,
    planned      INTEGER NOT NULL DEFAULT 1,
    source       TEXT    NOT NULL DEFAULT 'manual',
    created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_entry ON trades(entry_time);
CREATE INDEX IF NOT EXISTS idx_trades_day   ON trades(session_day);
CREATE INDEX IF NOT EXISTS idx_trades_setup ON trades(setup);
"""


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _parse_dt(value: str | None, tz) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


class Journal:
    """A trade journal backed by a SQLite file.

    Use as a context manager so the connection is always closed::

        with Journal("trades.db") as journal:
            journal.record(trade)
    """

    def __init__(
        self,
        path: str | Path = "journal.db",
        session: SessionSpec = DEFAULT_SESSION,
    ) -> None:
        self.path = str(path)
        self.session = session
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(_SCHEMA)
        self.connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.connection.commit()

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    # -- writing -----------------------------------------------------------

    def record(
        self, trade: Trade, planned: bool = True, source: str = "manual"
    ) -> int:
        """Insert a trade and return its journal id."""
        row = (
            trade.symbol,
            trade.direction.value,
            _iso(trade.entry_time),
            trade.entry_price,
            trade.contracts,
            trade.point_value,
            trade.stop_price,
            trade.target_price,
            _iso(trade.exit_time),
            trade.exit_price,
            trade.commission,
            trade.mae_points,
            trade.mfe_points,
            trade.exit_reason.value if trade.exit_reason else None,
            trade.setup,
            ",".join(trade.tags),
            trade.notes,
            str(self.session.session_day(trade.entry_time)),
            1 if planned else 0,
            source,
            datetime.now(self.session.tz).isoformat(),
        )
        cursor = self.connection.execute(
            """
            INSERT INTO trades (
                symbol, direction, entry_time, entry_price, contracts, point_value,
                stop_price, target_price, exit_time, exit_price, commission,
                mae_points, mfe_points, exit_reason, setup, tags, notes,
                session_day, planned, source, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            row,
        )
        self.connection.commit()
        trade.trade_id = cursor.lastrowid
        return cursor.lastrowid

    def record_many(
        self, trades: Iterable[Trade], source: str = "import"
    ) -> int:
        count = 0
        for trade in trades:
            self.record(trade, source=source)
            count += 1
        return count

    def update(self, trade_id: int, **fields) -> None:
        """Patch columns on one trade, e.g. ``update(7, stop_price=40950)``."""
        allowed = {
            "stop_price", "target_price", "exit_time", "exit_price", "commission",
            "mae_points", "mfe_points", "exit_reason", "setup", "tags", "notes",
            "planned", "contracts",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise KeyError(f"cannot update {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [
            _iso(value) if isinstance(value, datetime) else value
            for value in fields.values()
        ]
        self.connection.execute(
            f"UPDATE trades SET {assignments} WHERE id = ?", (*values, trade_id)
        )
        self.connection.commit()

    def delete(self, trade_id: int) -> None:
        self.connection.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
        self.connection.commit()

    # -- reading -----------------------------------------------------------

    def _to_trade(self, row: sqlite3.Row) -> Trade:
        tz = self.session.tz
        trade = Trade(
            symbol=row["symbol"],
            direction=Direction(row["direction"]),
            entry_time=_parse_dt(row["entry_time"], tz),
            entry_price=row["entry_price"],
            contracts=row["contracts"],
            point_value=row["point_value"],
            stop_price=row["stop_price"],
            target_price=row["target_price"],
            exit_time=_parse_dt(row["exit_time"], tz),
            exit_price=row["exit_price"],
            commission=row["commission"],
            mae_points=row["mae_points"],
            mfe_points=row["mfe_points"],
            exit_reason=ExitReason(row["exit_reason"]) if row["exit_reason"] else None,
            setup=row["setup"],
            tags=[tag for tag in row["tags"].split(",") if tag],
            notes=row["notes"],
            trade_id=row["id"],
        )
        if not row["planned"]:
            trade.tags.append("unplanned")
        return trade

    def trades(
        self,
        start: date | str | None = None,
        end: date | str | None = None,
        symbol: str | None = None,
        setup: str | None = None,
        planned: bool | None = None,
        closed_only: bool = True,
        limit: int | None = None,
        newest_first: bool = False,
    ) -> list[Trade]:
        """Trades matching the filters, oldest first.

        With ``newest_first`` and a ``limit``, the most recent ``limit`` trades
        are selected -- still returned oldest first, which is what you want for
        both reading and any sequential analysis.
        """
        clauses, params = [], []
        if start:
            clauses.append("session_day >= ?")
            params.append(str(start))
        if end:
            clauses.append("session_day <= ?")
            params.append(str(end))
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        if setup:
            clauses.append("setup LIKE ?")
            params.append(f"%{setup}%")
        if planned is not None:
            clauses.append("planned = ?")
            params.append(1 if planned else 0)
        if closed_only:
            clauses.append("exit_price IS NOT NULL")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "DESC" if (newest_first and limit) else "ASC"
        sql = f"SELECT * FROM trades{where} ORDER BY entry_time {order}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = self.connection.execute(sql, params).fetchall()
        trades = [self._to_trade(row) for row in rows]
        return list(reversed(trades)) if order == "DESC" else trades

    def open_trades(self) -> list[Trade]:
        rows = self.connection.execute(
            "SELECT * FROM trades WHERE exit_price IS NULL ORDER BY entry_time"
        ).fetchall()
        return [self._to_trade(row) for row in rows]

    def count(self) -> int:
        return self.connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

    def planned_ratio(self) -> float | None:
        """Share of trades marked as planned. Below ~0.9 is worth a look."""
        row = self.connection.execute(
            "SELECT AVG(planned) FROM trades"
        ).fetchone()
        return None if row[0] is None else float(row[0])

    # -- analysis ----------------------------------------------------------

    def metrics(self, starting_equity: float = 0.0, **filters) -> Metrics:
        return compute_metrics(self.trades(**filters), starting_equity, self.session)

    def breakdown(self, by: str, **filters) -> str:
        return format_breakdown(self.trades(**filters), by, self.session)

    # -- import / export ---------------------------------------------------

    def import_csv(
        self,
        path: str | Path,
        symbol: str | None = None,
        tz: str | None = None,
        excursion_unit: str = "currency",
        default_stop_points: float | None = None,
        source: str = "csv",
    ) -> tuple[int, list[str]]:
        """Import trades from a CSV export.

        Understands NinjaTrader 8's Trade Performance grid export (``Entry
        time``, ``Market pos.``, ``MAE``, ...) as well as the backtester's own
        trade CSV. Returns ``(imported, warnings)``.

        NinjaTrader does not export your stop, so R-multiples are unavailable
        for imported trades unless you pass ``default_stop_points`` or fill in
        ``stop_price`` afterwards.
        """
        path = Path(path)
        zone = exchange_tz(tz) if tz else self.session.tz
        warnings: list[str] = []
        imported = 0

        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            sample = handle.read(4096)
            handle.seek(0)
            delimiter = ";" if sample.count(";") > sample.count(",") else ","
            reader = csv.DictReader(handle, delimiter=delimiter)
            if not reader.fieldnames:
                raise ValueError(f"{path.name}: no header row found")
            lookup = {
                (name or "").strip().lower().rstrip("."): name
                for name in reader.fieldnames
            }

            def pick(*candidates: str) -> str | None:
                for candidate in candidates:
                    key = candidate.strip().lower().rstrip(".")
                    if key in lookup:
                        return lookup[key]
                return None

            col_entry_time = pick("entry time", "entry_time", "datetime", "date")
            col_exit_time = pick("exit time", "exit_time")
            col_direction = pick("market pos", "market position", "direction", "side")
            col_quantity = pick("quantity", "contracts", "qty", "size")
            col_entry = pick("entry price", "entry_price")
            col_exit = pick("exit price", "exit_price")
            col_symbol = pick("instrument", "symbol")
            col_commission = pick("commission", "commissions", "fees")
            col_mae = pick("mae", "mae_points")
            col_mfe = pick("mfe", "mfe_points")
            col_stop = pick("stop_price", "stop", "stop price")
            col_target = pick("target_price", "target", "target price")
            col_setup = pick("setup", "strategy")
            col_setup_alt = pick("entry name", "signal")
            col_reason = pick("exit_reason", "exit name", "exit reason")
            col_notes = pick("notes", "comment")

            missing = [
                label
                for label, column in (
                    ("entry time", col_entry_time),
                    ("direction", col_direction),
                    ("entry price", col_entry),
                )
                if column is None
            ]
            if missing:
                raise ValueError(
                    f"{path.name}: missing required column(s) {missing}. "
                    f"Found: {reader.fieldnames}"
                )

            def number(row: dict, column: str | None) -> float | None:
                if not column:
                    return None
                raw = (row.get(column) or "").strip()
                if not raw:
                    return None
                cleaned = (
                    raw.replace("$", "").replace(",", "")
                    .replace("(", "-").replace(")", "")
                )
                try:
                    return float(cleaned)
                except ValueError:
                    return None

            def timestamp(raw: str | None) -> datetime | None:
                if not raw:
                    return None
                text = raw.strip()
                for fmt in (
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y %H:%M:%S",
                    "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M", "%Y%m%d %H%M%S",
                ):
                    try:
                        return datetime.strptime(text, fmt).replace(tzinfo=zone)
                    except ValueError:
                        continue
                try:
                    parsed = datetime.fromisoformat(text)
                    return parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)
                except ValueError:
                    return None

            for line_number, row in enumerate(reader, start=2):
                entry_time = timestamp(row.get(col_entry_time))
                if entry_time is None:
                    warnings.append(f"line {line_number}: unreadable entry time")
                    continue
                try:
                    direction = Direction.parse(row[col_direction])
                except ValueError:
                    warnings.append(f"line {line_number}: unreadable direction")
                    continue
                entry_price = number(row, col_entry)
                if entry_price is None:
                    warnings.append(f"line {line_number}: unreadable entry price")
                    continue

                raw_symbol = (row.get(col_symbol) or symbol or "YM").strip()
                try:
                    instrument = get_instrument(raw_symbol)
                except KeyError:
                    instrument = get_instrument(symbol or "YM")
                    warnings.append(
                        f"line {line_number}: unknown instrument {raw_symbol!r}, "
                        f"treated as {instrument.symbol}"
                    )

                contracts = int(number(row, col_quantity) or 1)
                stop_price = number(row, col_stop)
                if stop_price is None and default_stop_points:
                    stop_price = entry_price - direction.sign * abs(default_stop_points)

                mae = abs(number(row, col_mae) or 0.0)
                mfe = abs(number(row, col_mfe) or 0.0)
                if excursion_unit == "currency" and contracts > 0:
                    divisor = instrument.point_value * contracts
                    mae, mfe = mae / divisor, mfe / divisor

                reason_text = (row.get(col_reason) or "").strip().lower()
                try:
                    exit_reason = ExitReason(reason_text) if reason_text else None
                except ValueError:
                    exit_reason = ExitReason.MANUAL

                trade = Trade(
                    symbol=instrument.symbol,
                    direction=direction,
                    entry_time=entry_time,
                    entry_price=entry_price,
                    contracts=contracts,
                    point_value=instrument.point_value,
                    stop_price=stop_price,
                    target_price=number(row, col_target),
                    exit_time=timestamp(row.get(col_exit_time)) if col_exit_time else None,
                    exit_price=number(row, col_exit),
                    commission=abs(number(row, col_commission) or 0.0),
                    mae_points=mae,
                    mfe_points=mfe,
                    exit_reason=exit_reason,
                    setup=(
                        (row.get(col_setup) or "").strip()
                        or (row.get(col_setup_alt) or "").strip()
                    ),
                    notes=(row.get(col_notes) or "").strip(),
                )
                self.record(trade, source=source)
                imported += 1

        return imported, warnings

    def export_csv(self, path: str | Path, **filters) -> int:
        trades = self.trades(**filters)
        fields = [
            "id", "session_day", "symbol", "setup", "direction", "entry_time",
            "entry_price", "exit_time", "exit_price", "contracts", "stop_price",
            "target_price", "net_pnl", "r_multiple", "mae_r", "mfe_r",
            "exit_reason", "tags", "notes",
        ]
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(fields)
            for trade in trades:
                writer.writerow([
                    trade.trade_id,
                    self.session.session_day(trade.entry_time),
                    trade.symbol, trade.setup, trade.direction.value,
                    trade.entry_time.isoformat(), trade.entry_price,
                    trade.exit_time.isoformat() if trade.exit_time else "",
                    trade.exit_price, trade.contracts, trade.stop_price,
                    trade.target_price, round(trade.net_pnl, 2),
                    None if trade.r_multiple is None else round(trade.r_multiple, 3),
                    None if trade.mae_r is None else round(trade.mae_r, 3),
                    None if trade.mfe_r is None else round(trade.mfe_r, 3),
                    trade.exit_reason.value if trade.exit_reason else "",
                    ",".join(trade.tags), trade.notes,
                ])
        return len(trades)
