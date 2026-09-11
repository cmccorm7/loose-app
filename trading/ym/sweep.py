"""Testing one parameter across a range of values, without fooling yourself.

Running a strategy at ``min_rejections`` 2, 3, 4 and 5 and keeping the best
result is not a test -- it is four chances to find noise. This module exists to
make that failure mode visible rather than convenient:

* Every row reports a **standard error** on its expectancy. A row reading
  ``+0.08R ± 0.14`` has not shown you anything, no matter where it ranks.
* ``split`` re-runs each value on an earlier and a later slice of the data, so
  you can see whether the winner on the first half is still a winner on the
  second. That is the single cheapest defence against curve-fitting.
* The summary states how many configurations were tried, because the best of
  eight looks better than the best of two for reasons that have nothing to do
  with the market.

None of this makes a sweep safe. It makes a bad sweep legible.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Sequence

from .backtest import BacktestConfig, Backtester
from .backtest.strategy import Strategy
from .core import Bar
from .instruments import Instrument
from .metrics import Metrics
from .risk import RiskLimits
from .sessions import DEFAULT_SESSION, SessionSpec


@dataclass
class Slice:
    """One strategy run over one stretch of bars."""

    metrics: Metrics
    blocked: int
    expectancy_r_se: float | None = None   # standard error of the mean R

    @property
    def expectancy_r(self) -> float | None:
        return self.metrics.expectancy_r

    @property
    def is_significant(self) -> bool:
        """Is the expectancy more than two standard errors from zero?"""
        if self.expectancy_r is None or not self.expectancy_r_se:
            return False
        return abs(self.expectancy_r) > 2 * self.expectancy_r_se


@dataclass
class SweepRow:
    value: Any
    full: Slice
    train: Slice | None = None
    test: Slice | None = None


@dataclass
class SweepReport:
    param: str
    rows: list[SweepRow]
    strategy: str
    symbol: str
    bars: int
    split: bool
    fixed_params: dict = field(default_factory=dict)

    # -- reading the results ----------------------------------------------

    def best(self, on: str = "full") -> SweepRow | None:
        scored = [
            row for row in self.rows
            if getattr(row, on) is not None
            and getattr(row, on).expectancy_r is not None
        ]
        if not scored:
            return None
        return max(scored, key=lambda row: getattr(row, on).expectancy_r)

    def ranking(self, on: str) -> list[Any]:
        scored = [
            row for row in self.rows
            if getattr(row, on) is not None
            and getattr(row, on).expectancy_r is not None
        ]
        scored.sort(key=lambda row: -getattr(row, on).expectancy_r)
        return [row.value for row in scored]

    def _table(self, attribute: str, title: str) -> list[str]:
        header = (
            f"{self.param:<18}{'Trades':>7}{'Win%':>7}{'Net P&L':>12}"
            f"{'Exp R':>9}{'+/-':>8}{'PF':>7}{'MaxDD%':>8}{'Blocked':>9}"
        )
        lines = [title, header, "-" * len(header)]
        for row in self.rows:
            piece = getattr(row, attribute)
            if piece is None or piece.metrics.trades == 0:
                lines.append(f"{str(row.value):<18}{'no trades':>7}")
                continue
            metrics = piece.metrics
            expectancy = (
                "n/a" if piece.expectancy_r is None else f"{piece.expectancy_r:+.3f}"
            )
            error = "n/a" if piece.expectancy_r_se is None else f"{piece.expectancy_r_se:.3f}"
            factor = (
                "inf" if metrics.profit_factor == float("inf")
                else f"{metrics.profit_factor:.2f}"
            )
            marker = " *" if piece.is_significant else "  "
            lines.append(
                f"{str(row.value):<18}{metrics.trades:>7}{metrics.win_rate:>6.1f}%"
                f"{metrics.net_pnl:>12,.2f}{expectancy:>9}{error:>8}{factor:>7}"
                f"{metrics.max_drawdown_pct:>7.1f}%{piece.blocked:>9}{marker}"
            )
        return lines

    def format_report(self) -> str:
        fixed = ", ".join(f"{k}={v}" for k, v in self.fixed_params.items())
        lines = [
            f"Sweep of {self.param} -- {self.strategy} on {self.symbol}",
            "=" * 64,
            f"{self.bars:,} bars, {len(self.rows)} configurations tried"
            + (f", holding {fixed}" if fixed else ""),
            "",
        ]
        if self.split:
            lines += self._table("train", "FIRST HALF (in sample)")
            lines += ["", ]
            lines += self._table("test", "SECOND HALF (out of sample)")
        else:
            lines += self._table("full", "ALL DATA")
        lines += ["", "* = expectancy more than two standard errors from zero.", ""]
        lines += self._verdict()
        return "\n".join(lines)

    def _verdict(self) -> list[str]:
        lines = ["Reading this:"]
        significant = [
            row for row in self.rows
            if (row.test or row.full) and (row.test or row.full).is_significant
        ]
        if self.split:
            best_train = self.best("train")
            best_test = self.best("test")
            if best_train is None or best_test is None:
                lines.append(
                    "  Not enough trades in one of the halves to compare them."
                )
                return lines
            order = self.ranking("test")
            rank = order.index(best_train.value) + 1 if best_train.value in order else None
            lines.append(
                f"  Best in sample: {self.param}={best_train.value} "
                f"({best_train.train.expectancy_r:+.3f}R)."
            )
            out = best_train.test
            if out is None or out.metrics.trades == 0:
                lines.append("  It took no trades out of sample.")
            else:
                lines.append(
                    f"  Out of sample it did {out.expectancy_r:+.3f}R over "
                    f"{out.metrics.trades} trades"
                    + (f", ranking {rank} of {len(order)}." if rank else ".")
                )
                if rank == 1:
                    lines.append(
                        "  It held up. That is encouraging and still not proof -- "
                        "two halves of one instrument is a small test."
                    )
                else:
                    lines.append(
                        "  It did not hold up. The in-sample ranking was most "
                        "likely noise; do not adopt the winner."
                    )
        else:
            best = self.best("full")
            if best is None:
                lines.append("  No configuration produced enough trades to judge.")
                return lines
            lines.append(
                f"  Best: {self.param}={best.value} "
                f"({best.full.expectancy_r:+.3f}R over {best.full.metrics.trades} trades)."
            )
            lines.append(
                "  Re-run with --split before believing it. Picking the best of "
                f"{len(self.rows)} on one sample is how backtests lie."
            )

        if not significant:
            lines.append(
                "  No configuration's expectancy cleared two standard errors, so "
                "the differences between these rows are within noise."
            )
        return lines


# --------------------------------------------------------------------------

def split_bars(
    bars: Sequence[Bar], fraction: float = 0.5, session: SessionSpec = DEFAULT_SESSION
) -> tuple[list[Bar], list[Bar]]:
    """Split chronologically on a trading-day boundary, never mid-session."""
    if not 0 < fraction < 1:
        raise ValueError("fraction must be between 0 and 1")
    days = sorted({session.session_day(bar.ts) for bar in bars})
    if len(days) < 2:
        raise ValueError("need at least two trading days to split")
    cut_index = max(1, min(len(days) - 1, round(len(days) * fraction)))
    cutoff = days[cut_index]
    first = [bar for bar in bars if session.session_day(bar.ts) < cutoff]
    second = [bar for bar in bars if session.session_day(bar.ts) >= cutoff]
    return first, second


def _expectancy_error(trades) -> float | None:
    """Standard error of mean R -- how much of the expectancy is luck."""
    values = [trade.r_multiple for trade in trades if trade.r_multiple is not None]
    if len(values) < 3:
        return None
    return statistics.stdev(values) / math.sqrt(len(values))


def run_once(
    instrument: Instrument,
    strategy_class: type[Strategy],
    params: dict,
    bars: Sequence[Bar],
    equity: float,
    limits: RiskLimits,
    config: BacktestConfig,
    session: SessionSpec = DEFAULT_SESSION,
) -> Slice:
    """One backtest. The strategy is rebuilt each time, since it holds state."""
    strategy = strategy_class(**params)
    result = Backtester(
        instrument, strategy, equity, limits, config, session
    ).run(bars)
    return Slice(
        metrics=result.metrics,
        blocked=len(result.blocked),
        expectancy_r_se=_expectancy_error(result.trades),
    )


def sweep(
    instrument: Instrument,
    strategy_class: type[Strategy],
    param: str,
    values: Sequence[Any],
    bars: Sequence[Bar],
    equity: float = 25_000.0,
    limits: RiskLimits | None = None,
    config: BacktestConfig | None = None,
    fixed_params: dict | None = None,
    split: bool = False,
    split_fraction: float = 0.5,
    session: SessionSpec = DEFAULT_SESSION,
) -> SweepReport:
    """Run ``strategy_class`` once per value of ``param``."""
    if not values:
        raise ValueError("no values to sweep")
    limits = limits or RiskLimits()
    config = config or BacktestConfig()
    fixed_params = dict(fixed_params or {})

    train, test = (split_bars(bars, split_fraction, session) if split else (None, None))
    rows: list[SweepRow] = []
    # The class name, not a built instance's -- a strategy that folds the swept
    # parameter into its own name would otherwise label the whole report with
    # whichever value happened to run last.
    name = strategy_class.__name__

    for value in values:
        params = {**fixed_params, param: value}
        try:
            full = run_once(
                instrument, strategy_class, params, bars, equity, limits, config, session
            )
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{param}={value!r} is not a usable setting: {exc}") from exc
        row = SweepRow(value=value, full=full)
        if split:
            row.train = run_once(
                instrument, strategy_class, params, train, equity, limits, config, session
            )
            row.test = run_once(
                instrument, strategy_class, params, test, equity, limits, config, session
            )
        rows.append(row)

    return SweepReport(
        param=param,
        rows=rows,
        strategy=name,
        symbol=instrument.symbol,
        bars=len(bars),
        split=split,
        fixed_params=fixed_params,
    )
