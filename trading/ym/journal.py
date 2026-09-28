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

import bisect
import csv
import sqlite3
from datetime import date, datetime, timedelta
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
        """Parse a CSV export and record every trade it yields.

        Returns ``(imported, warnings)``. To see what a file contains *before*
        writing any of it, call :func:`parse_trade_csv` directly -- a statement
        that turns out to be misread is much easier to reject than to unpick
        from the journal afterwards.
        """
        trades, warnings = parse_trade_csv(
            path,
            symbol=symbol,
            tz=tz,
            excursion_unit=excursion_unit,
            default_stop_points=default_stop_points,
            session=self.session,
        )
        for trade in trades:
            self.record(trade, source=source)
        return len(trades), warnings

    def delete_by_source(self, source: str) -> int:
        """Remove every trade that came from one import. Returns the count."""
        cursor = self.connection.execute(
            "DELETE FROM trades WHERE source = ?", (source,)
        )
        self.connection.commit()
        return cursor.rowcount

    def sources(self) -> dict[str, int]:
        """How many trades came from each source."""
        rows = self.connection.execute(
            "SELECT source, COUNT(*) FROM trades GROUP BY source ORDER BY source"
        ).fetchall()
        return {row[0]: row[1] for row in rows}

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


def parse_trade_csv(
    path: str | Path,
    symbol: str | None = None,
    tz: str | None = None,
    excursion_unit: str = "currency",
    default_stop_points: float | None = None,
    session: SessionSpec = DEFAULT_SESSION,
) -> tuple[list[Trade], list[str]]:
    """Read trades out of a CSV export without storing anything.

    Understands NinjaTrader 8's Trade Performance grid export (``Entry time``,
    ``Market pos.``, ``MAE``, ...) as well as the backtester's own trade CSV.
    Returns ``(trades, warnings)``; rows it cannot read become warnings rather
    than exceptions, so one bad line does not cost you the file.

    NinjaTrader does not export your stop, so R-multiples are unavailable for
    imported trades unless you pass ``default_stop_points`` or fill in
    ``stop_price`` afterwards.
    """
    path = Path(path)
    zone = exchange_tz(tz) if tz else session.tz
    warnings: list[str] = []
    trades: list[Trade] = []

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

        columns = {
            "entry_time": pick("entry time", "entry_time", "datetime", "date"),
            "exit_time": pick("exit time", "exit_time"),
            "direction": pick("market pos", "market position", "direction", "side"),
            "quantity": pick("quantity", "contracts", "qty", "size"),
            "entry": pick("entry price", "entry_price"),
            "exit": pick("exit price", "exit_price"),
            "symbol": pick("instrument", "symbol"),
            "commission": pick("commission", "commissions", "fees"),
            "mae": pick("mae", "mae_points"),
            "mfe": pick("mfe", "mfe_points"),
            "stop": pick("stop_price", "stop", "stop price"),
            "target": pick("target_price", "target", "target price"),
            "setup": pick("setup", "strategy"),
            "setup_alt": pick("entry name", "signal"),
            "reason": pick("exit_reason", "exit name", "exit reason"),
            "notes": pick("notes", "comment"),
        }

        missing = [
            label
            for label, key in (
                ("entry time", "entry_time"),
                ("direction", "direction"),
                ("entry price", "entry"),
            )
            if columns[key] is None
        ]
        if missing:
            raise ValueError(
                f"{path.name}: missing required column(s) {missing}. "
                f"Found: {reader.fieldnames}"
            )

        def number(row: dict, key: str) -> float | None:
            column = columns[key]
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

        def text(row: dict, key: str) -> str:
            column = columns[key]
            return (row.get(column) or "").strip() if column else ""

        def timestamp(raw: str | None) -> datetime | None:
            if not raw:
                return None
            value = raw.strip()
            for fmt in (
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y %H:%M:%S",
                "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M", "%Y%m%d %H%M%S",
            ):
                try:
                    return datetime.strptime(value, fmt).replace(tzinfo=zone)
                except ValueError:
                    continue
            try:
                parsed = datetime.fromisoformat(value)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)
            except ValueError:
                return None

        for line_number, row in enumerate(reader, start=2):
            entry_time = timestamp(text(row, "entry_time"))
            if entry_time is None:
                warnings.append(f"line {line_number}: unreadable entry time")
                continue
            try:
                direction = Direction.parse(text(row, "direction"))
            except ValueError:
                warnings.append(f"line {line_number}: unreadable direction")
                continue
            entry_price = number(row, "entry")
            if entry_price is None:
                warnings.append(f"line {line_number}: unreadable entry price")
                continue

            raw_symbol = text(row, "symbol") or symbol or "YM"
            try:
                instrument = get_instrument(raw_symbol)
            except KeyError:
                instrument = get_instrument(symbol or "YM")
                warnings.append(
                    f"line {line_number}: unknown instrument {raw_symbol!r}, "
                    f"treated as {instrument.symbol}"
                )

            contracts = int(number(row, "quantity") or 1)
            stop_price = number(row, "stop")
            if stop_price is None and default_stop_points:
                stop_price = entry_price - direction.sign * abs(default_stop_points)

            mae = abs(number(row, "mae") or 0.0)
            mfe = abs(number(row, "mfe") or 0.0)
            if excursion_unit == "currency" and contracts > 0:
                divisor = instrument.point_value * contracts
                mae, mfe = mae / divisor, mfe / divisor

            reason_text = text(row, "reason").lower()
            try:
                exit_reason = ExitReason(reason_text) if reason_text else None
            except ValueError:
                exit_reason = ExitReason.MANUAL

            trades.append(
                Trade(
                    symbol=instrument.symbol,
                    direction=direction,
                    entry_time=entry_time,
                    entry_price=entry_price,
                    contracts=contracts,
                    point_value=instrument.point_value,
                    stop_price=stop_price,
                    target_price=number(row, "target"),
                    exit_time=timestamp(text(row, "exit_time")),
                    exit_price=number(row, "exit"),
                    commission=abs(number(row, "commission") or 0.0),
                    mae_points=mae,
                    mfe_points=mfe,
                    exit_reason=exit_reason,
                    setup=text(row, "setup") or text(row, "setup_alt"),
                    notes=text(row, "notes"),
                )
            )

    return trades, warnings


