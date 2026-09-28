"""Findings that need the bars: location, trend, stop size, profit-taking.

:mod:`ym.behavior` reads the journal alone -- when you traded, how often, what
happened after a loss. This module reads the journal *against the market*, and
so can ask the questions that were previously unanswerable: was the level
holding, were you with the structure, was your stop big enough to mean
anything, and how much of the move did you actually take.

Findings use the same :class:`~ym.behavior.Finding` shape, the same
multiple-comparison correction and the same confidence labels, so they render
beside the others and are held to the same standard of proof.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Sequence

from .behavior import (
    CONFIDENCE_ORDER, Finding, MIN_SLICE, SEVERITY_ORDER, STRONG_SLICE,
    _compare, _confidence, _severity, _severity_by_confidence, adjust_p,
)
from .core import Direction, Trade
from .market_context import AT_LEVEL, CHASING, EXIT_EARLY, TREND_UP, TradeContext


@dataclass
class Paired:
    """A closed trade beside what the market was doing when it was taken."""

    trade: Trade
    context: TradeContext
    value: float          # outcome in the report's unit


@dataclass
class ContextReport:
    findings: list[Finding]
    unit: str
    analysed: int
    unanalysed: int
    notes: list[str] = field(default_factory=list)
    headline: dict = field(default_factory=dict)

    def actionable(self) -> list[Finding]:
        return [f for f in self.findings if f.severity in ("act", "watch")]

    def format_report(self) -> str:
        lines = [
            "Market context review",
            "=" * 46,
            f"{self.analysed} trades read against the bars"
            + (f", {self.unanalysed} without bar data" if self.unanalysed else "")
            + f". Measured in {self.unit}.",
        ]
        for key, value in self.headline.items():
            lines.append(f"  {key:<34}{value}")
        lines += [f"note: {note}" for note in self.notes]
        if not self.findings:
            lines += ["", "Nothing cleared the minimum sample size yet."]
            return "\n".join(lines)
        lines += ["", "Findings, most actionable first:", ""]
        for finding in self.findings:
            lines.append(finding.format(self.unit))
            lines.append("")
        return "\n".join(lines)


# --------------------------------------------------------------------------

def pair(
    trades: Sequence[Trade], contexts: Sequence[TradeContext]
) -> tuple[list[Paired], str, list[str]]:
    """Match trades to their context and pick the unit to measure in."""
    by_id = {c.trade_id: c for c in contexts if c.trade_id is not None}
    closed = [t for t in trades if t.is_closed and t.trade_id in by_id]
    with_stops = [t for t in closed if t.r_multiple is not None]
    notes: list[str] = []
    if closed and len(with_stops) >= 0.8 * len(closed):
        unit = "R"
    else:
        unit = "$"
        if closed:
            notes.append(
                f"only {len(with_stops)}/{len(closed)} trades record a stop, so "
                "results are in dollars. Record stops to get R."
            )
    pairs = []
    for trade in closed:
        context = by_id[trade.trade_id]
        if not context.has_bars:
            continue
        value = trade.r_multiple if unit == "R" else trade.net_pnl
        pairs.append(Paired(trade, context, float(value if value is not None else 0.0)))
    return pairs, unit, notes


def _slice_finding(
    code: str, headline: str, detail: str, suggestion: str,
    inside: Sequence[float], outside: Sequence[float], unit: str,
    comparisons: int = 1, guardrail: dict | None = None, evidence: dict | None = None,
) -> Finding | None:
    """Compare one slice of trades against the rest, honestly."""
    if len(inside) < MIN_SLICE or len(outside) < MIN_SLICE:
        return None
    inside_mean, outside_mean, effect, p = _compare(inside, outside)
    p = adjust_p(p, comparisons)
    confidence = _confidence(len(inside), len(outside), p)
    threshold = 0.3 if unit == "R" else 40.0
    return Finding(
        code=code,
        severity=_severity(confidence, effect, threshold),
        headline=headline,
        detail=detail.format(
            inside=f"{inside_mean:+.2f}{unit}", outside=f"{outside_mean:+.2f}{unit}",
            n_in=len(inside), n_out=len(outside),
        ),
        suggestion=suggestion,
        sample=len(inside),
        confidence=confidence,
        effect=effect,
        p_value=p,
        guardrail=guardrail or {},
        evidence=evidence or {},
    )


# --- the stop, next to how far price travels ------------------------------

def detect_stop_vs_volatility(pairs: Sequence[Paired], unit: str) -> Finding | None:
    """Is the stop a statement about being wrong, or a coin flip on noise?"""
    ratios = [
        p.context.stop_vs_bar for p in pairs if p.context.stop_vs_bar is not None
    ]
    if len(ratios) < MIN_SLICE:
        return None
    median = statistics.median(ratios)
    if median >= 1.0:
        return None

    atrs = [p.context.stop_in_atr for p in pairs if p.context.stop_in_atr is not None]
    atr_median = statistics.median(atrs) if atrs else None
    stopped = sum(1 for p in pairs if p.context.exit_quality == "stopped")
    confidence = "strong" if len(ratios) >= STRONG_SLICE else "moderate"
    return Finding(
        code="stop_vs_volatility",
        severity="act",
        headline=(
            f"Your stop is smaller than a typical bar "
            f"({median:.2f}x the 5-minute range)"
        ),
        detail=(
            f"Across {len(ratios)} trades the median stop was {median:.2f} times "
            f"the median 5-minute bar range"
            + (f" and {atr_median:.2f} x ATR" if atr_median else "")
            + f". {stopped} of {len(pairs)} trades ended at the stop. A stop "
            f"inside the noise gets hit whether or not the read was right, so "
            f"those stop-outs carry almost no information about your entries."
        ),
        suggestion=(
            "Two ways out, and only two. Either place the stop where being hit "
            "means something -- beyond a level that is holding, which is what "
            "the location tag measures -- or take fewer, better trades so the "
            "risk per trade can be wider. Widening the stop without widening "
            "the account just means the same dollars lost more slowly."
        ),
        sample=len(ratios),
        confidence=confidence,
        effect=median,
        effect_unit="x bar range",
        evidence={
            "median_stop_vs_bar": round(median, 3),
            "median_stop_in_atr": None if atr_median is None else round(atr_median, 3),
            "stopped_out": stopped,
        },
    )


def detect_structural_risk(pairs: Sequence[Paired], unit: str) -> Finding | None:
    """How often was the stop actually placed beyond a level that had held?"""
    with_stops = [p for p in pairs if p.context.stop_points is not None]
    if len(with_stops) < MIN_SLICE:
        return None
    structural = [p for p in with_stops if p.context.stop_beyond_level]
    share = len(structural) / len(with_stops)
    if share >= 0.6:
        return None

    inside = [p.value for p in structural]
    outside = [p.value for p in with_stops if not p.context.stop_beyond_level]
    comparison = ""
    if len(inside) >= MIN_SLICE and len(outside) >= MIN_SLICE:
        inside_mean, outside_mean, _, _ = _compare(inside, outside)
        comparison = (
            f" Those that were average {inside_mean:+.2f}{unit}; the rest "
            f"{outside_mean:+.2f}{unit}."
        )
    confidence = "strong" if len(with_stops) >= STRONG_SLICE else "moderate"
    return Finding(
        code="structural_risk",
        severity=_severity_by_confidence(confidence),
        headline=f"Only {share:.0%} of your stops sat beyond a level that was holding",
        detail=(
            f"{len(structural)} of {len(with_stops)} trades placed the stop past a "
            f"level with at least two holds behind it.{comparison} The rest were "
            f"risking a fixed distance into open price."
        ),
        suggestion=(
            "Make the level the reason for the stop, not the number. If no level "
            "is close enough for your risk to sit beyond it, that is the setup "
            "telling you it is not your trade."
        ),
        sample=len(with_stops),
        confidence=confidence,
        effect=share,
        effect_unit=" structural",
        evidence={"structural": len(structural), "total": len(with_stops)},
    )


# --- location and trend ----------------------------------------------------

def detect_location_edge(pairs: Sequence[Paired], unit: str) -> Finding | None:
    """Do trades at a held level actually do better than the rest?"""
    at_level = [p.value for p in pairs if p.context.location == AT_LEVEL]
    elsewhere = [p.value for p in pairs if p.context.location != AT_LEVEL]
    if len(at_level) < MIN_SLICE or len(elsewhere) < MIN_SLICE:
        return None
    level_mean, other_mean, effect, p = _compare(at_level, elsewhere)
    p = adjust_p(p, 1)
    confidence = _confidence(len(at_level), len(elsewhere), p)
    better = effect > 0
    threshold = 0.3 if unit == "R" else 40.0
    return Finding(
        code="location_edge",
        severity=_severity(confidence, effect, threshold),
        headline=(
            "Trades at a held level do better than the rest" if better
            else "Trades at a held level do no better than the rest"
        ),
        detail=(
            f"At a level: {level_mean:+.2f}{unit} over {len(at_level)} trades. "
            f"Everywhere else: {other_mean:+.2f}{unit} over {len(elsewhere)}. "
            + (
                "That is the setup you say you want to trade, and the numbers "
                "back it." if better else
                "Worth knowing before you build rules around the setup -- on "
                "this sample it is not carrying you."
            )
        ),
        suggestion=(
            "Take only the trades where a level is close enough for the stop to "
            "sit beyond it, and skip the rest." if better else
            "Either the level rule needs tightening -- more holds, closer entry "
            "-- or the edge is somewhere else entirely. Check the trend and "
            "exit findings before rebuilding around levels."
        ),
        sample=len(at_level),
        confidence=confidence,
        effect=effect,
        p_value=p,
        evidence={"at_level": len(at_level), "elsewhere": len(elsewhere)},
    )


def detect_chasing(pairs: Sequence[Paired], unit: str) -> Finding | None:
    """Entries taken after the move had already run."""
    chasing = [p.value for p in pairs if p.context.location == CHASING]
    rest = [p.value for p in pairs if p.context.location != CHASING]
    return _slice_finding(
        code="chasing",
        headline="Entering after the move has already run costs you",
        detail=(
            "Trades entered more than a full move from the last swing average "
            "{inside} over {n_in} trades, against {outside} for the rest "
            "({n_out})."
        ),
        suggestion=(
            "The cost of chasing is the stop: by the time you are in, the level "
            "that would justify your risk is far behind you. Wait for the "
            "pullback or let it go -- there is another one."
        ),
        inside=chasing, outside=rest, unit=unit,
        evidence={"chasing": len(chasing)},
    )


def detect_trend_alignment(pairs: Sequence[Paired], unit: str) -> Finding | None:
    """With the 5-minute structure, or against it?"""
    with_trend = [p.value for p in pairs if p.context.with_trend is True]
    against = [p.value for p in pairs if p.context.with_trend is False]
    if len(with_trend) < MIN_SLICE or len(against) < MIN_SLICE:
        return None
    with_mean, against_mean, effect, p = _compare(against, with_trend)
    p = adjust_p(p, 1)
    confidence = _confidence(len(against), len(with_trend), p)
    threshold = 0.3 if unit == "R" else 40.0
    return Finding(
        code="trend_alignment",
        severity=_severity(confidence, effect, threshold),
        headline=(
            "Trading against the 5-minute structure costs you" if effect < 0
            else "Counter-trend entries are holding up"
        ),
        detail=(
            f"Against the trend: {with_mean:+.2f}{unit} over {len(against)} trades. "
            f"With it: {against_mean:+.2f}{unit} over {len(with_trend)}. "
            "Buying a level that is holding is counter-trend by construction -- "
            "price has to be falling to reach support -- so read this next to "
            "the location finding rather than on its own."
        ),
        suggestion=(
            "If the counter-trend trades that lose are also the ones away from a "
            "level, the problem is location, not direction. Your own rule already "
            "handles this: wait for the higher high and higher low that says the "
            "floor turned it."
        ),
        sample=len(against),
        confidence=confidence,
        effect=effect,
        p_value=p,
    )


# --- taking profit ---------------------------------------------------------

def detect_early_exits(pairs: Sequence[Paired], unit: str) -> Finding | None:
    """How much of the move did you actually take?"""
    winners = [
        p for p in pairs
        if p.context.capture_ratio is not None and p.context.realized_points > 0
    ]
    if len(winners) < MIN_SLICE:
        return None
    captures = [p.context.capture_ratio for p in winners]
    median = statistics.median(captures)
    early = [p for p in winners if p.context.exit_quality == EXIT_EARLY]
    share = len(early) / len(winners)
    if median >= 0.6 and share < 0.4:
        return None

    left = [
        p.context.forward_points for p in early if p.context.forward_points is not None
    ]
    median_left = statistics.median(left) if left else None
    confidence = "strong" if len(winners) >= STRONG_SLICE else "moderate"
    return Finding(
        code="early_exits",
        severity=_severity_by_confidence(confidence),
        headline=f"You take about {median:.0%} of the move on your winners",
        detail=(
            f"{len(early)} of {len(winners)} winning trades were followed by at "
            f"least as much again within the hour"
            + (f" -- a median {median_left:,.0f} further points" if median_left else "")
            + f". Median capture across all winners was {median:.0%}."
        ),
        suggestion=(
            "A fixed target cannot know how big the move is. Try the rule you "
            "already described: stop to breakeven once it has paid for itself, "
            "then trail under each new 5-minute higher low, and let the market "
            "decide the exit. Backtest it before adopting it -- trailing gives "
            "back some winners in exchange for the large ones."
        ),
        sample=len(winners),
        confidence=confidence,
        effect=median,
        effect_unit=" of the move",
        evidence={
            "median_capture": round(median, 3),
            "early_share": round(share, 3),
            "median_points_left": None if median_left is None else round(median_left, 1),
        },
    )


# --- do you trade your own rule? -------------------------------------------

def detect_rule_adherence(pairs: Sequence[Paired], unit: str) -> Finding | None:
    """The stated rule: long, at a level that held, once structure turned up."""
    if len(pairs) < MIN_SLICE * 2:
        return None
    on_rule = [
        p for p in pairs
        if p.trade.direction is Direction.LONG
        and p.context.location == AT_LEVEL
        and p.context.trend == TREND_UP
    ]
    at_level_early = [
        p for p in pairs
        if p.trade.direction is Direction.LONG
        and p.context.location == AT_LEVEL
        and p.context.trend != TREND_UP
    ]
    off_rule = [p for p in pairs if p not in on_rule and p not in at_level_early]
    share = len(on_rule) / len(pairs)

    detail = (
        f"{len(on_rule)} of {len(pairs)} trades were the setup as you describe it: "
        f"long, at a level that had held, with the 5-minute structure already "
        f"turned up. Another {len(at_level_early)} were long at a level but "
        f"before that confirmation, and {len(off_rule)} were something else."
    )
    if len(on_rule) >= MIN_SLICE and len(off_rule) >= MIN_SLICE:
        on_mean, off_mean, _, _ = _compare([p.value for p in on_rule],
                                           [p.value for p in off_rule])
        detail += (
            f" On-rule trades average {on_mean:+.2f}{unit}; everything else "
            f"{off_mean:+.2f}{unit}."
        )
    confidence = "moderate" if len(pairs) >= STRONG_SLICE else "low"
    return Finding(
        code="rule_adherence",
        severity=_severity_by_confidence(confidence if share < 0.5 else "low"),
        headline=f"{share:.0%} of your trades were the setup you say you trade",
        detail=detail,
        suggestion=(
            "The gap between the rule and the trades is the thing to close first "
            "-- no amount of tuning helps a rule you are not taking. If the "
            "on-rule trades are too few to judge, that is the finding: the setup "
            "is rarer than the urge to trade."
        ),
        sample=len(pairs),
        confidence=confidence,
        effect=share,
        effect_unit=" on-rule",
        evidence={
            "on_rule": len(on_rule),
            "at_level_unconfirmed": len(at_level_early),
            "off_rule": len(off_rule),
        },
    )


def detect_fighting_levels(pairs: Sequence[Paired], unit: str) -> Finding | None:
    """Shorting into a floor that is holding -- the bias creep he described."""
    # A floor only counts as one you are fighting if it is near enough to
    # matter -- there is always some support far below. Whether the trade also
    # chased is a separate question, so it does not exclude it here.
    # The floor beneath a short is the *opposing* level, not the one the trade
    # was taken against. It only counts as one you are fighting if it is near
    # enough to matter -- there is always some support far below.
    fighting = [
        p for p in pairs
        if p.trade.direction is Direction.SHORT
        and p.context.opposing_level_rejections >= 2
        and p.context.opposing_level_distance is not None
        and p.context.atr_points
        and p.context.opposing_level_distance <= 2.0 * p.context.atr_points
    ]
    if len(fighting) < MIN_SLICE:
        return None
    rest = [p.value for p in pairs if p not in fighting]
    values = [p.value for p in fighting]
    mean = statistics.fmean(values)
    detail = (
        f"{len(fighting)} trades were shorts taken above a floor that had already "
        f"held at least twice, averaging {mean:+.2f}{unit}."
    )
    if len(rest) >= MIN_SLICE:
        _, rest_mean, _, _ = _compare(values, rest)
        detail += f" The rest of the book averages {rest_mean:+.2f}{unit}."
    confidence = "moderate" if len(fighting) >= MIN_SLICE * 2 else "low"
    return Finding(
        code="fighting_levels",
        severity=_severity_by_confidence(confidence),
        headline=f"{len(fighting)} shorts taken above a floor that was holding",
        detail=detail + (
            " This is the pattern you named: wanting to be short while price is "
            "ranging above a level it keeps refusing to break."
        ),
        suggestion=(
            "Make it mechanical rather than a matter of willpower: if a support "
            "level below you has two or more holds and has not broken, shorts "
            "are off. The level breaking is the signal, not your read of it."
        ),
        sample=len(fighting),
        confidence=confidence,
        effect=mean,
        evidence={"fighting": len(fighting)},
    )


DETECTORS = (
    detect_stop_vs_volatility,
    detect_structural_risk,
    detect_location_edge,
    detect_chasing,
    detect_trend_alignment,
    detect_early_exits,
    detect_rule_adherence,
    detect_fighting_levels,
)


def analyse(
    trades: Sequence[Trade],
    contexts: Sequence[TradeContext],
    min_trades: int = 12,
) -> ContextReport:
    """Run every context detector over a set of trades and their contexts."""
    pairs, unit, notes = pair(trades, contexts)
    unanalysed = sum(1 for c in contexts if not c.has_bars)

    headline: dict = {}
    if pairs:
        stops = [p.context.stop_vs_bar for p in pairs if p.context.stop_vs_bar]
        caps = [p.context.capture_ratio for p in pairs if p.context.capture_ratio]
        at_level = sum(1 for p in pairs if p.context.location == AT_LEVEL)
        headline = {
            "at a held level": f"{at_level}/{len(pairs)}",
            "stop vs a 5-min bar": (
                f"{statistics.median(stops):.2f}x" if stops else "n/a"
            ),
            "share of the move taken": (
                f"{statistics.median(caps):.0%}" if caps else "n/a"
            ),
        }

    if len(pairs) < min_trades:
        notes.append(
            f"{len(pairs)} trades have bar context, below the {min_trades} needed "
            "to look for patterns. Import bars covering more of your trades."
        )
        return ContextReport([], unit, len(pairs), unanalysed, notes, headline)

    findings = [detector(pairs, unit) for detector in DETECTORS]
    findings = [finding for finding in findings if finding is not None]
    findings.sort(
        key=lambda f: (
            SEVERITY_ORDER[f.severity], CONFIDENCE_ORDER[f.confidence], -f.sample
        )
    )
    return ContextReport(findings, unit, len(pairs), unanalysed, notes, headline)
