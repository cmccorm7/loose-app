"""Persistent state the hub owns: settings and the statement manifest.

Both are plain JSON on disk. A desktop app's state should be something you can
open in a text editor and understand -- and, when something goes wrong, fix.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DEFAULT_SETTINGS, Paths


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class StatementRecord:
    """One uploaded file and what became of it."""

    id: str
    filename: str
    stored_as: str
    kind: str                      # what the detector made of it
    uploaded_at: str
    size_bytes: int
    status: str = "pending"        # pending -> imported | rejected | failed
    rows_detected: int = 0
    rows_imported: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    imported_at: str = ""
    options: dict = field(default_factory=dict)
    period: dict = field(default_factory=dict)   # first/last trade seen

    @property
    def source_tag(self) -> str:
        """The journal ``source`` value for trades from this upload.

        Tagging every row with its upload is what makes an import reversible.
        """
        return f"statement:{self.id}"

    def to_dict(self) -> dict:
        return asdict(self)


class Store:
    """Reads and writes the manifest and settings. Safe across threads."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self._lock = threading.Lock()

    # -- settings ----------------------------------------------------------

    def settings(self) -> dict:
        data = dict(DEFAULT_SETTINGS)
        if self.paths.settings.exists():
            try:
                data.update(json.loads(self.paths.settings.read_text()))
            except (json.JSONDecodeError, OSError):
                pass  # a corrupt settings file should not stop the app booting
        return data

    def update_settings(self, changes: dict) -> dict:
        with self._lock:
            current = self.settings()
            unknown = set(changes) - set(DEFAULT_SETTINGS)
            if unknown:
                raise KeyError(f"unknown setting(s): {sorted(unknown)}")
            current.update(changes)
            self._write(self.paths.settings, current)
            return current

    # -- statements --------------------------------------------------------

    def statements(self) -> list[StatementRecord]:
        if not self.paths.manifest.exists():
            return []
        try:
            raw = json.loads(self.paths.manifest.read_text())
        except (json.JSONDecodeError, OSError):
            return []
        records = []
        for item in raw:
            known = {k: v for k, v in item.items() if k in StatementRecord.__annotations__}
            records.append(StatementRecord(**known))
        return records

    def statement(self, statement_id: str) -> StatementRecord | None:
        for record in self.statements():
            if record.id == statement_id:
                return record
        return None

    def save_statement(self, record: StatementRecord) -> StatementRecord:
        with self._lock:
            records = self.statements()
            for index, existing in enumerate(records):
                if existing.id == record.id:
                    records[index] = record
                    break
            else:
                records.append(record)
            records.sort(key=lambda item: item.uploaded_at, reverse=True)
            self._write(self.paths.manifest, [item.to_dict() for item in records])
            return record

    def remove_statement(self, statement_id: str) -> StatementRecord | None:
        with self._lock:
            records = self.statements()
            removed = None
            kept = []
            for record in records:
                if record.id == statement_id:
                    removed = record
                else:
                    kept.append(record)
            if removed is not None:
                self._write(self.paths.manifest, [item.to_dict() for item in kept])
            return removed

    # -- disk --------------------------------------------------------------

    @staticmethod
    def _write(path: Path, payload: Any) -> None:
        """Write via a temp file so a crash mid-write cannot truncate state."""
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, default=str))
        temporary.replace(path)
