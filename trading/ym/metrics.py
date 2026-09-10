"""Performance statistics for a list of closed trades.

Shared by the backtester and the journal, so a backtest and your real results
are measured with the same yardstick and can be compared honestly.

The numbers worth watching are expectancy (average R per trade), profit factor,
and maximum drawdown. Win rate on its own tells you almost nothing.
"""

from __future__ import annotations

import math
import statistics
from collections import OrderedDict, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from typing import Callable, Iterable, Sequence

from .core import Trade
from .sessions import DEFAULT_SESSION, SessionSpec

TRADING_DAYS_PER_YEAR = 252


@dataclass
class Metrics:
    """Summary statistics. All dollar figures are net of commission."""

    trades: int = 0
    wins: int = 0
    losses: int = 0
    scratches: int = 0
    win_rate: float = 0.0
    net_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    commission: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0        # dollars per trade
    expectancy_r: float | None = None
    total_r: float | None = None
    avg_win: float = 0.0
    avg_loss: float = 0.0
    win_loss_ratio: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    sharpe: float | None = None
    sortino: float | None = None
    avg_mae_r: float | None = None
    avg_mfe_r: float | None = None
    avg_duration_minutes: float | None = None
    starting_equity: float = 0.0
    final_equity: float = 0.0
    return_pct: float = 0.0
    trading_days: int = 0
    avg_daily_pnl: float = 0.0
    best_day: float = 0.0
    worst_day: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)

    def format_report(self, title: str = "Performance") -> str:
        def money(value: float) -> str:
            return f"${value:,.2f}"

        def maybe(value: float | None, spec: str = ".2f") -> str:
            return "n/a" if value is None else format(value, spec)

        rows = [
            ("Trades", f"{self.trades}  ({self.wins}W / {self.losses}L / {self.scratches}S)"),
            ("Win rate", f"{self.win_rate:.1f}%"),
            ("Net P&L", money(self.net_pnl)),
            ("Commission", money(self.commission)),
            ("Profit factor", f"{self.profit_factor:.2f}" if self.profit_factor else "n/a"),
            ("Expectancy", f"{money(self.expectancy)} / trade"),
            ("Expectancy (R)", maybe(self.expectancy_r, "+.3f")),
            ("Total R", maybe(self.total_r, "+.2f")),
            ("Avg win / loss", f"{money(self.avg_win)} / {money(self.avg_loss)}"),
            ("Win:loss size", f"{self.win_loss_ratio:.2f}" if self.win_loss_ratio else "n/a"),
            ("Largest win/loss", f"{money(self.largest_win)} / {money(self.largest_loss)}"),
            ("Max drawdown", f"{money(self.max_drawdown)}  ({self.max_drawdown_pct:.2f}%)"),
            ("Max streaks", f"{self.max_consecutive_wins}W / {self.max_consecutive_losses}L"),
            ("Sharpe / Sortino", f"{maybe(self.sharpe)} / {maybe(self.sortino)}"),
            ("Avg MAE / MFE (R)", f"{maybe(self.avg_mae_r, '+.2f')} / {maybe(self.avg_mfe_r, '+.2f')}"),
            ("Avg hold", f"{maybe(self.avg_duration_minutes, '.1f')} min"),
            ("Equity", f"{money(self.starting_equity)} -> {money(self.final_equity)}  ({self.return_pct:+.2f}%)"),
            ("Days traded", f"{self.trading_days}  (avg {money(self.avg_daily_pnl)}/day)"),
            ("Best / worst day", f"{money(self.best_day)} / {money(self.worst_day)}"),
        ]
        width = max(len(label) for label, _ in rows)
        lines = [title, "=" * max(len(title), 46)]
        lines += [f"{label.ljust(width)}  {value}" for label, value in rows]
        return "\n".join(lines)


def _drawdown(equity_points: Sequence[float]) -> tuple[float, float]:
    peak = equity_points[0] if equity_points else 0.0
    worst = 0.0
    worst_pct = 0.0
    for value in equity_points:
        peak = max(peak, value)
        decline = peak - value
        if decline > worst:
            worst = decline
            worst_pct = 100.0 * decline / peak if peak else 0.0
    return worst, worst_pct


