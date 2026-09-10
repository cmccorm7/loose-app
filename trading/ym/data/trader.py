"""A simulated trading history with configurable bad habits.

Used by the tests, and useful for seeing what :mod:`ym.behavior` produces
before you have enough of your own trades for it to say anything. The habits
are knobs so you can confirm a detector actually fires on the behavior it
claims to detect -- and, just as importantly, stays quiet when the behavior is
absent.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..core import Direction, ExitReason, Trade
from ..instruments import Instrument, MYM
from ..sessions import DEFAULT_SESSION, SessionSpec


@dataclass
class Habits:
    """How the simulated trader misbehaves. Zero everything for a clean trader."""

    early_edge_r: float = 0.35       # expectancy on the first trades of the day
    late_edge_r: float = -0.45       # expectancy once the good setups are gone
    edge_decays_after: int = 2       # trade number at which the edge disappears
    revenge_probability: float = 0.6  # chance of re-entering fast after a loss
    revenge_penalty_r: float = -0.35  # extra damage on those revenge trades
    revenge_size_multiplier: float = 2.0
    trades_per_day: tuple[int, int] = (1, 6)
    stop_overshoot: float = 0.0      # extra R lost past the stop (mental stops)
    unplanned_probability: float = 0.0


def simulate_trader_history(
    start: date = date(2026, 5, 4),
    days: int = 60,
    habits: Habits | None = None,
    instrument: Instrument = MYM,
    stop_points: float = 25.0,
    base_contracts: int = 2,
    seed: int | None = 42,
    session: SessionSpec = DEFAULT_SESSION,
) -> list[Trade]:
    """Generate closed trades exhibiting ``habits``."""
    habits = habits or Habits()
    rng = random.Random(seed)
    tz = session.tz
    trades: list[Trade] = []
    day = start
    produced = 0

    while produced < days:
        if day.weekday() >= 5:
            day += timedelta(days=1)
            continue

        count = rng.randint(*habits.trades_per_day)
        clock = datetime.combine(day, session.rth_open, tzinfo=tz) + timedelta(
            minutes=rng.randint(1, 20)
        )
        previous_was_loss = False

        for ordinal in range(1, count + 1):
            revenge = previous_was_loss and rng.random() < habits.revenge_probability
            if revenge:
                clock += timedelta(minutes=rng.randint(2, 9))
            else:
                clock += timedelta(minutes=rng.randint(20, 70))
            if clock.time() >= session.rth_close:
                break

            edge = (
                habits.early_edge_r
                if ordinal <= habits.edge_decays_after
                else habits.late_edge_r
            )
            if revenge:
                edge += habits.revenge_penalty_r
            realized_r = rng.gauss(edge, 1.15)

            # Winners run to a target. Losers are mostly full stop-outs -- plus
            # any overshoot the habits ask for -- with some partial exits mixed
            # in, which is what a real record looks like.
            if realized_r <= 0:
                if rng.random() < 0.75:
                    realized_r = -1.0 - habits.stop_overshoot
                else:
                    realized_r = max(realized_r, -0.95)

            contracts = base_contracts
            if revenge:
                contracts = max(1, int(round(base_contracts * habits.revenge_size_multiplier)))

            direction = Direction.LONG if rng.random() < 0.55 else Direction.SHORT
            entry = 41000 + rng.gauss(0, 180)
            entry = instrument.round_to_tick(entry)
            stop = entry - direction.sign * stop_points
            commission = instrument.round_turn_commission(contracts)
            risk_dollars = stop_points * instrument.point_value * contracts
            exit_points = (realized_r * risk_dollars + commission) / (
                instrument.point_value * contracts
            )
            exit_price = instrument.round_to_tick(entry + direction.sign * exit_points)

            held = timedelta(minutes=rng.randint(4, 45))
            trade = Trade(
                symbol=instrument.symbol,
                direction=direction,
                entry_time=clock,
                entry_price=entry,
                contracts=contracts,
                point_value=instrument.point_value,
                stop_price=stop,
                target_price=entry + direction.sign * stop_points * 2,
                commission=commission,
                setup="revenge" if revenge else f"setup-{(ordinal - 1) % 3 + 1}",
            )
            favorable = max(0.0, realized_r) * stop_points
            adverse = max(0.0, -realized_r) * stop_points
            if realized_r > 0:
                adverse = max(adverse, rng.uniform(0.1, 0.6) * stop_points)
            else:
                favorable = max(favorable, rng.uniform(0.0, 1.0) * stop_points)
            trade.mfe_points = favorable
            trade.mae_points = min(adverse, stop_points * (1 + habits.stop_overshoot))
            trade.close(
                clock + held,
                exit_price,
                ExitReason.TARGET if realized_r > 0 else ExitReason.STOP,
            )
            if habits.unplanned_probability and rng.random() < habits.unplanned_probability:
                trade.tags.append("unplanned")
            trades.append(trade)

            clock += held
            previous_was_loss = trade.net_pnl < 0

        produced += 1
        day += timedelta(days=1)

    return trades
