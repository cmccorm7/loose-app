# Live bars from NinjaTrader

Two NinjaScript indicators, either of which feeds YM Desk with bars as they
close. Install one, not both. They are deliberately small and do one thing:
hand over a finished bar.

| | `YmDeskBarFile` | `YmDeskBarHttp` |
|---|---|---|
| How | Appends to a CSV the app watches | Posts to the app on localhost |
| App closed | Bars keep accumulating; the app catches up | Bars are lost |
| Firewall | Nothing to configure | May prompt the first time |
| Debugging | Open the file in Notepad | Check NinjaTrader's Output window |
| Latency | A second or two | Immediate |
| Supported by NinjaTrader | Yes, plain file writing | Outbound HTTP is outside their supported use |

**Start with `YmDeskBarFile`.** It is forgiving about the app not running,
which is most of the time, and you can see exactly what it produced.

## Installing

1. In NinjaTrader: **New → NinjaScript Editor**
2. In the Editor's tree, right-click **Indicators → New Indicator**, give it any
   name, click through to Finish
3. Select all the generated code and paste one of these files over it
4. Press **F5** to compile. Errors appear at the bottom of the Editor
5. On an **MYM 1-minute chart**: right-click → **Indicators** → add
   `YmDeskBarFile` (or `YmDeskBarHttp`)

Leave the chart open while you trade. The indicator writes one line per closed
bar — one a minute, which is nothing.

## Where the bars go

`YmDeskBarFile` writes to `%USERPROFILE%\.ym-desk\live\` — beside the app's own
data — one file per instrument and period:

```
C:\Users\<you>\.ym-desk\live\MYM_1min.csv

symbol,datetime,open,high,low,close,volume
MYM,2026-09-28 09:30:00,41012,41045,41003,41038,2481
MYM,2026-09-28 09:31:00,41038,41050,41030,41033,1902
```

That is a normal CSV. If the live feed is ever a nuisance, you can import the
file by hand on the Statements screen exactly like any other bar export.

## Two things to get right

**Timezone.** Timestamps are written in whatever timezone NinjaTrader is
configured for, with no offset attached. The app's **Statement timezone**
setting has to match it, or every session boundary shifts silently. Check
NinjaTrader's setting under *Tools → Options → General → Time zone*.

**Port.** `YmDeskBarHttp` defaults to port 8787. If that port was busy, the app
picked another and printed it in its window — set the indicator's **Endpoint**
to match.

## Seeding history

`YmDeskBarFile` has an **Include historical bars** option, off by default. Turn
it on once, add the indicator to a chart with plenty of history loaded, and it
writes everything the chart holds — a quick way to backfill without a manual
export. Turn it off again afterwards, or every restart re-writes the lot.

Re-writing is harmless: the app stores bars keyed by symbol, timeframe and
time, so a bar imported twice replaces itself rather than duplicating.

## What it does not do

These feed bars only. They do not send your fills, place orders, or read
anything back from the app. Capturing trades live is a different NinjaScript
hook and a separate decision.
