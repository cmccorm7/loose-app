"""In-session guidance: turn what you did last month into a nudge right now.

:mod:`ym.behavior` looks backwards and tells you that your fourth trade of the
day loses money. That is only useful if something says it *while you are about
to take a fourth trade*. This module is that something.

A :class:`Coach` combines three things:

* your behavioral profile, from past journalled trades,
* the hard limits in your :class:`~ym.risk.RiskLimits`, and
* today, so far.

It emits :class:`Nudge` objects rather than blocking anything. Blocking is the
risk manager's job and it happens on hard numbers; the coach exists for the
softer signals -- the ones that are true of you specifically, that no generic
rule would catch.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from .behavior import BehaviorReport, analyze
from .core import Trade
from .risk import Lockout, RiskManager
from .sessions import DEFAULT_SESSION, SessionSpec

URGENCY_ORDER = {"stop": 0, "caution": 1, "note": 2}


@dataclass
class Nudge:
    urgency: str        # "stop" | "caution" | "note"
    message: str
    basis: str = ""     # the evidence behind it, so you can disagree with it

    def format(self) -> str:
        marker = {"stop": "STOP  ", "caution": "CAUTION", "note": "note   "}[self.urgency]
        line = f"{marker}  {self.message}"
        if self.basis:
            line += f"\n          ({self.basis})"
        return line


@dataclass
class BehaviorProfile:
    """The handful of numbers the coach actually needs at the table."""

    unit: str = "R"
    by_ordinal: dict[int, float] = field(default_factory=dict)
    by_hour: dict[int, float] = field(default_factory=dict)
    decay_ordinal: int | None = None      # last trade number that still pays
    fade_hour: int | None = None          # hour from which expectancy goes negative
    post_loss_window: int | None = None   # minutes after a loss that are costly
    post_loss_value: float | None = None
    giveback_share: float | None = None   # share of green days that finish red
    median_green_peak: float | None = None
    sizes_up_after_loss: bool = False
    sample: int = 0

    @classmethod
    def from_report(cls, report: BehaviorReport) -> "BehaviorProfile":
        profile = cls(unit=report.unit, sample=report.trades)
        if not report.features:
            return profile

        ordinal_values: dict[int, list[float]] = defaultdict(list)
        hour_values: dict[int, list[float]] = defaultdict(list)
        for feature in report.features:
            ordinal_values[min(feature.ordinal, 6)].append(feature.value)
            hour_values[feature.hour].append(feature.value)
        profile.by_ordinal = {
            key: statistics.fmean(values) for key, values in sorted(ordinal_values.items())
        }
        profile.by_hour = {
            key: statistics.fmean(values) for key, values in sorted(hour_values.items())
        }

        for finding in report.findings:
            if finding.severity == "info":
                continue
            if finding.code == "ordinal_decay":
                profile.decay_ordinal = finding.guardrail.get("max_trades_per_day")
            elif finding.code == "time_of_day_decay":
                stop_time = finding.guardrail.get("hard_stop_time")
                profile.fade_hour = stop_time.hour if stop_time else None
            elif finding.code == "revenge_window":
                profile.post_loss_window = finding.evidence.get("window_minutes")
                profile.post_loss_value = finding.effect
            elif finding.code == "giveback":
                profile.giveback_share = finding.effect
                profile.median_green_peak = finding.evidence.get("median_peak")
            elif finding.code == "size_escalation":
                profile.sizes_up_after_loss = True
        return profile


class Coach:
    """Watches the session in progress and says something when it should."""

    def __init__(
        self,
        profile: BehaviorProfile | None = None,
        session: SessionSpec = DEFAULT_SESSION,
    ) -> None:
        self.profile = profile or BehaviorProfile()
        self.session = session

    @classmethod
    def from_trades(
        cls, history: Sequence[Trade], session: SessionSpec = DEFAULT_SESSION
    ) -> "Coach":
        """Build a coach from journalled history."""
        return cls(BehaviorProfile.from_report(analyze(history, session)), session)

    # -- the check you run before clicking buy -----------------------------

    def check(
        self,
        now: datetime,
        risk: RiskManager,
        today: Sequence[Trade] = (),
    ) -> list[Nudge]:
        """Everything worth saying at this moment, most urgent first."""
        profile = self.profile
        unit = profile.unit
        nudges: list[Nudge] = []
        state = risk.state
        local = self.session.localize(now)
        closed_today = [trade for trade in today if trade.is_closed]
        next_ordinal = len(closed_today) + 1

        # --- hard state first -------------------------------------------
        if state.lockout is not Lockout.NONE:
            nudges.append(
                Nudge("stop", f"You are locked out: {state.lockout_reason}",
                      "risk rules, not a suggestion")
            )

        remaining = risk.remaining_daily_risk()
        if remaining is not None and remaining > 0:
            budget = risk.base_risk_budget()
            if budget and remaining < budget:
                nudges.append(
                    Nudge(
                        "caution",
                        f"Only ${remaining:,.2f} of today's loss budget is left -- "
                        f"less than one full-size trade (${budget:,.2f}).",
                        "the next trade will be sized down automatically",
                    )
                )

        limits = risk.limits
        if limits.hard_stop_time is not None:
            minutes_left = (
                datetime.combine(local.date(), limits.hard_stop_time, tzinfo=local.tzinfo)
                - local
            ).total_seconds() / 60.0
            if 0 < minutes_left <= 20:
                nudges.append(
                    Nudge("caution",
                          f"{minutes_left:.0f} minutes to your {limits.hard_stop_time:%H:%M} "
                          f"hard stop. Do not start something you cannot finish.",
                          "your own rule")
                )

        # --- profile-driven, the part that is specific to you ------------
        if profile.decay_ordinal and next_ordinal > profile.decay_ordinal:
            value = profile.by_ordinal.get(min(next_ordinal, 6))
            basis = (
                f"trade {min(next_ordinal, 6)} historically averages "
                f"{value:+.2f}{unit} over {profile.sample} trades"
                if value is not None else f"based on {profile.sample} past trades"
            )
            nudges.append(
                Nudge(
                    "stop",
                    f"This would be trade {next_ordinal} today. Your edge historically "
                    f"stops after trade {profile.decay_ordinal}.",
                    basis,
                )
            )
        elif profile.decay_ordinal and next_ordinal == profile.decay_ordinal:
            nudges.append(
                Nudge("note",
                      f"This is your last planned trade of the day (limit "
                      f"{profile.decay_ordinal}). Make it one you would post.",
                      "your ordinal decay pattern")
            )

        if profile.fade_hour is not None and local.hour >= profile.fade_hour:
            value = profile.by_hour.get(local.hour)
            basis = (
                f"{local.hour:02d}:00 averages {value:+.2f}{unit} for you"
                if value is not None else ""
            )
            nudges.append(
                Nudge("stop",
                      f"It is {local:%H:%M}. Everything you take from "
                      f"{profile.fade_hour:02d}:00 on has lost money historically.",
                      basis)
            )

        last_loss_exit = None
        for trade in reversed(closed_today):
            if trade.net_pnl < 0:
                last_loss_exit = trade.exit_time
                break
        if last_loss_exit and profile.post_loss_window:
            elapsed = (now - last_loss_exit).total_seconds() / 60.0
            if 0 <= elapsed <= profile.post_loss_window:
                value = profile.post_loss_value
                nudges.append(
                    Nudge(
                        "stop",
                        f"You took a loss {elapsed:.0f} minutes ago. Your trades inside "
                        f"{profile.post_loss_window} minutes of a loss are your worst "
                        f"trades. Wait {profile.post_loss_window - elapsed:.0f} more "
                        f"minutes.",
                        f"they run {value:+.2f}{unit} worse than the rest"
                        if value is not None else "",
                    )
                )

        if profile.sizes_up_after_loss and closed_today and closed_today[-1].net_pnl < 0:
            nudges.append(
                Nudge("caution",
                      "Last trade was a loss and you have a habit of sizing up after "
                      "one. Take the size the stop gives you, not the size that gets "
                      "it back.",
                      "your size-after-loss pattern")
            )

        # --- protecting a green day --------------------------------------
        peak = state.day_peak_pnl
        if peak > 0 and state.day_pnl < peak:
            given = peak - state.day_pnl
            share = given / peak
            allowed = limits.max_daily_giveback_pct
            if allowed is not None and share >= allowed / 100.0 * 0.6:
                nudges.append(
                    Nudge("caution",
                          f"You are ${given:,.2f} off today's ${peak:,.2f} peak "
                          f"({share:.0%} of it). Your give-back limit is {allowed:.0f}%.",
                          "green days turning red is a pattern of yours"
                          if profile.giveback_share else "")
                )
            elif profile.giveback_share and share >= 0.4:
                nudges.append(
                    Nudge("caution",
                          f"You have given back {share:.0%} of today's ${peak:,.2f} peak. "
                          f"{profile.giveback_share:.0%} of your green days end flat or red.",
                          "consider banking it")
                )

        if (
            profile.median_green_peak
            and state.day_pnl >= profile.median_green_peak
            and state.lockout is Lockout.NONE
        ):
            nudges.append(
                Nudge("note",
                      f"Today is +${state.day_pnl:,.2f}, at or past your typical good "
                      f"day (${profile.median_green_peak:,.2f}). This is the point you "
                      f"usually start giving it back.",
                      "")
            )

        if limits.max_consecutive_losses and state.consecutive_losses:
            left = limits.max_consecutive_losses - state.consecutive_losses
            if left == 1:
                nudges.append(
                    Nudge("caution",
                          f"{state.consecutive_losses} "
                          f"{'loss' if state.consecutive_losses == 1 else 'losses'} "
                          f"in a row. One more ends your day.",
                          "your max_consecutive_losses rule")
                )

        nudges.sort(key=lambda nudge: URGENCY_ORDER[nudge.urgency])
        return nudges

    def brief(
        self, now: datetime, risk: RiskManager, today: Sequence[Trade] = ()
    ) -> str:
        """A short pre-trade readout: state, then anything worth saying."""
        nudges = self.check(now, risk, today)
        lines = [risk.format_status(now), ""]
        if not nudges:
            lines.append("Nothing flagged. Take the setup or do not.")
        else:
            lines.append("Coach:")
            lines += [nudge.format() for nudge in nudges]
        return "\n".join(lines)
