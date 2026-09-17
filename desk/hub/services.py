"""The hub's capability layer.

Every operation the app can perform is one function here, taking plain
arguments and returning JSON-able data. The HTTP API is a thin wrapper over
this class, and when the assistant is added its tools will be wrappers over the
same functions -- so the model can only do things the app itself can do, and
anything you teach the app, the model gets for free.

That is the whole reason this file exists as a layer rather than as logic
scattered through route handlers.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .config import Paths, bootstrap_engine_path
from .statements import (
    BAR_KIND, StatementError, TRADE_KIND, file_digest, preview,
)
from .store import StatementRecord, Store

bootstrap_engine_path()

from ym import behavior as behavior_module            # noqa: E402
from ym.core import Trade                             # noqa: E402
from ym.instruments import REGISTRY, get_instrument   # noqa: E402
from ym.journal import Journal, parse_trade_csv       # noqa: E402
from ym.metrics import GROUPERS, compute_metrics, daily_pnl, equity_curve, group_by  # noqa: E402
from ym.risk import RiskLimits, RiskManager           # noqa: E402
from ym.sessions import DEFAULT_SESSION               # noqa: E402

MAX_UPLOAD_BYTES = 32 * 1024 * 1024


class HubError(Exception):
    """Something the user did that the app can explain. Becomes a 400."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _trade_row(trade: Trade) -> dict:
    duration = trade.duration
    return {
        "id": trade.trade_id,
        "session_day": str(DEFAULT_SESSION.session_day(trade.entry_time)),
        "entry_time": trade.entry_time.isoformat(),
        "exit_time": trade.exit_time.isoformat() if trade.exit_time else None,
        "symbol": trade.symbol,
        "direction": trade.direction.value,
        "contracts": trade.contracts,
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "stop_price": trade.stop_price,
        "target_price": trade.target_price,
        "net_pnl": round(trade.net_pnl, 2),
        "r_multiple": None if trade.r_multiple is None else round(trade.r_multiple, 3),
        "mae_r": None if trade.mae_r is None else round(trade.mae_r, 3),
        "mfe_r": None if trade.mfe_r is None else round(trade.mfe_r, 3),
        "setup": trade.setup,
        "tags": trade.tags,
        "exit_reason": trade.exit_reason.value if trade.exit_reason else None,
        "duration_minutes": (
            None if duration is None else round(duration.total_seconds() / 60, 1)
        ),
        "notes": trade.notes,
    }


def _metrics_dict(metrics) -> dict:
    data = metrics.as_dict()
    # JSON has no infinity. An unbeaten profit factor is better said in words.
    if data.get("profit_factor") == float("inf"):
        data["profit_factor"] = None
        data["profit_factor_note"] = "no losing trades"
    return data


