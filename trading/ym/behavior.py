"""Behavioral analysis: find the patterns in *how* you trade, not what you trade.

Most retail damage is not a bad strategy. It is a decent strategy operated by a
person who trades a fourth time after three wins, doubles up eight minutes
after a stop-out, or gives back a green morning between 13:00 and 15:00. Those
patterns are invisible in a headline P&L number and obvious in a per-trade
breakdown.

Each detector here compares a slice of your trades against the rest of them and
reports:

* the **effect** -- how much worse (or better) the slice is,
* the **sample** -- how many trades support it, and
* a **guardrail** -- a concrete change to :class:`~ym.risk.RiskLimits` that
  would have prevented it.

A warning about what this is. These are hypotheses generated from your own
small sample, not laws. Thirty trades cannot distinguish a real habit from a
run of bad luck, so every finding carries a confidence label and the p-value
behind it, and a "low" confidence finding is a thing to watch, not to act on.
The honest use is: read the findings, decide which match something you already
suspect about yourself, and put a guardrail on that one.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Callable, Sequence

from .core import Direction, Trade
from .risk import RiskLimits
from .sessions import DEFAULT_SESSION, SessionSpec

# A slice needs at least this many trades before it is reported at all.
MIN_SLICE = 6
# ... and this many before it can reach "strong" confidence.
STRONG_SLICE = 20

SEVERITY_ORDER = {"act": 0, "watch": 1, "info": 2}
CONFIDENCE_ORDER = {"strong": 0, "moderate": 1, "low": 2}


# --------------------------------------------------------------------------
# statistics helpers (stdlib only, deliberately)
# --------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def welch(a: Sequence[float], b: Sequence[float]) -> tuple[float | None, float | None]:
    """Welch's t statistic and a normal-approximation two-sided p-value.

    The normal approximation is close enough at these sample sizes to separate
    "probably noise" from "probably real", which is all it is used for.
    """
    if len(a) < 2 or len(b) < 2:
        return None, None
    variance_a = statistics.variance(a)
    variance_b = statistics.variance(b)
    standard_error = math.sqrt(variance_a / len(a) + variance_b / len(b))
    if standard_error == 0:
        return None, None
    t = (statistics.fmean(a) - statistics.fmean(b)) / standard_error
    return t, 2.0 * (1.0 - _norm_cdf(abs(t)))


def adjust_p(p: float | None, comparisons: int) -> float | None:
    """Bonferroni correction for detectors that scan for their own worst slice.

    A detector that tries five cutoffs and reports the worst one has five
    chances to find a pattern in noise, so its raw p-value is optimistic by
    roughly that factor. Correcting for it is the difference between a tool that
    finds your habits and one that invents them.
    """
    if p is None:
        return None
    return min(1.0, p * max(1, comparisons))


def _confidence(slice_n: int, rest_n: int, p: float | None) -> str:
    if p is None:
        return "low"
    if p < 0.05 and slice_n >= STRONG_SLICE and rest_n >= STRONG_SLICE:
        return "strong"
    if p < 0.15 and slice_n >= MIN_SLICE * 2:
        return "moderate"
    return "low"


def _severity_by_confidence(confidence: str) -> str:
    """For detectors whose effect is not measured in the report's unit."""
    return {"strong": "act", "moderate": "watch", "low": "info"}[confidence]


def _severity(confidence: str, effect: float, threshold: float) -> str:
    if confidence == "strong" and abs(effect) >= threshold:
        return "act"
    if confidence in ("strong", "moderate") and abs(effect) >= threshold * 0.6:
        return "watch"
    return "info"


# --------------------------------------------------------------------------
# per-trade features
# --------------------------------------------------------------------------

@dataclass
class TradeFeatures:
    """One trade, annotated with the context it was taken in."""

    trade: Trade
    value: float                 # outcome in the report's unit (R or dollars)
    session_day: date
    ordinal: int                 # 1 = first trade of that day
    minutes_since_open: float
    hour: int
    minutes_since_prior_exit: float | None
    prior_was_loss: bool
    losses_before_today: int     # consecutive losses immediately before this trade
    day_pnl_before: float
    day_peak_before: float
    contracts: int
    weekday: int

    @property
    def is_loss(self) -> bool:
        return self.trade.net_pnl < 0


