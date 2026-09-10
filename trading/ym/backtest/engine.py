"""The bar-by-bar backtest engine.

Design rules, because a backtester's job is to be pessimistic:

* **No lookahead.** A strategy sees bars up to and including the current close.
  Orders it places are filled on later bars only.
* **The worst plausible path.** When a bar's range contains both the stop and
  the target, the stop is assumed to have been hit first. When price gaps
  through a stop, the fill is the gap price, not the stop price.
* **Costs are real.** Every entry and exit pays slippage in ticks, and every
  round turn pays commission.
* **Risk rules bind here too.** Every signal goes through the same
  :class:`~ym.risk.RiskManager` you would trade with, so a strategy cannot look
  good by taking positions your account would never have allowed. Signals that
  are refused are recorded, not silently dropped.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Sequence

from ..core import Bar, Direction, ExitReason, Trade
from ..instruments import Instrument
from ..metrics import Metrics, compute_metrics, equity_curve, format_breakdown
from ..risk import RiskLimits, RiskManager
from ..sessions import DEFAULT_SESSION, SessionSpec
from .strategy import Context, EntryType, Manage, Signal, Strategy


@dataclass(frozen=True)
class BacktestConfig:
    """Execution assumptions. Tighten them before you believe a result."""

    slippage_ticks: float = 1.0
    pessimistic_intrabar: bool = True
    trade_sessions: tuple[str, ...] = ("rth",)   # sessions where entries are allowed
    flatten_minutes_before_close: int | None = 5  # None keeps positions overnight
    no_new_trades_minutes_before_close: int = 30
    max_bars_in_trade: int | None = None
    allow_same_bar_exit: bool = True
    warmup_bars: int = 0


@dataclass
class PendingOrder:
    signal: Signal
    entry_price: float
    stop_price: float
    target_price: float | None
    contracts: int
    placed_index: int

    @property
    def expires_after(self) -> int:
        return self.placed_index + max(1, self.signal.valid_bars)


@dataclass
class BlockedSignal:
    """A signal the risk manager refused, kept for diagnosis."""

    ts: datetime
    direction: str
    entry_price: float
    stop_price: float
    setup: str
    reasons: list[str]


@dataclass
class BacktestResult:
    strategy: str
    symbol: str
    config: BacktestConfig
    limits: RiskLimits
    trades: list[Trade]
    blocked: list[BlockedSignal]
    metrics: Metrics
    risk_status: dict
    bars: int
    first_ts: datetime | None
    last_ts: datetime | None
    breaker_log: list[tuple[datetime, str]] = field(default_factory=list)
    starting_equity: float = 0.0

    def equity_points(self) -> list[tuple[object, float]]:
        return equity_curve(self.trades, self.starting_equity)

    def blocked_summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.blocked:
            for reason in item.reasons:
                code = reason.split("]")[0].lstrip("[")
                counts[code] = counts.get(code, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def format_report(self) -> str:
        span = ""
        if self.first_ts and self.last_ts:
            span = f"{self.first_ts:%Y-%m-%d} to {self.last_ts:%Y-%m-%d}"
        sections = [
            self.metrics.format_report(f"{self.strategy} on {self.symbol}  {span}"),
            "",
            f"Bars processed   {self.bars:,}",
            f"Slippage         {self.config.slippage_ticks:g} tick(s) per side",
            f"Sessions traded  {', '.join(self.config.trade_sessions)}",
        ]
        if self.blocked:
            sections += ["", f"Signals refused by risk rules: {len(self.blocked)}"]
            for code, count in self.blocked_summary().items():
                sections.append(f"  {code:<24}{count}")
        if self.breaker_log:
            sections += ["", "Circuit breakers tripped:"]
            for ts, message in self.breaker_log[:10]:
                sections.append(f"  {ts:%Y-%m-%d %H:%M}  {message}")
            if len(self.breaker_log) > 10:
                sections.append(f"  ... and {len(self.breaker_log) - 10} more")
        if self.trades:
            sections += ["", format_breakdown(self.trades, "exit_reason")]
        return "\n".join(sections)

    def write_trades_csv(self, path: str) -> None:
        fields = [
            "trade_id", "symbol", "setup", "direction", "entry_time", "entry_price",
            "exit_time", "exit_price", "contracts", "stop_price", "target_price",
            "points", "gross_pnl", "commission", "net_pnl", "r_multiple",
            "mae_points", "mfe_points", "exit_reason", "duration_minutes",
        ]
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(fields)
            for trade in self.trades:
                duration = trade.duration
                writer.writerow([
                    trade.trade_id, trade.symbol, trade.setup, trade.direction.value,
                    trade.entry_time.isoformat(), trade.entry_price,
                    trade.exit_time.isoformat() if trade.exit_time else "",
                    trade.exit_price, trade.contracts, trade.stop_price,
                    trade.target_price, round(trade.points, 4),
                    round(trade.gross_pnl, 2), round(trade.commission, 2),
                    round(trade.net_pnl, 2),
                    None if trade.r_multiple is None else round(trade.r_multiple, 4),
                    round(trade.mae_points, 4), round(trade.mfe_points, 4),
                    trade.exit_reason.value if trade.exit_reason else "",
                    None if duration is None else round(duration.total_seconds() / 60.0, 2),
                ])


class Backtester:
    """Runs one strategy over one series of bars."""

    def __init__(
        self,
        instrument: Instrument,
        strategy: Strategy,
        starting_equity: float = 25_000.0,
        limits: RiskLimits | None = None,
        config: BacktestConfig | None = None,
        session: SessionSpec = DEFAULT_SESSION,
    ) -> None:
        self.instrument = instrument
        self.strategy = strategy
        self.config = config or BacktestConfig()
        self.session = session
        self.limits = limits or RiskLimits()
        self.starting_equity = starting_equity
        self.risk = RiskManager(instrument, starting_equity, self.limits, session)

    # -- helpers -----------------------------------------------------------

    @property
    def _slippage(self) -> float:
        return self.config.slippage_ticks * self.instrument.tick_size

    def _commission(self, contracts: int) -> float:
        return self.instrument.round_turn_commission(contracts)

    def _entries_allowed(self, ts: datetime, day: date) -> bool:
        if self.session.session_of(ts).value not in self.config.trade_sessions:
            return False
        cutoff = self.config.no_new_trades_minutes_before_close
        if cutoff and self.session.session_of(ts).value == "rth":
            if ts >= self.session.flatten_time(day, cutoff):
                return False
        return True

    # -- fills -------------------------------------------------------------

    def _try_fill(self, order: PendingOrder, bar: Bar) -> float | None:
        """Return the fill price for a resting order on this bar, or ``None``."""
        long = order.signal.direction is Direction.LONG
        slip = self._slippage
        kind = order.signal.entry_type

        if kind is EntryType.MARKET:
            return bar.open + slip if long else bar.open - slip

        trigger = order.entry_price
        if kind is EntryType.STOP:
            # A buy stop can gap: if the bar opens above it, that is the fill.
            if long and bar.high >= trigger:
                return max(trigger, bar.open) + slip
            if not long and bar.low <= trigger:
                return min(trigger, bar.open) - slip
            return None

        if kind is EntryType.LIMIT:
            # Limit orders do not slip, but they only fill if price trades there.
            if long and bar.low <= trigger:
                return min(trigger, bar.open)
            if not long and bar.high >= trigger:
                return max(trigger, bar.open)
            return None

        raise ValueError(f"unhandled entry type {kind}")

    def _check_exit(
        self, trade: Trade, bar: Bar, stop: float, target: float | None
    ) -> tuple[float, ExitReason] | None:
        """Resolve stop/target against one bar, assuming the worse path."""
        long = trade.direction is Direction.LONG
        slip = self._slippage

        # Gaps first: an open beyond the level fills at the open, not the level.
        if long and bar.open <= stop:
            return bar.open - slip, ExitReason.STOP
        if not long and bar.open >= stop:
            return bar.open + slip, ExitReason.STOP
        if target is not None:
            if long and bar.open >= target:
                return bar.open, ExitReason.TARGET
            if not long and bar.open <= target:
                return bar.open, ExitReason.TARGET

        hit_stop = bar.low <= stop if long else bar.high >= stop
        hit_target = (
            target is not None and (bar.high >= target if long else bar.low <= target)
        )

        if hit_stop and hit_target:
            if self.config.pessimistic_intrabar:
                return (stop - slip if long else stop + slip), ExitReason.STOP
            return target, ExitReason.TARGET
        if hit_stop:
            return (stop - slip if long else stop + slip), ExitReason.STOP
        if hit_target:
            return target, ExitReason.TARGET
        return None

    def _market_exit_price(self, trade: Trade, bar: Bar) -> float:
        slip = self._slippage
        return bar.close - slip if trade.direction is Direction.LONG else bar.close + slip

    # -- the run loop ------------------------------------------------------

    def run(self, bars: Sequence[Bar]) -> BacktestResult:
        if not bars:
            raise ValueError("no bars to backtest")

        strategy = self.strategy
        config = self.config
        trades: list[Trade] = []
        blocked: list[BlockedSignal] = []
        breaker_log: list[tuple[datetime, str]] = []

        position: Trade | None = None
        pending: PendingOrder | None = None
        active_stop: float = 0.0
        active_target: float | None = None
        bars_in_trade = 0
        current_day: date | None = None
        day_start_index = 0

        def make_context(index: int) -> Context:
            return Context(
                instrument=self.instrument,
                session=self.session,
                bars=bars,
                index=index,
                equity=self.risk.state.equity,
                position=position,
                session_day=current_day,
                day_bars=bars[day_start_index : index + 1],
            )

        def close_position(
            trade: Trade, ts: datetime, price: float, reason: ExitReason, index: int
        ) -> None:
            nonlocal position, bars_in_trade
            trade.close(ts, price, reason, commission=self._commission(trade.contracts))
            trades.append(trade)
            for violation in self.risk.register_trade(trade):
                breaker_log.append((ts, str(violation)))
            position = None
            bars_in_trade = 0
            strategy.on_trade_closed(make_context(index), trade)

        strategy.on_start(make_context(0))

        for index, bar in enumerate(bars):
            day = self.session.session_day(bar.ts)
            if day != current_day:
                current_day = day
                day_start_index = index
                self.risk.roll_to(bar.ts)
                strategy.on_session_start(make_context(index), day)

            if index < config.warmup_bars:
                continue

            # 1. Resolve an open position against this bar.
            if position is not None:
                position.update_excursions(bar.high, bar.low)
                bars_in_trade += 1
                exit_fill = self._check_exit(position, bar, active_stop, active_target)
                if exit_fill is not None:
                    price, reason = exit_fill
                    close_position(position, bar.ts, price, reason, index)
                elif (
                    config.max_bars_in_trade is not None
                    and bars_in_trade >= config.max_bars_in_trade
                ):
                    close_position(
                        position, bar.ts, self._market_exit_price(position, bar),
                        ExitReason.TIME_STOP, index,
                    )
                elif config.flatten_minutes_before_close is not None and bar.ts >= (
                    self.session.flatten_time(day, config.flatten_minutes_before_close)
                ):
                    close_position(
                        position, bar.ts, self._market_exit_price(position, bar),
                        ExitReason.SESSION_CLOSE, index,
                    )

            # 2. Fill a resting entry order.
            if position is None and pending is not None:
                fill_price = self._try_fill(pending, bar)
                if fill_price is not None:
                    position = Trade(
                        symbol=self.instrument.symbol,
                        direction=pending.signal.direction,
                        entry_time=bar.ts,
                        entry_price=fill_price,
                        contracts=pending.contracts,
                        point_value=self.instrument.point_value,
                        stop_price=pending.stop_price,
                        target_price=pending.target_price,
                        commission=self._commission(pending.contracts),
                        setup=pending.signal.setup or strategy.name,
                        tags=list(pending.signal.tags),
                        notes=pending.signal.notes,
                        trade_id=len(trades) + 1,
                    )
                    active_stop = pending.stop_price
                    active_target = pending.target_price
                    bars_in_trade = 1
                    pending = None
                    position.update_excursions(bar.high, bar.low)
                    if config.allow_same_bar_exit:
                        exit_fill = self._check_exit(position, bar, active_stop, active_target)
                        if exit_fill is not None:
                            price, reason = exit_fill
                            close_position(position, bar.ts, price, reason, index)
                elif index >= pending.expires_after:
                    pending = None

            context = make_context(index)

            # 3. Manage an open position at the close of this bar.
            if position is not None:
                instruction = strategy.manage(context, position)
                if isinstance(instruction, Manage):
                    if instruction.exit_now:
                        close_position(
                            position, bar.ts, self._market_exit_price(position, bar),
                            ExitReason.SIGNAL, index,
                        )
                    else:
                        if instruction.new_stop is not None:
                            active_stop = instruction.new_stop
                        if instruction.new_target is not None:
                            active_target = instruction.new_target
                            position.target_price = instruction.new_target
                continue

            # 4. Look for a new entry.
            if pending is not None or not self._entries_allowed(bar.ts, day):
                continue

            signal = strategy.on_bar(context)
            if signal is None:
                continue

            entry_price, stop_price, target_price = signal.resolve(bar.close)
            decision = self.risk.evaluate(
                bar.ts, entry_price, stop_price, target_price, signal.max_contracts
            )
            if not decision.approved:
                blocked.append(
                    BlockedSignal(
                        ts=bar.ts,
                        direction=signal.direction.value,
                        entry_price=entry_price,
                        stop_price=stop_price,
                        setup=signal.setup or strategy.name,
                        reasons=[str(v) for v in decision.blockers],
                    )
                )
                continue

            pending = PendingOrder(
                signal=signal,
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                contracts=decision.contracts,
                placed_index=index,
            )

        # Any position still open at the end is marked to the last close.
        if position is not None:
            last = bars[-1]
            close_position(
                position, last.ts, self._market_exit_price(position, last),
                ExitReason.END_OF_DATA, len(bars) - 1,
            )

        return BacktestResult(
            strategy=strategy.name,
            symbol=self.instrument.symbol,
            config=config,
            limits=self.limits,
            trades=trades,
            blocked=blocked,
            metrics=compute_metrics(trades, self.starting_equity, self.session),
            risk_status=self.risk.status(),
            bars=len(bars),
            first_ts=bars[0].ts,
            last_ts=bars[-1].ts,
            breaker_log=breaker_log,
            starting_equity=self.starting_equity,
        )
