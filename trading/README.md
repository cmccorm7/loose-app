# YM — a risk and behavior desk for Dow e-mini futures

A Python toolkit for trading the CBOT Dow futures (**YM**, $5/point, and
**MYM**, $0.50/point) built around the two things that actually decide whether
a retail futures account survives: **how much each trade is allowed to cost**,
and **whether you keep trading after your edge has gone for the day**.

Four layers, sharing one set of rules:

| Layer | Module | What it does |
|---|---|---|
| **Risk** | `ym/risk.py` | Sizes every position from the stop distance, enforces daily/weekly loss limits, drawdown and give-back breakers, cooldowns, and a hard stop time. |
| **Backtest** | `ym/backtest/`, `ym/sweep.py` | Bar-by-bar engine with pessimistic fills — and it routes every signal through the *same* risk manager, so a strategy can't look good by taking trades your account would never have allowed. Parameter sweeps report standard errors and an out-of-sample half. |
| **Journal** | `ym/journal.py` | SQLite store for trades you really took, with a NinjaTrader importer. |
| **Behavior** | `ym/behavior.py`, `ym/coach.py` | Finds the patterns in *how* you trade — the afternoon fade, the revenge re-entry, the give-back — and turns them into guardrails and in-session nudges. |

No runtime dependencies beyond the standard library.

## What this is not

- **Not a broker connection.** Nothing here places, modifies or cancels an
  order. It informs decisions; you execute them in your platform.
- **Not financial advice, and not a strategy.** The two bundled strategies are
  worked examples of the interface, not recommendations.
- **Not a substitute for your own data.** The synthetic generator exists so the
  plumbing is testable on day one. A backtest on a random walk tells you your
  code works, nothing more.

## Quick start

```bash
cd trading
python -m ym demo          # the whole pipeline on simulated data
python examples/first_week.py   # the same thing, narrated
```

On Windows, `pip install tzdata` first — Python there has no timezone database,
and every session boundary in this project depends on one.

## 1. Risk: size follows the stop

You choose what a trade may cost. The distance to the stop then determines how
many contracts that buys — never the other way round.

```bash
python -m ym size --symbol YM --equity 25000 --entry 41000 --stop 40985
```

```
YM  E-mini Dow ($5) Futures
  entry 41000  stop 40985  (15 pts = 15 ticks = $75.00 per contract)

APPROVED  1 contract(s)  risking $75.00 (15 pts)
  - risk budget $125.00 / $75.00 per contract
```

Widen that stop to 30 points and the same account is refused the trade
outright, because one contract would now cost $150 against a $125 budget. That
refusal is the product working. The answer is a smaller contract (`--symbol
MYM`), a tighter stop, or no trade.

### The limits

Every field below is optional — set it to `None` to switch it off. Defaults are
deliberately conservative for a personal retail account.

| Limit | Default | What it does |
|---|---|---|
| `risk_per_trade_pct` | 0.5 | Percent of equity risked per trade. |
| `max_contracts` | 10 | Absolute size cap. |
| `max_daily_loss_pct` | 2.0 | Locks the day out when breached — **and shrinks the trades before it**, so the last trade of a bad day can't blow through the limit. |
| `max_drawdown_pct` | 10.0 | Peak-to-trough lockout that does *not* expire overnight. |
| `max_consecutive_losses` | 3 | Fires a `cooldown` or ends the day (`consecutive_loss_action`). |
| `max_daily_giveback_pct` | off | Ends the session once you've handed back this share of the day's peak profit. |
| `hard_stop_time` | off | No new entries at or after this exchange-local time. |
| `max_trades_per_day` | off | Volume cap. |
| `min_stop_ticks` / `max_stop_points` | 4 / off | Rejects absurd stops. |
| `min_reward_risk` | off | Refuses trades below a reward:risk floor. |
| `max_margin_utilization` | 0.5 | Share of equity allowed in day margin. |

Commissions, margins and session hours are *defaults, not facts* — they vary by
broker and change. Override them with `Instrument.with_costs()`.

## 2. Backtest