@dataclass
class DayRecord:
    """One trading day, reconstructed from its trades."""

    day: date
    trades: int
    net_pnl: float
    peak_pnl: float              # best point the day's cumulative P&L reached
    trough_pnl: float
    first_trade: datetime
    last_trade: datetime

    @property
    def giveback(self) -> float:
        """Dollars handed back from the day's high-water mark."""
        return max(0.0, self.peak_pnl - self.net_pnl)


@dataclass
class Finding:
    code: str
    severity: str                # "act" | "watch" | "info"
    headline: str
    detail: str
    suggestion: str
    sample: int
    confidence: str
    effect: float = 0.0
    effect_unit: str | None = None   # None means the report's own unit (R or $)
    p_value: float | None = None
    guardrail: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)

    def format(self, unit: str) -> str:
        marker = {"act": "!!", "watch": " !", "info": "  "}[self.severity]
        p_text = "n/a" if self.p_value is None else f"{self.p_value:.3f}"
        effect_unit = unit if self.effect_unit is None else self.effect_unit
        lines = [
            f"{marker} {self.headline}",
            f"     {self.detail}",
            f"     -> {self.suggestion}",
            f"     n={self.sample}  effect={self.effect:+.2f}{effect_unit}  "
            f"p~{p_text}  confidence={self.confidence}",
        ]
        if self.guardrail:
            settings = ", ".join(
                f"{key}={value:%H:%M}" if hasattr(value, "hour") else f"{key}={value}"
                for key, value in self.guardrail.items()
            )
            lines.append(f"     guardrail: {settings}")
        return "\n".join(lines)


@dataclass
class BehaviorReport:
    findings: list[Finding]
    unit: str                    # "R" or "$"
    trades: int
    days: int
    day_records: list[DayRecord]
    features: list[TradeFeatures]
    notes: list[str] = field(default_factory=list)

    def actionable(self) -> list[Finding]:
        return [f for f in self.findings if f.severity in ("act", "watch")]

    def guardrails(self) -> dict:
        """Merged guardrail settings from every actionable finding.

        Findings are ordered by severity, then confidence, then sample size, and
        the first one to set a key keeps it. So where two detectors disagree --
        a per-trade pattern over 90 trades saying "cap the day at 2" against a
        day-level one over 20 days saying "cap it at 1" -- the better-evidenced
        number wins rather than simply the tighter one.
        """
        merged: dict = {}
        for finding in self.actionable():
            for key, value in finding.guardrail.items():
                merged.setdefault(key, value)
        return merged

    def suggested_limits(self, base: RiskLimits | None = None) -> RiskLimits:
        """``base`` with the merged guardrails applied. Review before using."""
        from dataclasses import replace

        current = base or RiskLimits()
        guardrails = self.guardrails()
        valid = {
            key: value
            for key, value in guardrails.items()
            if key in current.__dataclass_fields__
        }
        return replace(current, **valid) if valid else current

    def format_report(self) -> str:
        header = [
            "Behavioral review",
            "=" * 46,
            f"{self.trades} closed trades across {self.days} trading days, "
            f"measured in {self.unit}.",
        ]
        header += [f"note: {note}" for note in self.notes]
        if not self.findings:
            header += [
                "",
                "No patterns cleared the minimum sample size. Keep journaling -- "
                "most detectors need 40+ trades before they can say anything.",
            ]
            return "\n".join(header)

        body = ["", "Findings, most actionable first:", ""]
        for finding in self.findings:
            body.append(finding.format(self.unit))
            body.append("")
        guardrails = self.guardrails()
        if guardrails:
            body.append("Suggested guardrails (review before adopting):")
            for key, value in guardrails.items():
                body.append(f"  {key} = {value}")
            body.append("")
        body.append(
            "Remember: these are hypotheses from your own sample, not laws. "
            "Act on the ones you recognize."
        )
        return "\n".join(header + body)


# --------------------------------------------------------------------------
# feature extraction
# --------------------------------------------------------------------------

