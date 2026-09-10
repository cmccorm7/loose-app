"""Risk management: position sizing, loss limits and circuit breakers.

This is the layer the rest of the system is built around. The backtester runs
every candidate trade through the same :class:`RiskManager` you would use
live, so a strategy cannot backtest well by taking positions your rules would
never have allowed.

Two ideas do most of the work:

* **Size follows the stop.** You choose what a trade may cost you; the distance
  to the stop then determines how many contracts that buys. Never the reverse.
* **Budgets are hard.** A daily loss limit does not merely stop you after it is
  breached -- it shrinks the size of the trades before it, so the last trade of
  a bad day cannot blow through it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum

from .core import Trade
from .instruments import Instrument
from .sessions import DEFAULT_SESSION, SessionSpec


class SizingMethod(str, Enum):
    FIXED_FRACTIONAL = "fixed_fractional"  # risk a % of current equity
    FIXED_DOLLAR = "fixed_dollar"          # risk a flat dollar amount
    FIXED_CONTRACTS = "fixed_contracts"    # always the same size


class Lockout(str, Enum):
    NONE = "none"
    DAILY_LOSS = "daily_loss_limit"
    GIVEBACK = "daily_giveback"
    WEEKLY_LOSS = "weekly_loss_limit"
    DRAWDOWN = "max_drawdown"
    CONSECUTIVE_LOSSES = "consecutive_losses"
    PROFIT_TARGET = "daily_profit_target"
    TRADE_COUNT = "max_trades_per_day"
    COOLDOWN = "cooldown"
    MANUAL = "manual"


@dataclass(frozen=True)
class RiskLimits:
    """The rules of your account, in one place.

    Set a limit to ``None`` to disable it. The defaults are deliberately
    conservative for a personal retail account: half a percent per trade, two
    percent per day, ten percent maximum drawdown.
    """

    # --- sizing -----------------------------------------------------------
    sizing: SizingMethod = SizingMethod.FIXED_FRACTIONAL
    risk_per_trade_pct: float = 0.5          # percent of equity, not a fraction
    fixed_dollar_risk: float | None = None   # used by FIXED_DOLLAR
    fixed_contracts: int = 1                 # used by FIXED_CONTRACTS
    max_risk_per_trade: float | None = None  # absolute dollar ceiling
    max_contracts: int = 10

    # --- trade quality ----------------------------------------------------
    min_stop_ticks: int = 4                  # reject absurdly tight stops
    max_stop_points: float | None = None     # reject absurdly wide ones
    min_reward_risk: float | None = None     # e.g. 1.5 to refuse sub-1.5R trades

    # --- daily / weekly budgets ------------------------------------------
    max_daily_loss_pct: float | None = 2.0
    max_daily_loss: float | None = None
    daily_profit_target: float | None = None       # dollars; None = keep trading
    daily_profit_target_pct: float | None = None
    max_trades_per_day: int | None = None
    max_weekly_loss_pct: float | None = None

    # Stop handing back a good day: once the session's P&L has peaked, this caps
    # the share of that peak you may give back before the day is over.
    max_daily_giveback_pct: float | None = None
    # No new entries at or after this exchange-local time, whatever the setup.
    hard_stop_time: time | None = None

    # --- circuit breakers -------------------------------------------------
    max_drawdown_pct: float | None = 10.0          # from peak closed equity
    max_consecutive_losses: int | None = 3
    consecutive_loss_action: str = "cooldown"      # "cooldown" or "lock_day"
    cooldown_minutes: int = 30

    # --- capital ----------------------------------------------------------
    max_margin_utilization: float = 0.5      # fraction of equity tied up in margin

    def __post_init__(self) -> None:
        if self.risk_per_trade_pct <= 0 and self.sizing is SizingMethod.FIXED_FRACTIONAL:
            raise ValueError("risk_per_trade_pct must be positive")
        if self.sizing is SizingMethod.FIXED_DOLLAR and not self.fixed_dollar_risk:
            raise ValueError("FIXED_DOLLAR sizing needs fixed_dollar_risk")
        if self.consecutive_loss_action not in ("cooldown", "lock_day"):
            raise ValueError("consecutive_loss_action must be 'cooldown' or 'lock_day'")


@dataclass
class Violation:
    code: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


@dataclass
class RiskDecision:
    """The answer to 'may I take this trade, and how big?'"""

    approved: bool
    contracts: int
    risk_dollars: float
    risk_points: float
    blockers: list[Violation] = field(default_factory=list)
    warnings: list[Violation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def explain(self) -> str:
        lines = []
        if self.approved:
            lines.append(
                f"APPROVED  {self.contracts} contract(s)  "
                f"risking ${self.risk_dollars:,.2f} ({self.risk_points:g} pts)"
            )
        else:
            lines.append("REJECTED")
        for note in self.notes:
            lines.append(f"  - {note}")
        for warning in self.warnings:
            lines.append(f"  ! {warning}")
        for blocker in self.blockers:
            lines.append(f"  x {blocker}")
        return "\n".join(lines)


@dataclass
class RiskState:
    """Mutable account state. Persisted between sessions if you want it to be."""

    equity: float
    peak_equity: float
    session_day: date | None = None
    day_start_equity: float = 0.0
    day_pnl: float = 0.0
    day_peak_pnl: float = 0.0
    day_trades: int = 0
    week_key: tuple[int, int] | None = None
    week_start_equity: float = 0.0
    consecutive_losses: int = 0
    lockout: Lockout = Lockout.NONE
    lockout_reason: str = ""
    locked_until: datetime | None = None

    @property
    def drawdown(self) -> float:
        return max(0.0, self.peak_equity - self.equity)

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return 100.0 * self.drawdown / self.peak_equity


class RiskManager:
    """Enforces :class:`RiskLimits` against a running account.

    Usage is always the same three steps: ``evaluate`` before entering,
    trade, then ``register_trade`` on the closed result.
    """

    def __init__(
        self,
        instrument: Instrument,
        starting_equity: float,
        limits: RiskLimits | None = None,
        session: SessionSpec = DEFAULT_SESSION,
    ) -> None:
        if starting_equity <= 0:
            raise ValueError("starting_equity must be positive")
        self.instrument = instrument
        self.limits = limits or RiskLimits()
        self.session = session
        self.state = RiskState(equity=starting_equity, peak_equity=starting_equity)
        self.starting_equity = starting_equity

    # -- day/week rollover -------------------------------------------------

    def roll_to(self, ts: datetime) -> None:
        """Advance internal day/week state to ``ts``, resetting what resets."""
        day = self.session.session_day(ts)
        state = self.state

        if state.session_day != day:
            state.session_day = day
            state.day_start_equity = state.equity
            state.day_pnl = 0.0
            state.day_peak_pnl = 0.0
            state.day_trades = 0
            # Day-scoped lockouts expire overnight; drawdown does not.
            if state.lockout in (
                Lockout.DAILY_LOSS,
                Lockout.GIVEBACK,
                Lockout.PROFIT_TARGET,
                Lockout.TRADE_COUNT,
                Lockout.CONSECUTIVE_LOSSES,
                Lockout.COOLDOWN,
            ):
                self.clear_lockout()
            state.consecutive_losses = 0

        week = (day.isocalendar().year, day.isocalendar().week)
        if state.week_key != week:
            state.week_key = week
            state.week_start_equity = state.equity
            if state.lockout is Lockout.WEEKLY_LOSS:
                self.clear_lockout()

        if (
            state.lockout is Lockout.COOLDOWN
            and state.locked_until is not None
            and ts >= state.locked_until
        ):
            self.clear_lockout()

    def clear_lockout(self) -> None:
        self.state.lockout = Lockout.NONE
        self.state.lockout_reason = ""
        self.state.locked_until = None

    def lock(self, kind: Lockout, reason: str, until: datetime | None = None) -> None:
        self.state.lockout = kind
        self.state.lockout_reason = reason
        self.state.locked_until = until

    # -- budgets -----------------------------------------------------------

    def daily_loss_limit(self) -> float | None:
        """Dollars the account may lose today, or ``None`` if uncapped."""
        limits = self.limits
        candidates = []
        if limits.max_daily_loss is not None:
            candidates.append(abs(limits.max_daily_loss))
        if limits.max_daily_loss_pct is not None:
            base = self.state.day_start_equity or self.state.equity
            candidates.append(base * limits.max_daily_loss_pct / 100.0)
        return min(candidates) if candidates else None

    def remaining_daily_risk(self) -> float | None:
        """What is left of today's loss budget."""
        limit = self.daily_loss_limit()
        if limit is None:
            return None
        return max(0.0, limit + min(0.0, self.state.day_pnl))

    def daily_profit_target(self) -> float | None:
        limits = self.limits
        candidates = []
        if limits.daily_profit_target is not None:
            candidates.append(abs(limits.daily_profit_target))
        if limits.daily_profit_target_pct is not None:
            base = self.state.day_start_equity or self.state.equity
            candidates.append(base * limits.daily_profit_target_pct / 100.0)
        return min(candidates) if candidates else None

    def base_risk_budget(self) -> float:
        """Dollars this account is willing to lose on one trade, before caps."""
        limits = self.limits
        if limits.sizing is SizingMethod.FIXED_DOLLAR:
            budget = float(limits.fixed_dollar_risk or 0.0)
        else:
            budget = self.state.equity * limits.risk_per_trade_pct / 100.0
        if limits.max_risk_per_trade is not None:
            budget = min(budget, limits.max_risk_per_trade)
        return budget

    # -- the main entry point ---------------------------------------------

    def evaluate(
        self,
        ts: datetime,
        entry_price: float,
        stop_price: float,
        target_price: float | None = None,
        requested_contracts: int | None = None,
    ) -> RiskDecision:
        """Decide whether to take a trade and at what size."""
        self.roll_to(ts)
        inst = self.instrument
        limits = self.limits
        state = self.state
        blockers: list[Violation] = []
        warnings: list[Violation] = []
        notes: list[str] = []

        risk_points = abs(entry_price - stop_price)
        per_contract_risk = risk_points * inst.point_value

        # --- structural checks on the trade itself ------------------------
        if risk_points <= 0:
            blockers.append(Violation("no_stop", "entry and stop are the same price"))
            return RiskDecision(False, 0, 0.0, 0.0, blockers, warnings, notes)

        stop_ticks = inst.points_to_ticks(risk_points)
        if stop_ticks < limits.min_stop_ticks:
            blockers.append(
                Violation(
                    "stop_too_tight",
                    f"stop is {stop_ticks:.1f} ticks, minimum is {limits.min_stop_ticks}",
                )
            )
        if limits.max_stop_points is not None and risk_points > limits.max_stop_points:
            blockers.append(
                Violation(
                    "stop_too_wide",
                    f"stop is {risk_points:g} pts, maximum is {limits.max_stop_points:g}",
                )
            )
        if target_price is not None and limits.min_reward_risk is not None:
            reward = abs(target_price - entry_price)
            rr = reward / risk_points
            if rr < limits.min_reward_risk:
                blockers.append(
                    Violation(
                        "reward_risk",
                        f"reward:risk is {rr:.2f}, minimum is {limits.min_reward_risk:.2f}",
                    )
                )

        # --- account-level gates ------------------------------------------
        if state.lockout is not Lockout.NONE:
            blockers.append(
                Violation(f"locked_{state.lockout.value}", state.lockout_reason)
            )

        if limits.hard_stop_time is not None:
            local_time = self.session.localize(ts).time()
            if local_time >= limits.hard_stop_time:
                blockers.append(
                    Violation(
                        "hard_stop_time",
                        f"it is {local_time:%H:%M}, past the "
                        f"{limits.hard_stop_time:%H:%M} hard stop time",
                    )
                )

        if limits.max_trades_per_day is not None and state.day_trades >= limits.max_trades_per_day:
            blockers.append(
                Violation(
                    "trade_count",
                    f"already took {state.day_trades} trades today "
                    f"(limit {limits.max_trades_per_day})",
                )
            )

        remaining = self.remaining_daily_risk()
        if remaining is not None and remaining <= 0:
            blockers.append(
                Violation(
                    "daily_loss_limit",
                    f"daily loss limit of ${self.daily_loss_limit():,.2f} is exhausted",
                )
            )

        # --- sizing --------------------------------------------------------
        if limits.sizing is SizingMethod.FIXED_CONTRACTS:
            contracts = limits.fixed_contracts
            budget = contracts * per_contract_risk
            notes.append(f"fixed size: {contracts} contract(s)")
        else:
            budget = self.base_risk_budget()
            notes.append(f"risk budget ${budget:,.2f} / ${per_contract_risk:,.2f} per contract")
            if remaining is not None and remaining < budget:
                budget = remaining
                notes.append(
                    f"budget reduced to ${budget:,.2f} by remaining daily loss allowance"
                )
            contracts = int(math.floor(budget / per_contract_risk)) if per_contract_risk else 0

        if requested_contracts is not None:
            if requested_contracts < contracts:
                notes.append(f"strategy requested {requested_contracts}, below the cap")
            contracts = min(contracts, requested_contracts)

        if contracts > limits.max_contracts:
            notes.append(f"capped at max_contracts={limits.max_contracts}")
            contracts = limits.max_contracts

        # Margin: never tie up more than max_margin_utilization of equity.
        if inst.day_margin > 0 and limits.max_margin_utilization > 0:
            affordable = int(
                math.floor(state.equity * limits.max_margin_utilization / inst.day_margin)
            )
            if affordable < contracts:
                notes.append(
                    f"capped at {affordable} by margin "
                    f"(${inst.day_margin:,.0f}/contract, "
                    f"{limits.max_margin_utilization:.0%} of equity)"
                )
                contracts = max(0, affordable)

        if contracts < 1 and not blockers:
            blockers.append(
                Violation(
                    "size_zero",
                    f"risk budget does not cover one contract "
                    f"(${per_contract_risk:,.2f} needed at a {risk_points:g} pt stop)",
                )
            )

        # --- soft warnings --------------------------------------------------
        dd = state.drawdown_pct
        if limits.max_drawdown_pct is not None and dd > limits.max_drawdown_pct * 0.7:
            warnings.append(
                Violation(
                    "drawdown_warning",
                    f"drawdown is {dd:.1f}% of peak, limit is {limits.max_drawdown_pct:.1f}%",
                )
            )
        if remaining is not None and per_contract_risk and remaining < 2 * per_contract_risk:
            warnings.append(
                Violation(
                    "budget_warning",
                    f"only ${remaining:,.2f} of today's loss budget remains",
                )
            )

        approved = not blockers and contracts >= 1
        risk_dollars = contracts * per_contract_risk if approved else 0.0
        return RiskDecision(
            approved=approved,
            contracts=contracts if approved else 0,
            risk_dollars=risk_dollars,
            risk_points=risk_points,
            blockers=blockers,
            warnings=warnings,
            notes=notes,
        )

    def size_position(
        self, entry_price: float, stop_price: float, ts: datetime | None = None
    ) -> RiskDecision:
        """Sizing-only convenience wrapper; uses 'now' when no time is given."""
        when = ts or datetime.now(self.session.tz)
        return self.evaluate(when, entry_price, stop_price)

    # -- feedback loop -----------------------------------------------------

    def register_trade(self, trade: Trade) -> list[Violation]:
        """Apply a closed trade's result and fire any circuit breakers.

        Returns the breakers that tripped, so a caller can log or alert.
        """
        if not trade.is_closed:
            raise ValueError("only closed trades can be registered")
        ts = trade.exit_time
        self.roll_to(trade.entry_time)
        state = self.state
        limits = self.limits
        pnl = trade.net_pnl

        state.equity += pnl
        state.day_pnl += pnl
        state.day_peak_pnl = max(state.day_peak_pnl, state.day_pnl)
        state.day_trades += 1
        state.peak_equity = max(state.peak_equity, state.equity)
        if pnl < 0:
            state.consecutive_losses += 1
        elif pnl > 0:
            state.consecutive_losses = 0

        tripped: list[Violation] = []

        if limits.max_drawdown_pct is not None and state.drawdown_pct >= limits.max_drawdown_pct:
            reason = (
                f"drawdown {state.drawdown_pct:.1f}% reached the "
                f"{limits.max_drawdown_pct:.1f}% limit; stop trading and review"
            )
            self.lock(Lockout.DRAWDOWN, reason)
            tripped.append(Violation("max_drawdown", reason))
            return tripped  # hardest breaker wins; nothing else matters today

        limit = self.daily_loss_limit()
        if limit is not None and state.day_pnl <= -limit:
            reason = (
                f"down ${abs(state.day_pnl):,.2f} today, at or past the "
                f"${limit:,.2f} daily loss limit"
            )
            self.lock(Lockout.DAILY_LOSS, reason)
            tripped.append(Violation("daily_loss_limit", reason))

        if limits.max_daily_giveback_pct is not None and state.day_peak_pnl > 0:
            given_back = state.day_peak_pnl - state.day_pnl
            allowance = state.day_peak_pnl * limits.max_daily_giveback_pct / 100.0
            if given_back > 0 and given_back >= allowance:
                reason = (
                    f"gave back ${given_back:,.2f} of a ${state.day_peak_pnl:,.2f} "
                    f"peak, past the {limits.max_daily_giveback_pct:.0f}% give-back "
                    f"limit; bank the day"
                )
                self.lock(Lockout.GIVEBACK, reason)
                tripped.append(Violation("daily_giveback", reason))

        if limits.max_weekly_loss_pct is not None and state.week_start_equity > 0:
            week_pnl = state.equity - state.week_start_equity
            week_limit = state.week_start_equity * limits.max_weekly_loss_pct / 100.0
            if week_pnl <= -week_limit:
                reason = (
                    f"down ${abs(week_pnl):,.2f} this week, at or past the "
                    f"${week_limit:,.2f} weekly limit"
                )
                self.lock(Lockout.WEEKLY_LOSS, reason)
                tripped.append(Violation("weekly_loss_limit", reason))

        target = self.daily_profit_target()
        if target is not None and state.day_pnl >= target:
            reason = f"hit the ${target:,.2f} daily profit target; done for the day"
            self.lock(Lockout.PROFIT_TARGET, reason)
            tripped.append(Violation("daily_profit_target", reason))

        if (
            limits.max_consecutive_losses is not None
            and state.consecutive_losses >= limits.max_consecutive_losses
            and state.lockout is Lockout.NONE
        ):
            if limits.consecutive_loss_action == "lock_day":
                reason = (
                    f"{state.consecutive_losses} losses in a row; "
                    "stepping away for the day"
                )
                self.lock(Lockout.CONSECUTIVE_LOSSES, reason)
            else:
                until = ts + timedelta(minutes=limits.cooldown_minutes)
                reason = (
                    f"{state.consecutive_losses} losses in a row; "
                    f"cooling off until {until:%H:%M}"
                )
                self.lock(Lockout.COOLDOWN, reason, until=until)
            tripped.append(Violation("consecutive_losses", reason))

        if (
            limits.max_trades_per_day is not None
            and state.day_trades >= limits.max_trades_per_day
            and state.lockout is Lockout.NONE
        ):
            reason = f"took {state.day_trades} trades today; that is the limit"
            self.lock(Lockout.TRADE_COUNT, reason)
            tripped.append(Violation("max_trades_per_day", reason))

        return tripped

    # -- reporting ---------------------------------------------------------

    def status(self, ts: datetime | None = None) -> dict:
        if ts is not None:
            self.roll_to(ts)
        state = self.state
        return {
            "equity": round(state.equity, 2),
            "peak_equity": round(state.peak_equity, 2),
            "total_pnl": round(state.equity - self.starting_equity, 2),
            "drawdown": round(state.drawdown, 2),
            "drawdown_pct": round(state.drawdown_pct, 2),
            "session_day": str(state.session_day) if state.session_day else None,
            "day_pnl": round(state.day_pnl, 2),
            "day_peak_pnl": round(state.day_peak_pnl, 2),
            "day_trades": state.day_trades,
            "daily_loss_limit": self.daily_loss_limit(),
            "remaining_daily_risk": self.remaining_daily_risk(),
            "risk_per_trade": round(self.base_risk_budget(), 2),
            "consecutive_losses": state.consecutive_losses,
            "lockout": state.lockout.value,
            "lockout_reason": state.lockout_reason,
        }

    def format_status(self, ts: datetime | None = None) -> str:
        s = self.status(ts)
        lines = [
            f"Equity           ${s['equity']:,.2f}  (peak ${s['peak_equity']:,.2f})",
            f"Total P&L        ${s['total_pnl']:,.2f}",
            f"Drawdown         ${s['drawdown']:,.2f}  ({s['drawdown_pct']:.2f}%)",
            f"Session day      {s['session_day']}",
            f"Today            ${s['day_pnl']:,.2f} over {s['day_trades']} trade(s)"
            + (
                f"  (peaked at ${s['day_peak_pnl']:,.2f})"
                if s["day_peak_pnl"] > s["day_pnl"]
                else ""
            ),
            f"Risk per trade   ${s['risk_per_trade']:,.2f}",
        ]
        if s["daily_loss_limit"] is not None:
            lines.append(
                f"Daily loss limit ${s['daily_loss_limit']:,.2f}  "
                f"(${s['remaining_daily_risk']:,.2f} left)"
            )
        lines.append(f"Losing streak    {s['consecutive_losses']}")
        if s["lockout"] != "none":
            lines.append(f"LOCKED OUT       {s['lockout']}: {s['lockout_reason']}")
        return "\n".join(lines)
