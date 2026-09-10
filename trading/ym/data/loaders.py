"""Loading OHLCV bars, with first-class support for NinjaTrader exports.

NinjaTrader 8 writes historical data as semicolon-delimited text::

    20260908 093000;41012;41045;41003;41038;2481

but chart and grid exports are comma-delimited with headers, and the date
format follows your Windows locale. Rather than make you normalize files by
hand, :func:`load_bars` sniffs the delimiter, the header and the timestamp
format, and tells you what it decided.

Timezones are the one thing it cannot sniff. NinjaTrader exports in whatever
timezone the platform is configured for, so pass ``tz=`` if that is not
US Eastern. Getting this wrong silently shifts every session boundary.
"""

from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..core import Bar
from ..sessions import EXCHANGE_TZ_NAME, SessionSpec, exchange_tz

DELIMITERS = (";", ",", "\t", "|")

TIMESTAMP_FORMATS = (
    "%Y%m%d %H%M%S",
    "%Y%m%d %H%M",
    "%Y%m%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y",
)

DAYFIRST_FORMATS = (
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
)

_COLUMN_ALIASES = {
    "datetime": {"datetime", "date time", "timestamp", "time stamp", "bar time"},
    "date": {"date", "day"},
    "time": {"time"},
    "open": {"open", "o", "open price"},
    "high": {"high", "h", "high price"},
    "low": {"low", "l", "low price"},
    "close": {"close", "c", "last", "close price", "settle"},
    "volume": {"volume", "vol", "v", "total volume"},
}


class DataFormatError(ValueError):
    """Raised when a file cannot be understood as OHLCV bars."""


@dataclass
class FileDialect:
    """What the sniffer concluded about a file. Print it to check yourself."""

    delimiter: str
    has_header: bool
    columns: dict[str, int]
    timestamp_format: str | None
    sample_row: list[str]

    def describe(self) -> str:
        delim = {";": "semicolon", ",": "comma", "\t": "tab", "|": "pipe"}.get(
            self.delimiter, repr(self.delimiter)
        )
        order = ", ".join(
            name for name, _ in sorted(self.columns.items(), key=lambda kv: kv[1])
        )
        return (
            f"delimiter={delim}  header={'yes' if self.has_header else 'no'}\n"
            f"columns={order}\n"
            f"timestamp_format={self.timestamp_format!r}\n"
            f"sample={self.sample_row}"
        )


def _is_number(text: str) -> bool:
    try:
        float(text.replace(",", "").strip())
        return True
    except (TypeError, ValueError):
        return False


def _to_float(text: str) -> float:
    return float(text.replace(",", "").strip())


def _sniff_delimiter(lines: list[str]) -> str:
    best, best_count = ",", 0
    for delim in DELIMITERS:
        counts = [line.count(delim) for line in lines if line.strip()]
        if not counts:
            continue
        # A real delimiter appears the same number of times on every row.
        if min(counts) >= 3 and len(set(counts)) == 1 and min(counts) > best_count:
            best, best_count = delim, min(counts)
    if best_count == 0:  # fall back to whichever appears most often
        for delim in DELIMITERS:
            count = sum(line.count(delim) for line in lines)
            if count > best_count:
                best, best_count = delim, count
    return best


def _match_column(name: str) -> str | None:
    key = name.strip().strip('"').lower()
    for canonical, aliases in _COLUMN_ALIASES.items():
        if key in aliases:
            return canonical
    return None


def _sniff_columns(row: list[str]) -> tuple[bool, dict[str, int]]:
    """Return ``(has_header, column_map)`` for the first row of a file."""
    named = {}
    for index, cell in enumerate(row):
        canonical = _match_column(cell)
        if canonical is not None and canonical not in named:
            named[canonical] = index
    has_header = len(named) >= 3 and not _is_number(row[0])
    if has_header:
        return True, named

    # Headerless: NinjaTrader's own export order.
    width = len(row)
    if width == 5:  # datetime;o;h;l;c
        return False, {"datetime": 0, "open": 1, "high": 2, "low": 3, "close": 4}
    if width == 6:  # datetime;o;h;l;c;v
        return False, {
            "datetime": 0, "open": 1, "high": 2, "low": 3, "close": 4, "volume": 5
        }
    if width >= 7:  # date;time;o;h;l;c;v
        return False, {
            "date": 0, "time": 1, "open": 2, "high": 3,
            "low": 4, "close": 5, "volume": 6,
        }
    raise DataFormatError(
        f"cannot infer columns from a {width}-field row: {row!r}. "
        "Add a header row, or pass columns= explicitly."
    )