def build_features(
    trades: Sequence[Trade], session: SessionSpec = DEFAULT_SESSION
) -> tuple[list[TradeFeatures], str, list[str]]:
    """Annotate trades with context. Returns ``(features, unit, notes)``."""
    closed = sorted(
        (trade for trade in trades if trade.is_closed), key=lambda t: t.entry_time
    )
    notes: list[str] = []
    with_stops = [trade for trade in closed if trade.r_multiple is not None]
    if closed and len(with_stops) >= 0.8 * len(closed):
        unit = "R"
    else:
        unit = "$"
        if closed:
            notes.append(
                f"only {len(with_stops)}/{len(closed)} trades record an initial stop, "
                "so results are measured in dollars. Record stops to get R."
            )

    by_day: dict[date, list[Trade]] = defaultdict(list)
    for trade in closed:
        by_day[session.session_day(trade.entry_time)].append(trade)

    features: list[TradeFeatures] = []
    for day, day_trades in sorted(by_day.items()):
        running = 0.0
        peak = 0.0
        streak = 0
        previous_exit: datetime | None = None
        previous_loss = False
        for ordinal, trade in enumerate(day_trades, start=1):
            gap = None
            if previous_exit is not None:
                gap = (trade.entry_time - previous_exit).total_seconds() / 60.0
            value = trade.r_multiple if unit == "R" else trade.net_pnl
            local = session.localize(trade.entry_time)
            features.append(
                TradeFeatures(
                    trade=trade,
                    value=float(value if value is not None else 0.0),
                    session_day=day,
                    ordinal=ordinal,
                    minutes_since_open=session.minutes_since_rth_open(trade.entry_time),
                    hour=local.hour,
                    minutes_since_prior_exit=gap,
                    prior_was_loss=previous_loss,
                    losses_before_today=streak,
                    day_pnl_before=running,
                    day_peak_before=peak,
                    contracts=trade.contracts,
                    weekday=local.weekday(),
                )
            )
            running += trade.net_pnl
            peak = max(peak, running)
            previous_exit = trade.exit_time
            previous_loss = trade.net_pnl < 0
            streak = streak + 1 if trade.net_pnl < 0 else 0
    return features, unit, notes


def build_days(
    features: Sequence[TradeFeatures],
) -> list[DayRecord]:
    by_day: dict[date, list[TradeFeatures]] = defaultdict(list)
    for feature in features:
        by_day[feature.session_day].append(feature)

    records = []
    for day, items in sorted(by_day.items()):
        running = 0.0
        peak = 0.0
        trough = 0.0
        for item in items:
            running += item.trade.net_pnl
            peak = max(peak, running)
            trough = min(trough, running)
        records.append(
            DayRecord(
                day=day,
                trades=len(items),
                net_pnl=running,
                peak_pnl=peak,
                trough_pnl=trough,
                first_trade=items[0].trade.entry_time,
                last_trade=items[-1].trade.exit_time or items[-1].trade.entry_time,
            )
        )
    return records


# --------------------------------------------------------------------------
# detectors
# --------------------------------------------------------------------------

def _compare(
    slice_values: Sequence[float], rest_values: Sequence[float]
) -> tuple[float, float, float, float | None]:
    """Return ``(slice_mean, rest_mean, effect, p_value)``."""
    slice_mean = statistics.fmean(slice_values) if slice_values else 0.0
    rest_mean = statistics.fmean(rest_values) if rest_values else 0.0
    _, p = welch(slice_values, rest_values)
    return slice_mean, rest_mean, slice_mean - rest_mean, p


def detect_ordinal_decay(
    features: Sequence[TradeFeatures], unit: str
) -> Finding | None:
    """Does your edge survive the third trade of the day?

    This is the pattern of a good morning turned into a bad afternoon: the
    setups you waited for come first, and everything after them is boredom.
    """
    by_ordinal: dict[int, list[float]] = defaultdict(list)
    for feature in features:
        by_ordinal[min(feature.ordinal, 6)].append(feature.value)
    if len(by_ordinal) < 2:
        return None

    best_cut = None
    comparisons = 0
    for cut in range(1, 6):
        early = [f.value for f in features if f.ordinal <= cut]
        late = [f.value for f in features if f.ordinal > cut]
        if len(early) < MIN_SLICE or len(late) < MIN_SLICE:
            continue
        comparisons += 1
        early_mean, late_mean, _, p = _compare(early, late)
        if early_mean > 0 >= late_mean:
            effect = late_mean - early_mean
            candidate = (cut, early_mean, late_mean, effect, p, len(late))
            if best_cut is None or effect < best_cut[3]:
                best_cut = candidate
    if best_cut is None:
        return None

    cut, early_mean, late_mean, effect, p, late_n = best_cut
    p = adjust_p(p, comparisons)
    early_n = len(features) - late_n
    confidence = _confidence(late_n, early_n, p)
    threshold = 0.3 if unit == "R" else 40.0
    ordinal_table = {
        key: round(statistics.fmean(values), 3)
        for key, values in sorted(by_ordinal.items())
    }
    return Finding(
        code="ordinal_decay",
        severity=_severity(confidence, effect, threshold),
        headline=(
            f"Your first {cut} trade(s) each day make money; everything after "
            f"them loses it"
        ),
        detail=(
            f"Trades 1-{cut} average {early_mean:+.2f}{unit} over {early_n} trades. "
            f"Trade {cut + 1} onward averages {late_mean:+.2f}{unit} over {late_n} "
            f"trades -- a swing of {effect:+.2f}{unit} per trade. "
            f"Per-ordinal averages: {ordinal_table}"
        ),
        suggestion=(
            f"Cap the day at {cut} trade(s). Set max_trades_per_day={cut} so the "
            f"decision is made before the session starts, not after two losses."
        ),
        sample=late_n,
        confidence=confidence,
        effect=effect,
        p_value=p,
        guardrail={"max_trades_per_day": cut},
        evidence={"by_ordinal": ordinal_table},
    )