```bash
# your own NinjaTrader export
python -m ym backtest YM_1min.txt --strategy orb --symbol MYM --equity 25000

# tune the strategy
python -m ym backtest YM_1min.txt --strategy orb -p range_minutes=15 -p target_r=3.0
```

The engine is built to be pessimistic:

- **No lookahead.** A strategy sees bars up to the current close; its orders
  fill on later bars only.
- **The worse path.** A bar containing both your stop and your target is
  assumed to have hit the stop first (`--optimistic` to flip it).
- **Gaps cost.** Price opening through your stop fills at the open, not the stop.
- **Costs are real.** Slippage in ticks on every entry and exit, commission on
  every round turn.
- **Refused signals are reported, not dropped.** If the risk rules vetoed 80
  trades, the report says so and why — that's often the most useful output.

Three strategies ship as worked examples: `orb` (opening range breakout),
`ma_pullback`, and `support_rejection` — see below.

Writing your own means answering two questions. `Signal` to enter, `Manage`
to adjust or exit. Size is never the strategy's business:

```python
class OpenBreak(Strategy):
    name = "open-break"

    def on_bar(self, context):
        if context.minutes_since_open() < 30 or context.position:
            return None
        atr = context.atr(14)
        return Signal(
            direction=Direction.LONG,
            entry_type=EntryType.STOP,
            entry_price=context.highest(30) + context.instrument.tick_size,
            stop_points=1.5 * atr,
            target_r=2.0,
        )

    def manage(self, context, trade):
        if (context.bar.close - trade.entry_price) >= trade.risk_points:
            return Manage(new_stop=trade.entry_price)   # breakeven at +1R
```

## 3. Buying the Nth rejection of a support line

`support_rejection` is the "it has held three times, buy the next test" idea,
built so it can actually be tested rather than assumed.

```bash
# levels want a slower timeframe than 1-minute bars
python -m ym backtest YM_1min.txt --strategy support_rejection \
    --symbol MYM --rth-only --minutes 5
```

How the pattern is defined, all of it configurable:

- A **support line** is a cluster of confirmed swing lows within a tolerance
  band (`tolerance_atr`, default 0.35 × ATR, or `tolerance_points` for a fixed
  band). `ym/levels.py` maintains these bar by bar.
- A **rejection** is price trading into that band and closing back above the
  level, with `min_bars_between_touches` enforced so one long consolidation
  isn't counted as three separate tests.
- `min_rejections` counts **every** confirmed rejection, *including the swing
  low that drew the line*. So `min_rejections=3` means: a low, two more tests
  that held, and you buy the third.
- The **stop** goes below the level's *floor* — the lowest price it has actually
  traded to — not the line itself, which has already been hit by definition.
- `direction=short` mirrors the whole thing onto resistance.

Two things the implementation is careful about, because both would quietly
invent edge that isn't there:

**A level cannot exist before the bars that prove it.** A swing low at bar *j*
is only a swing low once `pivot_strength` further bars have printed, so the
level is created at bar *j + pivot_strength*, never at *j*. The bars in between
are then re-scanned for rejections — they are all in the past, so that is
honest, and it is what makes the forming low count as rejection one.

**`min_rejections=1` is refused unless you opt in.** Buying the line-forming low
means buying a bar you could not have identified at the time. Rather than
silently behaving like `min_rejections=2` — which would make a sweep of 1,2,3,4
report the first two as identical — it raises an error and tells you to pass
`max_bars_since_touch=3` if you really want that late entry.

### Does a level get stronger or weaker with each test?

Worth settling before you trade it. The folk wisdom says a level that has held
three times is well defended. The opposing argument is just as mechanical: each
test *consumes* the resting bids defending it, so the fourth test has less
underneath it than the first did.

This is exactly what a sweep is for:

```bash
python -m ym sweep YM_1min.txt --strategy support_rejection --symbol MYM \
    --rth-only --minutes 5 --sweep-param min_rejections --values 2,3,4,5 --split
```