def _streaks(results: Sequence[int]) -> tuple[int, int]:
    best_win = best_loss = run_win = run_loss = 0
    for outcome in results:
        if outcome > 0:
            run_win += 1
            run_loss = 0
        elif outcome < 0:
            run_loss += 1
            run_win = 0
        else:
            run_win = run_loss = 0
        best_win = max(best_win, run_win)
        best_loss = max(best_loss, run_loss)
    return best_win, best_loss


def equity_curve(
    trades: Iterable[Trade], starting_equity: float = 0.0
) -> list[tuple[object, float]]:
    """Closed-trade equity curve as ``(exit_time, equity)`` points."""
    equity = starting_equity
    points: list[tuple[object, float]] = [("start", equity)]
    for trade in sorted(
        (t for t in trades if t.is_closed), key=lambda t: t.exit_time
    ):
        equity += trade.net_pnl
        points.append((trade.exit_time, equity))
    return points


def daily_pnl(
    trades: Iterable[Trade], session: SessionSpec = DEFAULT_SESSION
) -> "OrderedDict[date, float]":
    """Net P&L per trading day, keyed by session day of the *entry*."""
    totals: dict[date, float] = defaultdict(float)
    for trade in trades:
        if trade.is_closed:
            totals[session.session_day(trade.entry_time)] += trade.net_pnl
    return OrderedDict(sorted(totals.items()))


def compute_metrics(
    trades: Sequence[Trade],
    starting_equity: float = 0.0,
    session: SessionSpec = DEFAULT_SESSION,
) -> Metrics:
    closed = [trade for trade in trades if trade.is_closed]
    metrics = Metrics(starting_equity=starting_equity, final_equity=starting_equity)
    if not closed:
        return metrics

    pnls = [trade.net_pnl for trade in closed]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]

    metrics.trades = len(closed)
    metrics.wins = len(wins)
    metrics.losses = len(losses)
    metrics.scratches = len(closed) - len(wins) - len(losses)
    decisive = len(wins) + len(losses)
    metrics.win_rate = 100.0 * len(wins) / decisive if decisive else 0.0
    metrics.net_pnl = sum(pnls)
    metrics.gross_profit = sum(wins)
    metrics.gross_loss = abs(sum(losses))
    metrics.commission = sum(trade.commission for trade in closed)
    if metrics.gross_loss:
        metrics.profit_factor = metrics.gross_profit / metrics.gross_loss
    else:
        metrics.profit_factor = float("inf") if metrics.gross_profit else 0.0
    metrics.expectancy = metrics.net_pnl / len(closed)
    metrics.avg_win = statistics.fmean(wins) if wins else 0.0
    metrics.avg_loss = statistics.fmean(losses) if losses else 0.0
    metrics.win_loss_ratio = (
        abs(metrics.avg_win / metrics.avg_loss) if metrics.avg_loss else 0.0
    )
    metrics.largest_win = max(pnls)
    metrics.largest_loss = min(pnls)

    r_values = [trade.r_multiple for trade in closed if trade.r_multiple is not None]
    if r_values:
        metrics.expectancy_r = statistics.fmean(r_values)
        metrics.total_r = sum(r_values)

    mae = [trade.mae_r for trade in closed if trade.mae_r is not None]
    mfe = [trade.mfe_r for trade in closed if trade.mfe_r is not None]
    metrics.avg_mae_r = statistics.fmean(mae) if mae else None
    metrics.avg_mfe_r = statistics.fmean(mfe) if mfe else None

    durations = [
        trade.duration.total_seconds() / 60.0 for trade in closed if trade.duration
    ]
    metrics.avg_duration_minutes = statistics.fmean(durations) if durations else None

    curve = [value for _, value in equity_curve(closed, starting_equity)]
    metrics.max_drawdown, metrics.max_drawdown_pct = _drawdown(curve)
    metrics.final_equity = curve[-1]
    metrics.return_pct = (
        100.0 * (metrics.final_equity - starting_equity) / starting_equity
        if starting_equity
        else 0.0
    )

    ordered = sorted(closed, key=lambda t: t.exit_time)
    metrics.max_consecutive_wins, metrics.max_consecutive_losses = _streaks(
        [1 if trade.net_pnl > 0 else -1 if trade.net_pnl < 0 else 0 for trade in ordered]
    )

    by_day = daily_pnl(closed, session)
    metrics.trading_days = len(by_day)
    day_values = list(by_day.values())
    if day_values:
        metrics.avg_daily_pnl = statistics.fmean(day_values)
        metrics.best_day = max(day_values)
        metrics.worst_day = min(day_values)

    if starting_equity > 0 and len(day_values) > 1:
        returns = [value / starting_equity for value in day_values]
        mean = statistics.fmean(returns)
        stdev = statistics.pstdev(returns)
        if stdev > 0:
            metrics.sharpe = mean / stdev * math.sqrt(TRADING_DAYS_PER_YEAR)
        downside = [value for value in returns if value < 0]
        if downside:
            downside_dev = math.sqrt(statistics.fmean([value ** 2 for value in downside]))
            if downside_dev > 0:
                metrics.sortino = mean / downside_dev * math.sqrt(TRADING_DAYS_PER_YEAR)

    return metrics