def detect_time_of_day(
    features: Sequence[TradeFeatures], unit: str, session: SessionSpec
) -> Finding | None:
    """Is there an hour after which you should simply go do something else?"""
    by_hour: dict[int, list[float]] = defaultdict(list)
    for feature in features:
        by_hour[feature.hour].append(feature.value)
    if len(by_hour) < 2:
        return None

    hours = sorted(by_hour)
    best = None
    comparisons = 0
    for cutoff in hours[1:]:
        before = [f.value for f in features if f.hour < cutoff]
        after = [f.value for f in features if f.hour >= cutoff]
        if len(before) < MIN_SLICE or len(after) < MIN_SLICE:
            continue
        comparisons += 1
        before_mean, after_mean, _, p = _compare(before, after)
        if before_mean > 0 >= after_mean:
            effect = after_mean - before_mean
            if best is None or effect < best[3]:
                best = (cutoff, before_mean, after_mean, effect, p, len(after), len(before))
    if best is None:
        return None

    cutoff, before_mean, after_mean, effect, p, after_n, before_n = best
    p = adjust_p(p, comparisons)
    confidence = _confidence(after_n, before_n, p)
    threshold = 0.3 if unit == "R" else 40.0
    hour_table = {
        f"{hour:02d}:00": round(statistics.fmean(values), 3)
        for hour, values in sorted(by_hour.items())
    }
    return Finding(
        code="time_of_day_decay",
        severity=_severity(confidence, effect, threshold),
        headline=f"Everything you trade from {cutoff:02d}:00 onward loses money",
        detail=(
            f"Before {cutoff:02d}:00 you average {before_mean:+.2f}{unit} "
            f"({before_n} trades). From {cutoff:02d}:00 you average "
            f"{after_mean:+.2f}{unit} ({after_n} trades). "
            f"By hour: {hour_table}"
        ),
        suggestion=(
            f"Set a hard stop time of {cutoff:02d}:00 ET -- flatten, close the "
            f"platform, and let the afternoon go. Treat it as a rule, not a "
            f"preference you renegotiate at {cutoff:02d}:05."
        ),
        sample=after_n,
        confidence=confidence,
        effect=effect,
        p_value=p,
        guardrail={"hard_stop_time": time(cutoff, 0)},
        evidence={"by_hour": hour_table},
    )