# -- NinjaTrader web / mobile account exports ---------------------------------

#: Cash History rows that are per-fill trading costs. Anything else that is
#: not realized P&L or a deposit is reported, never silently charged.
FEE_CASH_TYPES = frozenset({"commission", "exchange fee", "clearing fee", "nfa fee"})
_NON_FEE_CASH_TYPES = frozenset({"trade paired", "fund transaction"})

#: How long after a fill its fee rows may be stamped. Observed: 0-1 seconds.
FEE_LAG = timedelta(seconds=2)


def _read_export(path: Path) -> tuple[list[dict], dict[str, str]]:
    """Rows plus a header lookup normalized like :func:`parse_trade_csv`."""
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
        return list(reader), lookup


def _require(path: Path, lookup: dict[str, str], labels: tuple[str, ...]) -> dict[str, str]:
    missing = [label for label in labels if label not in lookup]
    if missing:
        raise ValueError(
            f"{path.name}: missing required column(s) {missing}. "
            f"Found: {list(lookup.values())}"
        )
    return {label: lookup[label] for label in labels}


def _money(raw: str | None) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    cleaned = raw.replace("$", "").replace(",", "").replace("(", "-").replace(")", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _local_time(raw: str | None, zone) -> datetime | None:
    value = (raw or "").strip()
    for fmt in ("%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=zone)
        except ValueError:
            continue
    return None


def _is_ambiguous(local: datetime) -> bool:
    """True inside a DST fall-back hour, where a wall-clock time happens twice."""
    return local.utcoffset() != local.replace(fold=1).utcoffset()


def parse_position_history(
    path: str | Path,
    *,
    tz: str,
    cash_history: str | Path | None = None,
    default_stop_points: float | None = None,
    session: SessionSpec = DEFAULT_SESSION,
) -> tuple[list[Trade], list[str]]:
    """Read round trips out of NinjaTrader's web/mobile *Position History* export.

    That platform (the Tradovate back end) does not export NT8's Trade
    Performance grid. Its Position History has one row per broker-paired
    buy/sell fill -- ``Pair ID``, ``Buy Fill ID``, ``Sell Fill ID``,
    ``Paired Qty``, ``Buy Price``, ``Sell Price``, ``Bought Timestamp``,
    ``Sold Timestamp``, ``P/L`` -- and no direction column. Direction comes
    from which leg filled first (fill ID breaks a same-second tie).

    ``tz`` is the zone the export's wall-clock timestamps are written in -- the
    account's display zone, not the exchange's -- and is required because
    getting it wrong silently moves trades across the 18:00 ET session roll.
    Times are converted to the session's zone (ET).

    ``P/L`` in the file is gross. Pass the *Cash History* export as
    ``cash_history`` to attach actual fees: each fee row is tied to the fill
    whose ID immediately precedes its Transaction ID, and accepted only if the
    contract matches and it is stamped within :data:`FEE_LAG` of that fill. A
    fill's fees are shared across the pairs that use it by paired quantity.
    Fee rows that cannot be tied to a fill are reported, not dropped quietly.

    Like :func:`parse_trade_csv`, bad rows become warnings, and no stop is
    exported, so R-multiples need ``default_stop_points``. No MAE/MFE either.
    """
    path = Path(path)
    zone = exchange_tz(tz)
    warnings: list[str] = []

    rows, lookup = _read_export(path)
    cols = _require(path, lookup, (
        "pair id", "buy fill id", "sell fill id", "paired qty", "buy price",
        "sell price", "bought timestamp", "sold timestamp", "contract",
    ))
    pl_col = lookup.get("p/l")

    # (pair id, fill ids, qty, legs) for every readable row.
    pairs: list[dict] = []
    seen: set[str] = set()
    for line_number, row in enumerate(rows, start=2):
        pair_id = (row.get(cols["pair id"]) or "").strip()
        if pair_id in seen:
            warnings.append(f"line {line_number}: duplicate pair {pair_id}, skipped")
            continue
        buy_time = _local_time(row.get(cols["bought timestamp"]), zone)
        sell_time = _local_time(row.get(cols["sold timestamp"]), zone)
        buy_price = _money(row.get(cols["buy price"]))
        sell_price = _money(row.get(cols["sell price"]))
        qty = _money(row.get(cols["paired qty"]))
        buy_id = (row.get(cols["buy fill id"]) or "").strip()
        sell_id = (row.get(cols["sell fill id"]) or "").strip()
        if buy_time is None or sell_time is None:
            warnings.append(f"line {line_number}: unreadable timestamp")
            continue
        if buy_price is None or sell_price is None:
            warnings.append(f"line {line_number}: unreadable price")
            continue
        if not qty or qty <= 0 or qty != int(qty):
            warnings.append(f"line {line_number}: unreadable paired qty")
            continue
        if not buy_id.isdigit() or not sell_id.isdigit():
            warnings.append(f"line {line_number}: unreadable fill id")
            continue
        raw_symbol = (row.get(cols["contract"]) or "").strip()
        try:
            instrument = get_instrument(raw_symbol)
        except KeyError:
            warnings.append(f"line {line_number}: unknown instrument {raw_symbol!r}")
            continue

        buy_first = (buy_time, int(buy_id)) < (sell_time, int(sell_id))
        direction = Direction.LONG if buy_first else Direction.SHORT
        entry = (buy_time, buy_price) if buy_first else (sell_time, sell_price)
        exit_ = (sell_time, sell_price) if buy_first else (buy_time, buy_price)
        for stamp in (buy_time, sell_time):
            if _is_ambiguous(stamp):
                warnings.append(
                    f"line {line_number}: {stamp:%m/%d/%Y %H:%M:%S} falls in a DST "
                    "fall-back hour; read as the first occurrence"
                )

        contracts = int(qty)
        trade = Trade(
            symbol=instrument.symbol,
            direction=direction,
            entry_time=entry[0].astimezone(session.tz),
            entry_price=entry[1],
            contracts=contracts,
            point_value=instrument.point_value,
            exit_time=exit_[0].astimezone(session.tz),
            exit_price=exit_[1],
        )
        if default_stop_points:
            trade.stop_price = entry[1] - direction.sign * abs(default_stop_points)

        reported = _money(row.get(pl_col)) if pl_col else None
        if reported is not None and abs(reported - trade.gross_pnl) > 0.005:
            warnings.append(
                f"line {line_number}: file P/L {reported:.2f} != computed "
                f"{trade.gross_pnl:.2f}; check contract and prices"
            )

        seen.add(pair_id)
        pairs.append({
            "trade": trade, "qty": contracts, "contract": raw_symbol,
            "legs": ((buy_id, buy_time), (sell_id, sell_time)),
        })

    trades = [pair["trade"] for pair in pairs]
    if cash_history is None:
        if trades:
            warnings.append("no Cash History given: fees not attached, P&L is gross")
        return trades, warnings

    # Every fill the pairs reference: contract, time, and paired quantity.
    fills: dict[int, dict] = {}
    for pair in pairs:
        for fill_id, stamp in pair["legs"]:
            fill = fills.setdefault(int(fill_id), {
                "contract": pair["contract"], "time": stamp, "qty": 0, "fee": 0.0,
            })
            fill["qty"] += pair["qty"]
    fill_ids = sorted(fills)

    cash_path = Path(cash_history)
    cash_rows, cash_lookup = _read_export(cash_path)
    cash = _require(cash_path, cash_lookup, (
        "transaction id", "timestamp", "contract", "cash change type", "delta",
    ))
    unallocated = 0.0
    unallocated_rows = 0
    other: dict[str, float] = {}
    realized = 0.0
    for line_number, row in enumerate(cash_rows, start=2):
        kind = (row.get(cash["cash change type"]) or "").strip()
        delta = _money(row.get(cash["delta"]))
        if delta is None:
            warnings.append(f"cash line {line_number}: unreadable delta")
            continue
        if kind.lower() == "trade paired":
            realized += delta
        if kind.lower() not in FEE_CASH_TYPES:
            if kind.lower() not in _NON_FEE_CASH_TYPES:
                other[kind] = other.get(kind, 0.0) + delta
            continue
        txn = (row.get(cash["transaction id"]) or "").strip()
        stamp = _local_time(row.get(cash["timestamp"]), zone)
        contract = (row.get(cash["contract"]) or "").strip()
        index = bisect.bisect_left(fill_ids, int(txn)) - 1 if txn.isdigit() else -1
        fill = fills[fill_ids[index]] if index >= 0 else None
        if (
            fill is None or stamp is None or fill["contract"] != contract
            or not timedelta(0) <= stamp - fill["time"] <= FEE_LAG
        ):
            unallocated += delta
            unallocated_rows += 1
            continue
        fill["fee"] += -delta  # fees are negative cash deltas

    for pair in pairs:
        pair["trade"].commission = sum(
            fills[int(fill_id)]["fee"] * pair["qty"] / fills[int(fill_id)]["qty"]
            for fill_id, _ in pair["legs"]
        )

    if unallocated_rows:
        warnings.append(
            f"{unallocated_rows} fee row(s) totalling {unallocated:.2f} matched no "
            "paired fill (open position, or files cover different dates); not charged"
        )
    for kind, total in sorted(other.items()):
        warnings.append(f"cash type {kind!r} totalling {total:.2f} ignored")
    gross = sum(trade.gross_pnl for trade in trades)
    if abs(realized - gross) > 0.005:
        warnings.append(
            f"Cash History realized P&L {realized:.2f} != Position History gross "
            f"{gross:.2f}; the two files likely cover different dates"
        )
    return trades, warnings
