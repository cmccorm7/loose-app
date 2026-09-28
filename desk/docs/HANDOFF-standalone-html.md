# Handoff — standalone HTML trading desk

Paste this whole file into a new chat as the opening message.

---

## Who I am and what I trade

I trade **Dow e-mini futures**, mostly **MYM** (Micro E-mini Dow, $0.50/point,
1.00-point tick). YM is the full-size contract at $5.00/point.

- My max risk is about **$10 per trade**, which on 1 MYM contract is a **20-point stop**.
- I trade **discretionary, at night**. I work days.
- I separately run an automated NinjaTrader strategy called `ThirdRejectionMYM`.
  **It is not in scope.** Do not write strategy code and do not touch it.
- My stated rule: go long when price hits a floor and ranges above it; entry trigger
  is an intraday higher high + higher low on the 5-minute; exit is breakeven at +40,
  then trail under each new 5-minute higher low.
- My known bad habits: taking $40–50 profits early, and bias creep — wanting to short
  above a floor that is holding.

A number I want kept in view, because it reframes everything: **a 20-point stop is
about 4.5% of a ~440-point median daily RTH range, and 96% of 5-minute bars have a
range exceeding 20 points.** An arbitrarily placed 20-point stop is a coin flip on
noise. It is only defensible directly under a level that is holding, where being
stopped means the level actually broke.

## How I want you to work

These are standing preferences, not suggestions:

1. **Explicit negative directives are absolute.** "Do not", "don't", "stop", "skip"
   are hard constraints. Ask rather than reinterpret.
2. **Clarify before assuming context.** Don't assume you know the project history.
3. **Distinguish information requests from action requests.** "Have we done X?" wants
   a yes/no, not a full audit.
4. **No scope expansion without permission.** Do only the task specified.
5. **Confirm understanding of scope** before starting: what you're doing, what you're
   NOT doing, what you're assuming.
6. **No assumptions about continuation.** Each step gets its own confirmation.
7. **Don't hallucinate.** If you can't verify a file or prior work, say so and ask.
8. **Flag contradictions** instead of resolving them yourself.
9. **Ask before edge cases** that could be read multiple ways.

Also: at each new prompt, check whether a fresh chat would keep token use lower.

## The task

Build a **single self-contained `.html` file** I can save to my computer, double-click,
and use offline. No install, no Python, no server, nothing leaving my machine.

**Agreed scope — journal + metrics + behavior:**

- Import my NinjaTrader statement CSV by drag-drop or file picker.
- Parse it into trades, persist them, and show **my** numbers in Account, Analysis and
  the trade list.
- Port the **12 behavior detectors** so they run on my real trades.
- Market context (swing detection, level tracking, trade tagging) is **explicitly out
  of scope for this pass.** Leave those panels on example data or hide them.

Do not port the backtester, the sweep tooling, or the strategies.

## What already exists in the repo

Branch: `claude/dow-emini-trading-system-dtndvt` in `cmccorm7/loose-app`.

Two directories that must sit side by side:

### `trading/ym/` — the engine (pure Python stdlib, 354 tests)

Read these before porting anything. The JS must match their behavior.

| File | What to take from it |
|---|---|
| `journal.py` | `parse_trade_csv()` — the CSV contract. **Port this first.** |
| `behavior.py` | `analyze()` and the 12 detectors. The whole behavior scope. |
| `metrics.py` | `compute_metrics`, `equity_curve`, `daily_pnl`, `group_by`, `GROUPERS` |
| `instruments.py` | `YM`, `MYM`, point values, tick rounding, `with_costs` |
| `sessions.py` | `session_day()` — 18:00 ET rollover, weekend skip. Not calendar days. |
| `core.py` | `Trade`, `Bar`, `Direction`, `ExitReason` |
| `risk.py` | `RiskManager` — position sizing and circuit breakers |

Out of scope this pass but present: `levels.py`, `market_context.py`,
`context_findings.py`, `barstore.py`, `backtest/`, `strategies/`, `sweep.py`, `coach.py`.

### `desk/` — the local app (FastAPI + plain HTML/CSS/JS, 152 tests)

`desk/hub/static/` is already framework-free HTML/CSS/JS, so UI work ports cleanly
between the app and the standalone file.

`desk/docs/ym-desk-mockup.html` — **the design mockup, already approved in look.**
Open it in a browser. It is a real standalone file with a proper `<!doctype>`.
Seven tabs, dark-first, IBM Plex Sans + IBM Plex Mono. Start from this markup and CSS
rather than designing fresh.

## Key details the port depends on

### The CSV contract (from `journal.py:363`)

`parse_trade_csv` reads NinjaTrader 8's Trade Performance grid export *and* the
backtester's own trade CSV. Behavior to reproduce exactly:

- Auto-detects delimiter: `;` if semicolons outnumber commas, else `,`.
- Reads as `utf-8-sig` (strips the BOM NinjaTrader writes).
- Header matching is case-insensitive with trailing `.` stripped, so `Market pos.`
  matches `market pos`.