def detect_revenge_window(
    features: Sequence[TradeFeatures], unit: str
) -> Finding | None:
    """Trades taken right after a loss -- the ones you take to get it back."""
    best = None
    comparisons = 0
    for window in (5, 10, 15, 30):
        inside = [
            f.value
            for f in features
            if f.prior_was_loss
            and f.minutes_since_prior_exit is not None
            and f.minutes_since_prior_exit <= window
        ]
        outside = [
            f.value
            for f in features
            if not (
                f.prior_was_loss
                and f.minutes_since_prior_exit is not None
                and f.minutes_since_prior_exit <= window
            )
        ]
        if len(inside) < MIN_SLICE or len(outside) < MIN_SLICE:
            continue
        comparisons += 1
        inside_mean, outside_mean, effect, p = _compare(inside, outside)
        if effect < 0 and (best is None or effect < best[3]):
            best = (window, inside_mean, outside_mean, effect, p, len(inside), len(outside))
    if best is None:
        return None

    window, inside_mean, outside_mean, effect, p, inside_n, outside_n = best
    p = adjust_p(p, comparisons)
    confidence = _confidence(inside_n, outside_n, p)
    threshold = 0.3 if unit == "R" else 40.0
    suggested_cooldown = max(window, 15)
    return Finding(
        code="revenge_window",
        severity=_severity(confidence, effect, threshold),
        headline=f"Re-entering within {window} minutes of a loss costs you",
        detail=(
            f"Trades taken inside {window} minutes of a losing exit average "
            f"{inside_mean:+.2f}{unit} ({inside_n} trades) against "
            f"{outside_mean:+.2f}{unit} for everything else ({outside_n} trades)."
        ),
        suggestion=(
            f"Enforce a {suggested_cooldown}-minute cooldown after any loss. "
            f"The RiskManager can hold the lockout for you so it is not a "
            f"decision you make while annoyed."
        ),
        sample=inside_n,
        confidence=confidence,
        effect=effect,
        p_value=p,
        guardrail={"cooldown_minutes": suggested_cooldown, "max_consecutive_losses": 2},
        evidence={"window_minutes": window},
    )


def detect_loss_streak(features: Sequence[TradeFeatures], unit: str) -> Finding | None:
    """What happens on the trade after two or three straight losses."""
    for streak in (3, 2):
        inside = [f.value for f in features if f.losses_before_today >= streak]
        outside = [f.value for f in features if f.losses_before_today < streak]
        if len(inside) < MIN_SLICE or len(outside) < MIN_SLICE:
            continue
        inside_mean, outside_mean, effect, p = _compare(inside, outside)
        if effect >= 0:
            continue
        confidence = _confidence(len(inside), len(outside), p)
        threshold = 0.3 if unit == "R" else 40.0
        return Finding(
            code="loss_streak",
            severity=_severity(confidence, effect, threshold),
            headline=f"Trading on after {streak} straight losses makes it worse",
            detail=(
                f"Trades taken while already down {streak}+ in a row average "
                f"{inside_mean:+.2f}{unit} ({len(inside)} trades) versus "
                f"{outside_mean:+.2f}{unit} otherwise ({len(outside)} trades)."
            ),
            suggestion=(
                f"Stop for the day at {streak} consecutive losses. Set "
                f"max_consecutive_losses={streak} with consecutive_loss_action="
                f"'lock_day'."
            ),
            sample=len(inside),
            confidence=confidence,
            effect=effect,
            p_value=p,
            guardrail={
                "max_consecutive_losses": streak,
                "consecutive_loss_action": "lock_day",
            },
        )
    return None


def detect_size_escalation(
    features: Sequence[TradeFeatures], unit: str
) -> Finding | None:
    """Do you size up after a loss? That is the expensive direction to be wrong."""
    after_loss = [float(f.contracts) for f in features if f.prior_was_loss]
    after_other = [float(f.contracts) for f in features if not f.prior_was_loss]
    if len(after_loss) < MIN_SLICE or len(after_other) < MIN_SLICE:
        return None
    loss_mean, other_mean, effect, p = _compare(after_loss, after_other)
    if effect <= 0.15:  # sizing up only matters if it is actually happening
        return None
    confidence = _confidence(len(after_loss), len(after_other), p)
    results_after_loss = [f.value for f in features if f.prior_was_loss]
    result_mean = statistics.fmean(results_after_loss) if results_after_loss else 0.0
    return Finding(
        code="size_escalation",
        severity=_severity_by_confidence(confidence),
        headline="You increase size after a loss",
        detail=(
            f"Average size after a loss is {loss_mean:.2f} contracts versus "
            f"{other_mean:.2f} otherwise (+{effect:.2f}). Those trades average "
            f"{result_mean:+.2f}{unit}. Sizing up to recover a loss is the "
            f"mechanism behind most account-ending days."
        ),
        suggestion=(
            "Fix size to the risk rule, not to how you feel. Fixed-fractional "
            "sizing off the stop distance removes the decision entirely; if you "
            "want a manual override, make it downward only."
        ),
        sample=len(after_loss),
        confidence=confidence,
        effect=effect,
        effect_unit=" contracts",
        p_value=p,
        guardrail={},
        evidence={"avg_size_after_loss": loss_mean, "avg_size_otherwise": other_mean},
    )