# --- slicing the results -------------------------------------------------

def group_by(
    trades: Sequence[Trade],
    key: Callable[[Trade], object],
    starting_equity: float = 0.0,
    session: SessionSpec = DEFAULT_SESSION,
) -> "OrderedDict[object, Metrics]":
    """Compute metrics per bucket, e.g. per setup or per hour of day."""
    buckets: dict[object, list[Trade]] = defaultdict(list)
    for trade in trades:
        if trade.is_closed:
            buckets[key(trade)].append(trade)
    return OrderedDict(
        (name, compute_metrics(items, starting_equity, session))
        for name, items in sorted(buckets.items(), key=lambda kv: str(kv[0]))
    )


WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

GROUPERS: dict[str, Callable[[Trade], object]] = {
    "setup": lambda t: t.setup or "(untagged)",
    "direction": lambda t: t.direction.value,
    "weekday": lambda t: WEEKDAY_NAMES[t.entry_time.weekday()],
    "hour": lambda t: f"{t.entry_time.hour:02d}:00",
    "session": lambda t: DEFAULT_SESSION.session_of(t.entry_time).value,
    "month": lambda t: f"{t.entry_time:%Y-%m}",
    "exit_reason": lambda t: t.exit_reason.value if t.exit_reason else "(none)",
    "contracts": lambda t: f"{t.contracts} lot",
}


def format_breakdown(
    trades: Sequence[Trade], by: str, session: SessionSpec = DEFAULT_SESSION
) -> str:
    """A compact per-bucket table -- where the money actually comes from."""
    if by not in GROUPERS:
        raise KeyError(f"unknown grouping {by!r}; choose from {sorted(GROUPERS)}")
    groups = group_by(trades, GROUPERS[by], session=session)
    if not groups:
        return f"No closed trades to break down by {by}."

    header = f"{by.title():<14}{'Trades':>7}{'Win%':>7}{'Net P&L':>12}{'Exp $':>10}{'Exp R':>8}{'PF':>7}"
    lines = [header, "-" * len(header)]
    for name, metrics in groups.items():
        expectancy_r = "n/a" if metrics.expectancy_r is None else f"{metrics.expectancy_r:+.2f}"
        profit_factor = (
            "inf" if metrics.profit_factor == float("inf") else f"{metrics.profit_factor:.2f}"
        )
        lines.append(
            f"{str(name):<14}{metrics.trades:>7}{metrics.win_rate:>6.1f}%"
            f"{metrics.net_pnl:>12,.2f}{metrics.expectancy:>10,.2f}"
            f"{expectancy_r:>8}{profit_factor:>7}"
        )
    return "\n".join(lines)