def parse_timestamp(
    text: str, formats: tuple[str, ...] = TIMESTAMP_FORMATS
) -> tuple[datetime, str]:
    """Parse a naive timestamp, returning it with the format that worked."""
    raw = text.strip().strip('"')
    # NinjaTrader tick exports append fractional seconds after a space.
    parts = raw.split(" ")
    if len(parts) == 3 and parts[2].isdigit():
        raw = f"{parts[0]} {parts[1]}"
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt), fmt
        except ValueError:
            continue
    try:  # last resort: ISO 8601 with offsets, fractional seconds, etc.
        return datetime.fromisoformat(raw), "isoformat"
    except ValueError:
        pass
    raise DataFormatError(
        f"unrecognized timestamp {text!r}. Pass timestamp_format= with a "
        "strptime pattern, e.g. '%d/%m/%Y %H:%M:%S'."
    )


def sniff(path: str | os.PathLike, max_lines: int = 25) -> FileDialect:
    """Inspect a file without loading it. Useful before a long import."""
    lines: list[str] = []
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            if line.strip():
                lines.append(line.rstrip("\r\n"))
            if len(lines) >= max_lines:
                break
    if not lines:
        raise DataFormatError(f"{path} is empty")

    delimiter = _sniff_delimiter(lines)
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
    has_header, columns = _sniff_columns(rows[0])
    data_row = rows[1] if has_header and len(rows) > 1 else rows[0]

    fmt = None
    if data_row:
        if "datetime" in columns:
            _, fmt = parse_timestamp(data_row[columns["datetime"]])
        elif "date" in columns:
            date_text = data_row[columns["date"]]
            time_text = data_row[columns["time"]] if "time" in columns else ""
            _, fmt = parse_timestamp(f"{date_text} {time_text}".strip())
    return FileDialect(delimiter, has_header, columns, fmt, data_row)


def load_bars(
    path: str | os.PathLike,
    tz: str = EXCHANGE_TZ_NAME,
    to_tz: str | None = EXCHANGE_TZ_NAME,
    timestamp_format: str | None = None,
    dayfirst: bool = False,
    columns: dict[str, int] | None = None,
    delimiter: str | None = None,
    has_header: bool | None = None,
    skip_bad_rows: bool = True,
    limit: int | None = None,
) -> list[Bar]:
    """Read OHLCV bars from a delimited text file.

    ``tz`` is the timezone the file's timestamps are *written in* -- for a
    NinjaTrader export that is the platform's configured timezone, not
    necessarily yours. ``to_tz`` is what they are converted to; leave it at
    US Eastern so session logic lines up with the exchange.
    """
    path = Path(path)
    dialect = sniff(path)
    delimiter = delimiter or dialect.delimiter
    has_header = dialect.has_header if has_header is None else has_header
    columns = columns or dialect.columns
    source_tz = exchange_tz(tz)
    target_tz = exchange_tz(to_tz) if to_tz else None

    formats: tuple[str, ...]
    if timestamp_format:
        formats = (timestamp_format,)
    elif dayfirst:
        formats = DAYFIRST_FORMATS + TIMESTAMP_FORMATS
    else:
        formats = TIMESTAMP_FORMATS

    required = ("open", "high", "low", "close")
    missing = [name for name in required if name not in columns]
    if missing:
        raise DataFormatError(f"{path.name}: missing column(s) {missing}")
    if "datetime" not in columns and "date" not in columns:
        raise DataFormatError(f"{path.name}: no date or datetime column found")

    bars: list[Bar] = []
    errors: list[str] = []
    cached_format: str | None = timestamp_format

    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        for line_number, row in enumerate(reader, start=1):
            if not row or not any(cell.strip() for cell in row):
                continue
            if has_header and line_number == 1:
                continue
            try:
                if "datetime" in columns:
                    stamp_text = row[columns["datetime"]]
                else:
                    stamp_text = row[columns["date"]]
                    if "time" in columns:
                        stamp_text = f"{stamp_text} {row[columns['time']]}".strip()

                if cached_format:
                    try:
                        naive, _ = parse_timestamp(stamp_text, (cached_format,))
                    except DataFormatError:
                        naive, cached_format = parse_timestamp(stamp_text, formats)
                else:
                    naive, cached_format = parse_timestamp(stamp_text, formats)

                ts = naive if naive.tzinfo else naive.replace(tzinfo=source_tz)
                if target_tz is not None:
                    ts = ts.astimezone(target_tz)

                bar = Bar(
                    ts=ts,
                    open=_to_float(row[columns["open"]]),
                    high=_to_float(row[columns["high"]]),
                    low=_to_float(row[columns["low"]]),
                    close=_to_float(row[columns["close"]]),
                    volume=(
                        _to_float(row[columns["volume"]])
                        if "volume" in columns and columns["volume"] < len(row)
                        and row[columns["volume"]].strip()
                        else 0.0
                    ),
                )
            except (ValueError, IndexError, DataFormatError) as exc:
                errors.append(f"line {line_number}: {exc}")
                if skip_bad_rows:
                    continue
                raise DataFormatError(f"{path.name} line {line_number}: {exc}") from exc
            bars.append(bar)
            if limit is not None and len(bars) >= limit:
                break

    if not bars:
        detail = f" First errors: {errors[:3]}" if errors else ""
        raise DataFormatError(f"{path.name}: no usable rows.{detail}")
    # A genuine format misread produces errors in bulk. A handful of bad lines
    # in a large file is just a broken export, so only the bulk case is fatal.
    if len(errors) >= 5 and len(errors) > len(bars) * 0.1:
        raise DataFormatError(
            f"{path.name}: {len(errors)} bad rows vs {len(bars)} good ones -- "
            f"the format is probably being misread. First errors: {errors[:3]}"
        )

    bars.sort(key=lambda bar: bar.ts)
    return dedupe(bars)