```
FIRST HALF (in sample)
min_rejections     Trades   Win%     Net P&L    Exp R     +/-     PF  MaxDD%
---------------------------------------------------------------------------
2                     183  37.2%       94.00   +0.006   0.105   1.01    6.0%
3                     115  31.3%   -1,890.00   -0.138   0.128   0.79    9.5%
4                      61  36.1%      186.50   +0.005   0.184   1.04    3.9%

Reading this:
  Best in sample: min_rejections=2 (+0.006R).
  Out of sample it did -0.223R over 72 trades, ranking 2 of 4.
  It did not hold up. The in-sample ranking was most likely noise.
  No configuration's expectancy cleared two standard errors, so the
  differences between these rows are within noise.
```

(That run is on synthetic bars, which have no real support levels — a result of
roughly zero is the correct answer and a good check that the engine isn't
manufacturing edge. Your own data is the only run that means anything.)

## 4. Sweeping a parameter without fooling yourself

Running a strategy at four settings and keeping the best is not a test — it is
four chances to find noise. `ym sweep` is built to make that visible:

- **Every row carries a standard error** on its expectancy. `+0.08R ± 0.14` has
  told you nothing, wherever it ranks. A `*` marks rows more than two standard
  errors from zero.
- **`--split`** re-runs each value on an earlier and a later stretch of your
  data, cut on a session boundary, and reports whether the in-sample winner was
  still a winner out of sample. This is the cheapest protection against
  curve-fitting there is.
- The header states **how many configurations were tried**, because the best of
  eight looks better than the best of two for reasons unrelated to the market.

It works on any strategy and any constructor parameter:

```bash
python -m ym sweep bars.csv --strategy orb --sweep-param range_minutes \
    --values 15,30,45,60 --split
python -m ym sweep bars.csv --strategy support_rejection \
    --sweep-param target_r --values 1.5,2,3 -p min_rejections=3 --split
```

None of this makes a sweep safe. It makes a bad sweep legible.

## 5. Journal

The journal is what makes the behavioral layer possible, so two fields carry
most of the weight:

- **`stop_price`** — the *initial* stop. Without it there is no 1R, and half
  the analysis can't be computed. It is never rewritten when you trail a stop.
- **`planned`** — whether this was the setup you intended or something you
  talked yourself into. Be honest here; it's the most useful column in the
  database.

```bash
# NinjaTrader: Control Center > Trade Performance > Trades tab > right-click > Export
python -m ym journal import NinjaTrader_trades.csv --default-stop-points 30

# or by hand
python -m ym journal add --symbol MYM --direction long \
  --entry-time 2026-09-08T09:45 --entry 41000 --contracts 2 --stop 40975 \
  --exit-time 2026-09-08T10:05 --exit 41025 --setup ORB --reason target

python -m ym journal stats --equity 25000 --by hour --by weekday --by setup
```

NinjaTrader does not export your stop, so imported trades have no R-multiple
until you supply one (`--default-stop-points`, or `journal set <id> --stop`).

### Getting data out of NinjaTrader

*Bars* — `Tools > Historical Data > Export`, which writes semicolon-delimited
`yyyyMMdd HHmmss;O;H;L;C;V`. The loader sniffs that, comma-separated exports
with headers, split date/time columns, and US or ISO date formats:

```bash
python -m ym data info YM_1min.txt     # check what it decided before trusting it
```

The one thing it cannot sniff is the timezone. NinjaTrader exports in whatever
timezone the platform is set to; pass `--tz` if that isn't US Eastern. Getting
it wrong silently shifts every session boundary.

## 6. Behavior — the part that watches how you trade

```bash
python -m ym review --apply
```

```
!! Your first 2 trade(s) each day make money; everything after them loses it
     Trades 1-2 average +0.32R over 107 trades. Trade 3 onward averages
     -0.62R over 95 trades -- a swing of -0.95R per trade.
     Per-ordinal averages: {1: 0.41, 2: 0.22, 3: -0.38, 4: -0.77, 5: -0.97}
     -> Cap the day at 2 trade(s). Set max_trades_per_day=2 so the decision is
        made before the session starts, not after two losses.
     n=95  effect=-0.95R  p~0.000  confidence=strong
     guardrail: max_trades_per_day=2
```

Twelve detectors, each reporting an effect, a sample size, a corrected
p-value, and a **concrete change to your `RiskLimits`**:

| Detector | The trap |
|---|---|
| `ordinal_decay` | Your edge dies after the *n*th trade of the day. |
| `time_of_day_decay` | Everything after some hour loses money. |
| `revenge_window` | Re-entering within minutes of a loss. |
| `loss_streak` | Trading on after 2–3 straight losses. |
| `size_escalation` | Sizing *up* after a loss. |
| `overtrading` | Your busy days are your losing days. |
| `giveback` | Green days that finish red. |
| `stop_discipline` | Losses running past the stop you planned. |
| `winners_round_trip` | Losers that were up 1R first. |
| `unplanned_trades` | Off-plan trades, priced. |
| `weekday` / `direction_bias` / `setup_leak` | The worst bucket in each. |

`review --apply` merges the guardrails from the actionable findings into a
ready `RiskLimits`. Where two detectors disagree, the better-evidenced one
wins — not simply the tighter one.

### Then it tells you in the moment

A review you read on Sunday doesn't help at 13:05 on Wednesday. `coach`
replays today's journalled trades through your risk rules and checks them
against your profile:

```bash
python -m ym coach --symbol MYM --equity 25000 --max-trades 2 --hard-stop 13:00
```

```
STOP    This would be trade 4 today. Your edge historically stops after trade 2.
          (trade 4 historically averages -0.83R over 218 trades)
STOP    You took a loss 6 minutes ago. Your trades inside 30 minutes of a loss
        are your worst trades. Wait 24 more minutes.
          (they run -0.44R worse than the rest)
CAUTION  You are $28.00 off today's $80.00 peak (35% of it). Your give-back
         limit is 50%.
```

The coach only ever advises. Blocking is the risk manager's job, and it happens
on hard numbers.

### How much to trust a finding

These are hypotheses drawn from your own small sample, not laws. The module is
built to resist telling you flattering stories:

- Detectors that **scan** for their worst slice (which hour? which cutoff?) have
  many chances to find a pattern in noise, so their p-values carry a
  **Bonferroni correction** for the number of comparisons tried.
- Every finding reports its **sample size and confidence**. Only `act` and
  `watch` findings can set a guardrail; a short history sets none at all.
- Against a simulated *disciplined* trader, the suite raises roughly one
  moderate flag per two reviews — so expect about one false positive in any
  given review, and treat a lone `watch` finding as something to watch.

Thirty trades cannot distinguish a habit from a run of bad luck. The honest use
is to read the findings, pick the ones that match something you already
suspected about yourself, and guardrail that one.

## Project layout

```
trading/
├── ym/
│   ├── instruments.py    YM/MYM specs; all price→dollar conversion
│   ├── sessions.py       RTH/overnight, and the trading day (18:00 ET rollover)
│   ├── core.py           Bar and Trade — P&L, R-multiples, MAE/MFE
│   ├── risk.py           sizing, loss budgets, circuit breakers
│   ├── metrics.py        expectancy, profit factor, drawdown, breakdowns
│   ├── levels.py         support/resistance detection and rejection counting
│   ├── sweep.py          parameter sweeps with an out-of-sample split
│   ├── journal.py        SQLite journal + NinjaTrader import
│   ├── behavior.py       the twelve detectors and guardrail synthesis
│   ├── coach.py          in-session nudges
│   ├── cli.py            python -m ym
│   ├── backtest/         strategy interface and execution engine
│   ├── strategies/       three worked examples
│   └── data/             loaders, synthetic bars, simulated trader
├── tests/                279 tests, standard-library unittest
└── examples/first_week.py
```

Two details worth knowing because they cause silent, wrong answers elsewhere:

- **Timestamps are always timezone-aware.** Naive datetimes are rejected, not
  guessed at.
- **The trading day is not the calendar day.** It opens at 18:00 ET the previous
  evening, so a loss at 20:00 Monday counts against Tuesday's daily limit.

## Tests

```bash
cd trading
python -m unittest discover -s tests      # 279 tests, ~13 seconds
```

The behavioral tests assert both halves of the contract: detectors must fire on
planted habits **and** stay quiet on a disciplined trader. A detector that
always fires is worse than none, because it teaches you to ignore the report.