def detect_overtrading(
    features: Sequence[TradeFeatures], days: Sequence[DayRecord], unit: str
) -> Finding | None:
    """Are your busy days your losing days?"""
    if len(days) < 8:
        return None
    counts = sorted({record.trades for record in days})
    if len(counts) < 2:
        return None
    best = None
    comparisons = 0
    for threshold_count in counts[1:]:
        busy = [r.net_pnl for r in days if r.trades >= threshold_count]
        quiet = [r.net_pnl for r in days if r.trades < threshold_count]
        if len(busy) < 4 or len(quiet) < 4:
            continue
        comparisons += 1
        busy_mean, quiet_mean, effect, p = _compare(busy, quiet)
        if busy_mean < 0 <= quiet_mean and (best is None or effect < best[3]):
            best = (threshold_count, busy_mean, quiet_mean, effect, p, len(busy), len(quiet))
    if best is None:
        return None

    threshold_count, busy_mean, quiet_mean, effect, p, busy_n, quiet_n = best
    p = adjust_p(p, comparisons)
    confidence = _confidence(busy_n, quiet_n, p)
    return Finding(
        code="overtrading",
        severity=_severity_by_confidence(confidence),
        headline=f"Days with {threshold_count}+ trades are your losing days",
        detail=(
            f"Days with {threshold_count} or more trades average "
            f"${busy_mean:,.2f} ({busy_n} days). Days with fewer average "
            f"${quiet_mean:,.2f} ({quiet_n} days)."
        ),
        suggestion=(
            f"Cap the day at {threshold_count - 1} "
            f"{'trade' if threshold_count - 1 == 1 else 'trades'}. Volume is not "
            f"the same as opportunity -- the extra trades are the ones you go "
            f"looking for once the good ones are gone."
        ),
        sample=busy_n,
        confidence=confidence,
        effect=effect,
        effect_unit="/day",
        p_value=p,
        guardrail={"max_trades_per_day": max(1, threshold_count - 1)},
    )


def detect_giveback(days: Sequence[DayRecord]) -> Finding | None:
    """Green days that end red -- the trap of not banking a good start."""
    if len(days) < 8:
        return None
    green = [record for record in days if record.peak_pnl > 0]
    if len(green) < 5:
        return None
    reversed_days = [record for record in green if record.net_pnl <= 0]
    if len(reversed_days) < 3:
        return None

    share = len(reversed_days) / len(green)
    # A trader with roughly flat expectancy produces green-days-that-finish-red
    # arithmetically, so the bar here is set above that baseline.
    if share < 0.35:
        return None

    total_given = sum(record.giveback for record in green)
    median_peak = statistics.median([record.peak_pnl for record in green])
    median_giveback = statistics.median([record.giveback for record in reversed_days])
    finished_red = sum(1 for record in reversed_days if record.net_pnl < 0)

    confidence = (
        "strong" if len(green) >= 20 and share >= 0.45
        else "moderate" if len(green) >= 12
        else "low"
    )
    # Cap the give-back at half the peak: tight enough to save most of a good
    # day, loose enough that ordinary noise does not end the session.
    allowed_giveback = 50.0
    return Finding(
        code="giveback",
        severity=_severity_by_confidence(confidence),
        headline=(
            f"{len(reversed_days)} of {len(green)} green days finished flat or red"
        ),
        detail=(
            f"On days you got ahead, you ended at or below zero {share:.0%} of the "
            f"time -- a median ${median_giveback:,.2f} handed back on those days, "
            f"and {finished_red} of them closed outright red. Median peak on a green "
            f"day was ${median_peak:,.2f}; across all green days ${total_given:,.2f} "
            f"went back from the high-water mark. Read this one alongside your "
            f"time-of-day and trade-count findings -- some give-back is just what a "
            f"flat edge looks like, but give-back plus a dead afternoon is a habit."
        ),
        suggestion=(
            f"Set max_daily_giveback_pct={allowed_giveback:.0f} so the session ends "
            f"once you have returned half of the day's peak. The point is not the "
            f"exact number -- it is that a good morning stops being something you "
            f"can undo in the afternoon."
        ),
        sample=len(green),
        confidence=confidence,
        effect=share,
        effect_unit=" of green days",
        p_value=None,
        guardrail={"max_daily_giveback_pct": allowed_giveback},
        evidence={
            "green_days": len(green),
            "reversed_days": len(reversed_days),
            "median_peak": round(median_peak, 2),
            "total_given_back": round(total_given, 2),
        },
    )