def dedupe(bars: list[Bar]) -> list[Bar]:
    """Drop repeated timestamps, keeping the last occurrence of each."""
    out: list[Bar] = []
    for bar in bars:
        if out and bar.ts == out[-1].ts:
            out[-1] = bar
        else:
            out.append(bar)
    return out


def write_csv(bars: list[Bar], path: str | os.PathLike) -> None:
    """Write bars back out in a plain, unambiguous ISO/comma format."""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["datetime", "open", "high", "low", "close", "volume"])
        for bar in bars:
            writer.writerow(
                [bar.ts.isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume]
            )


def resample(bars: list[Bar], minutes: int) -> list[Bar]:
    """Aggregate bars into a coarser timeframe, anchored to the hour."""
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    out: list[Bar] = []
    bucket_start: datetime | None = None
    o = h = l = c = 0.0
    volume = 0.0
    span = timedelta(minutes=minutes)

    for bar in bars:
        anchor = bar.ts.replace(minute=0, second=0, microsecond=0)
        offset = (bar.ts - anchor) // span
        start = anchor + offset * span
        if bucket_start is None or start != bucket_start:
            if bucket_start is not None:
                out.append(Bar(bucket_start, o, h, l, c, volume))
            bucket_start, o, h, l, c, volume = start, bar.open, bar.high, bar.low, bar.close, bar.volume
        else:
            h = max(h, bar.high)
            l = min(l, bar.low)
            c = bar.close
            volume += bar.volume
    if bucket_start is not None:
        out.append(Bar(bucket_start, o, h, l, c, volume))
    return out


def filter_session(
    bars: list[Bar], keep: str = "rth", session: SessionSpec | None = None
) -> list[Bar]:
    """Keep only bars in a given session (``rth``, ``overnight``, ``open``...)."""
    spec = session or SessionSpec()
    keep = keep.lower()
    if keep == "all":
        return list(bars)
    if keep == "open":
        return [bar for bar in bars if spec.is_open(bar.ts)]
    return [bar for bar in bars if spec.session_of(bar.ts).value == keep]


def summarize(bars: list[Bar]) -> str:
    """A quick sanity report: coverage, spacing, gaps."""
    if not bars:
        return "no bars"
    spacings: dict[float, int] = {}
    for previous, current in zip(bars, bars[1:]):
        gap = (current.ts - previous.ts).total_seconds() / 60.0
        spacings[gap] = spacings.get(gap, 0) + 1
    common = sorted(spacings.items(), key=lambda kv: -kv[1])[:3]
    lows = min(bar.low for bar in bars)
    highs = max(bar.high for bar in bars)
    return "\n".join(
        [
            f"bars       {len(bars):,}",
            f"first      {bars[0].ts.isoformat()}",
            f"last       {bars[-1].ts.isoformat()}",
            f"price      {lows:,.0f} - {highs:,.0f}",
            "spacing    "
            + ", ".join(f"{gap:g}min x{count:,}" for gap, count in common),
        ]
    )
