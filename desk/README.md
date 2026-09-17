# YM Desk

A local hub for your trading statements. Drop in a statement, see what the parser
made of it before anything is stored, and get the performance and behavioral
analysis from [`../trading`](../trading) rendered as a page rather than a
terminal dump.

It runs as a small server on your own machine and opens in your browser. Nothing
leaves the computer.

```bash
cd desk
pip install -r requirements.txt
python run.py                 # or: python -m hub
```

It prints where your data lives and opens `http://127.0.0.1:8787`.

## What it does today

| Screen | What it is for |
|---|---|
| **Dashboard** | Headline numbers, equity curve, profit and loss by day. |
| **Statements** | Drag in a file, review the preview, decide whether to import. |
| **Analysis** | Expectancy per hour / weekday / setup / session, and the trade list. |
| **Behavior** | The findings from `ym.behavior`, with the guardrails they imply. |
| **Position size** | What a trade may cost, and how many contracts that buys. |
| **Settings** | Account size, risk limits, statement timezone, data folder. |

## Importing is a two-step operation, on purpose

Upload **previews**; it does not store. You see the format that was detected, the
first rows as the parser understood them, any rows it could not read, and whether
the file duplicates one you already imported. Only then do you import.

That separation is the point. A misread statement is far easier to reject than to
unpick from a journal afterwards — and when you do want to undo one, every trade
is tagged with the upload it came from, so deleting a statement removes exactly
its trades and nothing else.

**The one setting worth caring about is the default stop distance.** Statements
almost never record where your stop was, and without it a trade has no
R-multiple — which is most of what the behavioral analysis reads. Set it at
import time, or fill in stops per trade later.

### What it accepts

- **NinjaTrader trade exports** — Control Center → Trade Performance → Trades tab
  → right-click → Export.
- **Any trade CSV** with at least an entry time, a direction and an entry price.
  Column names are matched loosely; `;` and `,` both work.
- **Bar data** (NinjaTrader historical exports, OHLCV CSVs) is recognised and
  described, but not added to your journal — it is market history, for
  backtesting, not a record of what you did.
- **PDFs are refused** with instructions, rather than guessed at. Broker PDF
  parsing is a later job.

## Where your data lives

Everything sits in one folder — `~/.ym-desk` by default, or wherever
`YM_DESK_HOME` points:

```
~/.ym-desk/
├── journal.db        your trades (SQLite — the same journal the CLI uses)
├── uploads/          the statement files exactly as you uploaded them
├── statements.json   what was imported, when, and with which options
└── settings.json     account size, risk limits, timezone
```

Back it up by copying the folder. Start fresh by deleting it. Both files are
plain JSON so you can read — and when something goes wrong, fix — them in any
editor.

## Architecture

```
desk/
├── run.py                 double-click launcher
└── hub/
    ├── config.py          data paths; puts ../trading/ym on the import path
    ├── store.py           settings and the statement manifest (atomic JSON)
    ├── statements.py      what is this file, and what would importing it do
    ├── services.py        THE capability layer  ← the important one
    ├── app.py             FastAPI routes, a thin shell over services
    ├── __main__.py        launcher: pick a port, serve, open a browser
    └── static/            the page (no framework, no build step, no CDN)
```

**`services.py` is the piece that matters.** Every operation the app can perform
is one function there, taking plain arguments and returning JSON-able data. The
HTTP API is a wrapper over it. When the assistant arrives, its tools will be
wrappers over the same functions — so the model can only do what the app can do,
and anything you teach the app, the model gets for free. That is why the layer
exists instead of logic living in route handlers.

The trading engine is imported, never duplicated: `ym.journal`, `ym.metrics`,
`ym.behavior` and `ym.risk` do the actual work, so the hub and the CLI can never
disagree about what your expectancy is.

## Safety

- The server binds to **127.0.0.1 only**. Nothing on your network can reach it.
- Localhost alone does not stop a web page in another tab from posting to the
  app, so state-changing requests must carry a same-origin `Origin` header.
  Browsers set that header and cannot be told to forge it.
- Uploads are capped at 32 MB and stored under names the app chooses, so a
  crafted filename cannot escape the uploads folder.

## About the chart colours

Profit is **blue** and loss is **red**, not the conventional green/red. Red-green
is the classic colour-blind failure, and a P&L chart is exactly where it does
damage. The pair used here is validated for colour-vision deficiency in both
light and dark mode, and the sign is always carried by a label or a `-` as well
as by hue, so colour is never the only channel.

## Tests

```bash
cd desk
python -m unittest discover -s tests -t .     # 88 tests
```

They cover format detection, preview, the two-step import, reversibility,
the analysis contracts, settings persistence, every route, and the
cross-origin guard.

## Next

The assistant: a chat panel whose tools are the functions in `services.py`, with
an approval step that shows exactly what is about to be sent to the model before
each call.