def detect_stop_discipline(
    features: Sequence[TradeFeatures], unit: str
) -> Finding | None:
    """Losses bigger than 1R mean the stop moved, or was never in the market.

    Measured as the *share* of losers past 1.15R rather than the average, so
    that taking sensible partial losses does not mask the few trades where the
    stop was abandoned -- which is where the damage is.
    """
    if unit != "R":
        return None
    losers = [
        feature.trade.r_multiple
        for feature in features
        if feature.is_loss and feature.trade.r_multiple is not None
    ]
    if len(losers) < MIN_SLICE * 2:
        return None

    beyond = [value for value in losers if value < -1.15]
    share = len(beyond) / len(losers)
    if share < 0.15:  # a little past 1R is slippage and commission, not behavior
        return None

    average = statistics.fmean(losers)
    worst = min(losers)
    confidence = "strong" if len(losers) >= STRONG_SLICE and share >= 0.25 else "moderate"
    return Finding(
        code="stop_discipline",
        severity="act" if confidence == "strong" else "watch",
        headline=f"{share:.0%} of your losses ran past the stop you planned",
        detail=(
            f"{len(beyond)} of {len(losers)} losing trades lost more than 1.15R; "
            f"the average loser is {average:.2f}R and the worst was {worst:.2f}R. "
            f"Either the stop is being moved as price approaches it, or it is a "
            f"mental stop that is not resting in the market."
        ),
        suggestion=(
            "Submit the protective stop as a resting order in the same action as "
            "the entry, so honoring it is not a decision you make later. If the "
            "overshoot is genuinely slippage, move the stop off the obvious round "
            "number and widen it a few ticks -- then take the smaller size that "
            "implies."
        ),
        sample=len(losers),
        confidence=confidence,
        effect=share,
        effect_unit=" of losers",
        p_value=None,
        evidence={
            "losers_past_1_15r": len(beyond),
            "avg_loser_r": round(average, 3),
            "worst_loser_r": round(worst, 3),
        },
    )


def detect_winners_cut_short(
    features: Sequence[TradeFeatures], unit: str
) -> Finding | None:
    """Trades that were up a full R and still came back as losses."""
    if unit != "R":
        return None
    losers = [f.trade for f in features if f.is_loss and f.trade.mfe_r is not None]
    if len(losers) < MIN_SLICE * 2:
        return None
    reached_1r = [trade for trade in losers if (trade.mfe_r or 0) >= 1.0]
    share = len(reached_1r) / len(losers)
    # Some losers going green first is normal. Only an unusually high share
    # suggests the exit, rather than the entry, is what needs work.
    if share < 0.35:
        return None
    average_mfe = statistics.fmean([trade.mfe_r or 0 for trade in losers])
    confidence = "strong" if len(losers) >= STRONG_SLICE and share >= 0.5 else "moderate"
    return Finding(
        code="winners_round_trip",
        severity="watch" if share >= 0.35 else "info",
        headline=f"{share:.0%} of your losses were up 1R or more first",
        detail=(
            f"{len(reached_1r)} of {len(losers)} losing trades reached at least "
            f"+1R before reversing; the average losing trade got to "
            f"{average_mfe:+.2f}R in your favor first."
        ),
        suggestion=(
            "Test a rule at +1R: move the stop to breakeven, or take half off and "
            "let the rest run. Backtest it before adopting it -- a breakeven stop "
            "converts these round trips into scratches, but it also stops you out "
            "of winners that needed room, and that trade-off can be net negative."
        ),
        sample=len(losers),
        confidence=confidence,
        effect=share,
        effect_unit=" of losers",
        p_value=None,
        guardrail={},
        evidence={"losers_reaching_1r": len(reached_1r), "avg_mfe_r": average_mfe},
    )


def detect_unplanned(features: Sequence[TradeFeatures], unit: str) -> Finding | None:
    """Trades you marked as off-plan versus the ones you actually waited for."""
    unplanned = [f.value for f in features if "unplanned" in f.trade.tags]
    planned = [f.value for f in features if "unplanned" not in f.trade.tags]
    if len(unplanned) < MIN_SLICE or len(planned) < MIN_SLICE:
        return None
    unplanned_mean, planned_mean, effect, p = _compare(unplanned, planned)
    if effect >= 0:
        return None
    confidence = _confidence(len(unplanned), len(planned), p)
    threshold = 0.3 if unit == "R" else 40.0
    share = len(unplanned) / len(features)
    return Finding(
        code="unplanned_trades",
        severity=_severity(confidence, effect, threshold),
        headline=f"Off-plan trades are {share:.0%} of your volume and lose money",
        detail=(
            f"Trades you marked unplanned average {unplanned_mean:+.2f}{unit} "
            f"({len(unplanned)} trades) against {planned_mean:+.2f}{unit} for "
            f"planned ones ({len(planned)} trades)."
        ),
        suggestion=(
            "Write the setup down before entry -- if you cannot name it in a "
            "few words, it is not a trade. The value here is that you are "
            "already tagging them honestly; now act on the tag."
        ),
        sample=len(unplanned),
        confidence=confidence,
        effect=effect,
        p_value=p,
    )