class Hub:
    """The application. One instance per running app."""

    def __init__(self, paths: Paths, store: Store | None = None) -> None:
        self.paths = paths
        self.store = store or Store(paths)

    # -- journal access ----------------------------------------------------

    def _journal(self) -> Journal:
        return Journal(self.paths.journal)

    def _trades(self, **filters) -> list[Trade]:
        with self._journal() as journal:
            return journal.trades(**filters)

    # -- settings ----------------------------------------------------------

    def settings(self) -> dict:
        return self.store.settings()

    def update_settings(self, changes: dict) -> dict:
        try:
            return self.store.update_settings(changes)
        except KeyError as exc:
            raise HubError(str(exc)) from exc

    def instruments(self) -> list[dict]:
        return [
            {
                "symbol": instrument.symbol,
                "name": instrument.name,
                "point_value": instrument.point_value,
                "tick_size": instrument.tick_size,
                "tick_value": instrument.tick_value,
            }
            for instrument in REGISTRY.values()
        ]

    # -- statements --------------------------------------------------------

    def list_statements(self) -> list[dict]:
        return [record.to_dict() for record in self.store.statements()]

    def upload_statement(self, filename: str, content: bytes) -> dict:
        """Store a file, work out what it is, and return a preview.

        Nothing reaches the journal here. The caller decides whether to import
        after seeing what the parser made of it.
        """
        if not content:
            raise HubError("the uploaded file is empty")
        if len(content) > MAX_UPLOAD_BYTES:
            raise HubError(
                f"file is {len(content) / 1e6:.1f} MB; the limit is "
                f"{MAX_UPLOAD_BYTES / 1e6:.0f} MB"
            )

        safe_name = Path(filename or "statement").name
        statement_id = uuid.uuid4().hex[:12]
        stored = self.paths.uploads / f"{statement_id}__{safe_name}"
        stored.write_bytes(content)

        digest = file_digest(stored)
        duplicates = [
            record for record in self.store.statements()
            if record.options.get("digest") == digest and record.status == "imported"
        ]

        record = StatementRecord(
            id=statement_id,
            filename=safe_name,
            stored_as=stored.name,
            kind="unknown",
            uploaded_at=_now(),
            size_bytes=len(content),
            options={"digest": digest},
        )

        settings = self.settings()
        try:
            found = preview(
                stored,
                symbol=settings.get("symbol"),
                tz=settings.get("timezone"),
                default_stop_points=settings.get("default_stop_points"),
            )
        except StatementError as exc:
            record.status = "failed"
            record.error = str(exc)
            self.store.save_statement(record)
            return {"statement": record.to_dict(), "preview": None, "error": str(exc)}

        record.kind = found.kind
        record.rows_detected = found.row_count
        record.warnings = found.warnings
        record.period = found.period
        self.store.save_statement(record)

        payload = {
            "statement": record.to_dict(),
            "preview": found.to_dict(),
            "error": None,
        }
        if duplicates:
            payload["preview"]["notes"] = [
                f"This file is byte-identical to {duplicates[0].filename}, "
                f"imported {duplicates[0].imported_at[:10]}. Importing it again "
                f"would double-count those trades.",
                *payload["preview"]["notes"],
            ]
        return payload

    def import_statement(
        self,
        statement_id: str,
        default_stop_points: float | None = None,
        symbol: str | None = None,
        tz: str | None = None,
        excursion_unit: str = "currency",
    ) -> dict:
        """Commit a previewed statement into the journal."""
        record = self.store.statement(statement_id)
        if record is None:
            raise HubError(f"no statement {statement_id!r}")
        if record.status == "imported":
            raise HubError(
                f"{record.filename} is already imported ({record.rows_imported} "
                f"trades). Delete it first if you want to re-import."
            )
        if record.kind == BAR_KIND:
            raise HubError(
                "this file is market data, not a statement -- there is nothing "
                "to add to the journal. Use it for backtesting instead."
            )
        if record.kind != TRADE_KIND:
            raise HubError(f"cannot import a file of kind {record.kind!r}")

        path = self.paths.uploads / record.stored_as
        if not path.exists():
            raise HubError(f"the stored file for {record.filename} is missing")

        settings = self.settings()
        stop_points = (
            default_stop_points
            if default_stop_points is not None
            else settings.get("default_stop_points")
        )
        try:
            trades, warnings = parse_trade_csv(
                path,
                symbol=symbol or settings.get("symbol"),
                tz=tz or settings.get("timezone"),
                excursion_unit=excursion_unit,
                default_stop_points=stop_points,
                session=DEFAULT_SESSION,
            )
        except ValueError as exc:
            record.status = "failed"
            record.error = str(exc)
            self.store.save_statement(record)
            raise HubError(str(exc)) from exc

        with self._journal() as journal:
            for trade in trades:
                journal.record(trade, source=record.source_tag)

        record.status = "imported"
        record.rows_imported = len(trades)
        record.warnings = warnings
        record.imported_at = _now()
        record.error = ""
        record.options = {
            **record.options,
            "default_stop_points": stop_points,
            "symbol": symbol or settings.get("symbol"),
            "timezone": tz or settings.get("timezone"),
            "excursion_unit": excursion_unit,
        }
        self.store.save_statement(record)
        return {"statement": record.to_dict(), "imported": len(trades),
                "warnings": warnings}

    def delete_statement(self, statement_id: str, remove_trades: bool = True) -> dict:
        """Remove an upload, and by default the trades it contributed."""
        record = self.store.statement(statement_id)
        if record is None:
            raise HubError(f"no statement {statement_id!r}")

        removed_trades = 0
        if remove_trades and record.status == "imported":
            with self._journal() as journal:
                removed_trades = journal.delete_by_source(record.source_tag)

        path = self.paths.uploads / record.stored_as
        path.unlink(missing_ok=True)
        self.store.remove_statement(statement_id)
        return {
            "deleted": record.to_dict(),
            "trades_removed": removed_trades,
        }

    # -- analysis ----------------------------------------------------------

    def overview(self) -> dict:
        """The headline numbers for the dashboard."""
        settings = self.settings()
        trades = self._trades()
        metrics = compute_metrics(trades, settings["equity"])
        statements = self.store.statements()
        with self._journal() as journal:
            sources = journal.sources()
            open_trades = len(journal.open_trades())
        return {
            "has_data": bool(trades),
            "metrics": _metrics_dict(metrics),
            "trade_count": len(trades),
            "open_trades": open_trades,
            "statement_count": len(statements),
            "imported_statements": sum(
                1 for record in statements if record.status == "imported"
            ),
            "sources": sources,
            "period": {
                "first": trades[0].entry_time.isoformat() if trades else None,
                "last": trades[-1].entry_time.isoformat() if trades else None,
            },
            "settings": settings,
        }

    def performance(
        self, start: str | None = None, end: str | None = None,
        symbol: str | None = None, setup: str | None = None,
    ) -> dict:
        settings = self.settings()
        trades = self._trades(start=start, end=end, symbol=symbol, setup=setup)
        metrics = compute_metrics(trades, settings["equity"])
        return {
            "metrics": _metrics_dict(metrics),
            "trade_count": len(trades),
            "filters": {"start": start, "end": end, "symbol": symbol, "setup": setup},
        }

    def equity_curve(self, **filters) -> dict:
        settings = self.settings()
        trades = self._trades(**filters)
        points = equity_curve(trades, settings["equity"])
        series = [
            {
                "t": None if isinstance(when, str) else when.isoformat(),
                "equity": round(value, 2),
                "index": index,
            }
            for index, (when, value) in enumerate(points)
        ]
        daily = [
            {"day": str(day), "pnl": round(value, 2)}
            for day, value in daily_pnl(trades).items()
        ]
        return {"curve": series, "daily": daily, "starting_equity": settings["equity"]}

    def breakdown(self, by: str, **filters) -> dict:
        """Per-bucket metrics -- where the money actually comes from."""
        if by not in GROUPERS:
            raise HubError(f"unknown grouping {by!r}; choose from {sorted(GROUPERS)}")
        trades = self._trades(**filters)
        grouped = group_by(trades, GROUPERS[by], self.settings()["equity"])
        return {
            "by": by,
            "buckets": [
                {
                    "name": str(name),
                    "trades": metrics.trades,
                    "win_rate": round(metrics.win_rate, 1),
                    "net_pnl": round(metrics.net_pnl, 2),
                    "expectancy": round(metrics.expectancy, 2),
                    "expectancy_r": (
                        None if metrics.expectancy_r is None
                        else round(metrics.expectancy_r, 3)
                    ),
                    "profit_factor": (
                        None if metrics.profit_factor == float("inf")
                        else round(metrics.profit_factor, 2)
                    ),
                }
                for name, metrics in grouped.items()
            ],
            "available": sorted(GROUPERS),
        }

    def trades(self, limit: int = 100, **filters) -> dict:
        """The most recent ``limit`` trades, oldest first.

        ``newest_first`` selects which trades come back, not their order --
        they arrive chronological, which is what sequential analysis needs.
        A caller that wants newest-at-the-top reverses for display.
        """
        with self._journal() as journal:
            rows = journal.trades(limit=limit, newest_first=True, **filters)
            total = journal.count()
        return {
            "trades": [_trade_row(trade) for trade in rows],
            "shown": len(rows),
            "total": total,
        }

    def behavior_review(self, min_trades: int = 20, **filters) -> dict:
        """The behavioral findings, as data rather than a text report."""
        trades = self._trades(**filters)
        report = behavior_module.analyze(trades, min_trades=min_trades)
        return {
            "unit": report.unit,
            "trades": report.trades,
            "days": report.days,
            "notes": report.notes,
            "findings": [
                {
                    **{
                        key: value for key, value in asdict(finding).items()
                        if key != "guardrail"
                    },
                    "guardrail": {
                        key: (value.strftime("%H:%M") if hasattr(value, "hour") else value)
                        for key, value in finding.guardrail.items()
                    },
                }
                for finding in report.findings
            ],
            "guardrails": {
                key: (value.strftime("%H:%M") if hasattr(value, "hour") else value)
                for key, value in report.guardrails().items()
            },
            "actionable_count": len(report.actionable()),
        }

    def position_size(
        self, entry: float, stop: float, target: float | None = None,
        symbol: str | None = None, equity: float | None = None,
    ) -> dict:
        settings = self.settings()
        try:
            instrument = get_instrument(symbol or settings["symbol"])
        except KeyError as exc:
            raise HubError(str(exc)) from exc
        limits = RiskLimits(
            risk_per_trade_pct=settings["risk_per_trade_pct"],
            max_daily_loss_pct=settings["max_daily_loss_pct"],
            max_drawdown_pct=settings["max_drawdown_pct"],
        )
        manager = RiskManager(instrument, equity or settings["equity"], limits)
        decision = manager.evaluate(
            datetime.now(DEFAULT_SESSION.tz), entry, stop, target
        )
        stop_points = abs(entry - stop)
        return {
            "approved": decision.approved,
            "contracts": decision.contracts,
            "risk_dollars": round(decision.risk_dollars, 2),
            "risk_points": stop_points,
            "risk_ticks": round(instrument.points_to_ticks(stop_points), 2),
            "per_contract_risk": round(stop_points * instrument.point_value, 2),
            "commission": round(
                instrument.round_turn_commission(max(decision.contracts, 1)), 2
            ),
            "reward_risk": (
                None if target is None or not stop_points
                else round(abs(target - entry) / stop_points, 2)
            ),
            "blockers": [str(item) for item in decision.blockers],
            "warnings": [str(item) for item in decision.warnings],
            "notes": decision.notes,
            "explanation": decision.explain(),
            "symbol": instrument.symbol,
        }