- Column aliases:
  - entry_time ← `entry time`, `entry_time`, `datetime`, `date`
  - exit_time ← `exit time`, `exit_time`
  - direction ← `market pos`, `market position`, `direction`, `side`
  - quantity ← `quantity`, `contracts`, `qty`, `size`
  - entry ← `entry price`, `entry_price`
  - exit ← `exit price`, `exit_price`
  - symbol ← `instrument`, `symbol`
  - commission ← `commission`, `commissions`, `fees`
  - mae / mfe ← `mae`, `mae_points` / `mfe`, `mfe_points`
  - stop ← `stop_price`, `stop`, `stop price`
  - target ← `target_price`, `target`, `target price`
  - setup ← `setup`, `strategy`, then `entry name`, `signal`
  - reason ← `exit_reason`, `exit name`, `exit reason`
  - notes ← `notes`, `comment`
- Required: entry time, direction, entry price. Missing any → raise naming what was found.
- **A bad row becomes a warning, not an exception.** One bad line must not cost the file.
- **NinjaTrader does not export your stop**, so R-multiples are unavailable on import
  unless a `default_stop_points` is supplied or `stop_price` is filled in afterwards.
  For me that default is **20 points**. Surface this clearly — it is the difference
  between R-multiples working and not.

### The 12 behavior detectors (from `behavior.py:942` `analyze()`)

`detect_ordinal_decay`, `detect_time_of_day`, `detect_revenge_window`,
`detect_loss_streak`, `detect_size_escalation`, `detect_overtrading`,
`detect_giveback`, `detect_stop_discipline`, `detect_winners_cut_short`,
`detect_unplanned`, plus three `_worst_bucket` scans (weekday, direction bias,
setup leak).

Non-negotiable statistical details — these exist because the detectors originally
false-positived on a disciplined trader:

- `welch()` — Welch's t-test, with `_norm_cdf` for the p-value.
- **`adjust_p()` — Bonferroni correction on every scanning detector.** Do not drop this.
- Findings sort by `(severity, confidence, -sample)` — **not** by effect magnitude,
  because effects are in different units and aren't comparable.
- Minimum 20 closed trades before detectors run; below that, return a note explaining
  why nothing ran rather than running anyway.
- `winners_round_trip` threshold is 0.35; `giveback` threshold is 0.35.

### Session handling

The trading day rolls at **18:00 ET**, not midnight. Risk limits reset on the trading
day. `sessions.py:session_day()` has the rule including weekend skip. Naive datetimes
are rejected — everything is timezone-aware. My statements are `America/New_York`.

## Verified browser constraints

I tested these in Chromium in the prior session, so they are facts and not guesses:

- **`localStorage` works from a `file://` page and survives reload.** IndexedDB opens too.
- **But the origin is the bare `file://` bucket, shared with every other local HTML
  file.** Namespace every key (e.g. `ymdesk.trades.v1`) or a different local page can
  collide with it.
- Chrome's file-origin behavior varies by version and flag, and Safari is stricter.
  So: **always provide JSON export/import as the real backup.** Browser storage is a
  convenience, not the system of record. Wrap every read and write in try/catch and
  render correctly when it comes back empty.
- Downloads *do* work from a real local file in a browser (unlike inside an artifact
  sandbox), so an export button is fine.
- `FileReader`, file inputs and drag-drop all work — that's the import path.

## Design decisions already made

Don't relitigate these unless I ask:

- **Dark-first.** I trade at night. Ground `#0d1015`, panels `#151a22`, with a blue
  bias in the neutrals toward the accent.
- **Colour-vision-validated P&L pair: `#3987e5` / `#e66767`.** Not red/green — that is
  the classic accessibility failure and a P&L chart is where it does real damage.
  Validate with `node scripts/validate_palette.js` if you change it.
- **IBM Plex Sans for prose, IBM Plex Mono for every figure, label and tag.** That is
  the subject's own vernacular.
- **Risk state is chrome, not a page.** A persistent top rail carries equity, today's
  P&L, risk remaining and lockout status on every tab, so I never have to navigate to
  find out whether I'm allowed to take a trade.
- Three theme states handled at the token level: bare `:root` is dark, plus
  `@media (prefers-color-scheme: light)` guarded by `:not([data-theme="dark"])`, plus
  `:root[data-theme="light"]`.
- Example data must be **visibly marked as example data.** The mockup has a dashed
  warning banner saying none of the figures are mine. Keep that until real data loads,
  then it should disappear.

## What is NOT done and stays not done this pass

- Market context / trade tagging in the browser.
- The Claude chatbot layer. When it happens, `ai_share_mode` is **`"ask"`** — nothing
  goes to a model provider without an explicit per-time approval step.
- Live NinjaTrader price feed. `desk/ninjatrader/YmDeskBarFile.cs` exists but **I have
  not yet confirmed it compiles in NinjaTrader.**
- An open question I never answered: the trading window for the missed-setup
  scoreboard. I trade nights and work days; the proposal was to log all occurrences but
  have the headline count only a configurable window, e.g. 18:00–01:00.

## Where to start

Confirm the scope back to me first. Then I'd suggest: port `parse_trade_csv` and prove
it against a real statement before touching anything else, because if the import is
wrong every number downstream is wrong.