def _worst_bucket(
    features: Sequence[TradeFeatures],
    key: Callable[[TradeFeatures], object],
    unit: str,
    code: str,
    label: str,
) -> Finding | None:
    buckets: dict[object, list[float]] = defaultdict(list)
    for feature in features:
        buckets[key(feature)].append(feature.value)
    candidates = {
        name: values for name, values in buckets.items() if len(values) >= MIN_SLICE
    }
    if len(candidates) < 2:
        return None
    worst_name = min(candidates, key=lambda name: statistics.fmean(candidates[name]))
    worst = candidates[worst_name]
    rest = [
        value
        for name, values in candidates.items()
        if name != worst_name
        for value in values
    ]
    if not rest:
        return None
    worst_mean, rest_mean, effect, p = _compare(worst, rest)
    if worst_mean >= 0 or effect >= 0:
        return None
    # The worst of N buckets was *selected* for being worst, so correct for N.
    p = adjust_p(p, len(candidates))
    confidence = _confidence(len(worst), len(rest), p)
    threshold = 0.3 if unit == "R" else 40.0
    table = {
        str(name): round(statistics.fmean(values), 3)
        for name, values in sorted(candidates.items(), key=lambda kv: str(kv[0]))
    }
    return Finding(
        code=code,
        severity=_severity(confidence, effect, threshold),
        headline=f"{label} {worst_name} is your worst bucket",
        detail=(
            f"{label} {worst_name} averages {worst_mean:+.2f}{unit} over "
            f"{len(worst)} trades, against {rest_mean:+.2f}{unit} elsewhere. "
            f"All buckets: {table}"
        ),
        suggestion=(
            f"Before dropping it, check the sample -- {len(worst)} trades is a "
            f"thin basis. If it holds up over another month, stop trading "
            f"{worst_name} or halve size there."
        ),
        sample=len(worst),
        confidence=confidence,
        effect=effect,
        p_value=p,
        evidence={"buckets": table},
    )


WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Sat", "Sun")


def analyze(
    trades: Sequence[Trade],
    session: SessionSpec = DEFAULT_SESSION,
    min_trades: int = 20,
) -> BehaviorReport:
    """Run every detector over a set of closed trades."""
    features, unit, notes = build_features(trades, session)
    days = build_days(features)

    if len(features) < min_trades:
        notes.append(
            f"{len(features)} closed trades is below the {min_trades}-trade "
            "minimum; detectors were not run."
        )
        return BehaviorReport([], unit, len(features), len(days), days, features, notes)

    findings = [
        detect_ordinal_decay(features, unit),
        detect_time_of_day(features, unit, session),
        detect_revenge_window(features, unit),
        detect_loss_streak(features, unit),
        detect_size_escalation(features, unit),
        detect_overtrading(features, days, unit),
        detect_giveback(days),
        detect_stop_discipline(features, unit),
        detect_winners_cut_short(features, unit),
        detect_unplanned(features, unit),
        _worst_bucket(
            features, lambda f: WEEKDAY_NAMES[f.weekday], unit, "weekday", "Weekday"
        ),
        _worst_bucket(
            features,
            lambda f: f.trade.direction.value,
            unit,
            "direction_bias",
            "Direction",
        ),
        _worst_bucket(
            features,
            lambda f: f.trade.setup or "(untagged)",
            unit,
            "setup_leak",
            "Setup",
        ),
    ]
    findings = [finding for finding in findings if finding is not None]
    findings.sort(
        key=lambda f: (
            SEVERITY_ORDER[f.severity],
            CONFIDENCE_ORDER[f.confidence],
            -f.sample,
        )
    )
    return BehaviorReport(findings, unit, len(features), len(days), days, features, notes)
