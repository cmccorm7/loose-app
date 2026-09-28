"""The journal store and its importers."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

from ym.core import Direction, ExitReason, Trade
from ym.journal import Journal, parse_position_history
from ym.sessions import DEFAULT_SESSION, exchange_tz

ET = exchange_tz()

NT_TRADE_EXPORT = (
    "Trade number;Instrument;Account;Strategy;Market pos.;Quantity;Entry price;"
    "Exit price;Entry time;Exit time;Entry name;Exit name;Profit;Commission;MAE;MFE\n"
    "1;MYM 12-26;Sim101;;Long;2;41000.00;41025.00;9/8/2026 9:45:00;"
    "9/8/2026 10:05:00;ORB;Target;$25.00;$1.00;$12.00;$30.00\n"
    "2;MYM 12-26;Sim101;;Short;2;41010.00;41030.00;9/8/2026 11:00:00;"
    "9/8/2026 11:12:00;Fade;Stop;($20.00);$1.00;$22.00;$4.00\n"
)


def sample_trade(hour=9, minute=45, points=40, contracts=1, setup="ORB", day=8):
    entry_time = datetime(2026, 9, day, hour, minute, tzinfo=ET)
    trade = Trade("YM", Direction.LONG, entry_time, 41000.0, contracts, 5.0,
                  stop_price=40980.0, target_price=41040.0, commission=4.0,
                  setup=setup, notes="test note", tags=["a", "b"])
    trade.mae_points, trade.mfe_points = 8.0, 45.0
    trade.close(entry_time + timedelta(minutes=20), 41000.0 + points,
                ExitReason.TARGET if points > 0 else ExitReason.STOP)
    return trade


class JournalCase(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        os.unlink(handle.name)
        self.path = handle.name
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def write_csv(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                            encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name


class TestRoundTrip(JournalCase):
    def test_a_recorded_trade_comes_back_identical(self):
        original = sample_trade()
        with Journal(self.path) as journal:
            trade_id = journal.record(original)
            restored = journal.trades()[0]
        self.assertEqual(restored.trade_id, trade_id)
        self.assertEqual(restored.symbol, original.symbol)
        self.assertEqual(restored.direction, original.direction)
        self.assertEqual(restored.entry_time, original.entry_time)
        self.assertEqual(restored.exit_time, original.exit_time)
        self.assertAlmostEqual(restored.net_pnl, original.net_pnl)
        self.assertAlmostEqual(restored.r_multiple, original.r_multiple)
        self.assertEqual(restored.tags, original.tags)
        self.assertEqual(restored.setup, original.setup)
        self.assertIs(restored.exit_reason, ExitReason.TARGET)

    def test_the_database_persists_across_connections(self):
        with Journal(self.path) as journal:
            journal.record(sample_trade())
        with Journal(self.path) as journal:
            self.assertEqual(journal.count(), 1)

    def test_open_trades_are_listed_separately(self):
        open_trade = Trade("YM", Direction.LONG, datetime(2026, 9, 8, 10, tzinfo=ET),
                           41000, 1, 5.0, stop_price=40980)
        with Journal(self.path) as journal:
            journal.record(open_trade)
            journal.record(sample_trade())
            self.assertEqual(len(journal.trades()), 1)       # closed only
            self.assertEqual(len(journal.open_trades()), 1)

    def test_update_fills_in_a_missing_stop(self):
        trade = sample_trade()
        trade.stop_price = None
        with Journal(self.path) as journal:
            trade_id = journal.record(trade)
            self.assertIsNone(journal.trades()[0].r_multiple)
            journal.update(trade_id, stop_price=40980.0)
            self.assertIsNotNone(journal.trades()[0].r_multiple)

    def test_update_rejects_unknown_columns(self):
        with Journal(self.path) as journal:
            trade_id = journal.record(sample_trade())
            with self.assertRaises(KeyError):
                journal.update(trade_id, nonsense=1)

    def test_delete(self):
        with Journal(self.path) as journal:
            trade_id = journal.record(sample_trade())
            journal.delete(trade_id)
            self.assertEqual(journal.count(), 0)

    def test_unplanned_trades_are_tagged_on_the_way_out(self):
        with Journal(self.path) as journal:
            journal.record(sample_trade(), planned=False)
            self.assertIn("unplanned", journal.trades()[0].tags)
            self.assertEqual(journal.planned_ratio(), 0.0)


class TestFiltering(JournalCase):
    def setUp(self):
        super().setUp()
        self.journal = Journal(self.path)
        self.addCleanup(self.journal.close)
        for day, setup in ((8, "ORB"), (9, "ORB"), (10, "Fade")):
            self.journal.record(sample_trade(day=day, setup=setup))

    def test_filter_by_setup(self):
        self.assertEqual(len(self.journal.trades(setup="ORB")), 2)

    def test_filter_by_date_range(self):
        self.assertEqual(len(self.journal.trades(start="2026-09-09")), 2)
        self.assertEqual(len(self.journal.trades(end="2026-09-09")), 2)

    def test_filter_by_symbol(self):
        self.assertEqual(len(self.journal.trades(symbol="ym")), 3)
        self.assertEqual(len(self.journal.trades(symbol="MYM")), 0)

    def test_newest_first_still_returns_oldest_first(self):
        recent = self.journal.trades(limit=2, newest_first=True)
        self.assertEqual(len(recent), 2)
        self.assertLess(recent[0].entry_time, recent[1].entry_time)
        self.assertEqual(recent[-1].setup, "Fade")

    def test_metrics_come_from_the_shared_module(self):
        metrics = self.journal.metrics(25000)
        self.assertEqual(metrics.trades, 3)
        self.assertGreater(metrics.net_pnl, 0)

    def test_breakdown_by_setup(self):
        self.assertIn("ORB", self.journal.breakdown("setup"))


class TestNinjaTraderImport(JournalCase):
    def test_import_reads_positions_sizes_and_prices(self):
        path = self.write_csv(NT_TRADE_EXPORT)
        with Journal(self.path) as journal:
            imported, warnings = journal.import_csv(path)
            trades = journal.trades()
        self.assertEqual(imported, 2)
        self.assertEqual(warnings, [])
        self.assertEqual(trades[0].symbol, "MYM")
        self.assertIs(trades[0].direction, Direction.LONG)
        self.assertIs(trades[1].direction, Direction.SHORT)
        self.assertEqual(trades[0].contracts, 2)
        self.assertAlmostEqual(trades[0].entry_price, 41000.0)
        self.assertAlmostEqual(trades[0].net_pnl, 25.0 - 1.0)

    def test_entry_name_is_used_as_the_setup_when_strategy_is_blank(self):
        path = self.write_csv(NT_TRADE_EXPORT)
        with Journal(self.path) as journal:
            journal.import_csv(path)
            self.assertEqual(journal.trades()[0].setup, "ORB")

    def test_currency_excursions_are_converted_to_points(self):
        path = self.write_csv(NT_TRADE_EXPORT)
        with Journal(self.path) as journal:
            journal.import_csv(path, excursion_unit="currency")
            trade = journal.trades()[0]
        # $12 of MAE on 2 MYM contracts ($0.50/pt) is 12 points.
        self.assertAlmostEqual(trade.mae_points, 12.0)
        self.assertAlmostEqual(trade.mfe_points, 30.0)

    def test_without_a_stop_there_is_no_r_multiple(self):
        path = self.write_csv(NT_TRADE_EXPORT)
        with Journal(self.path) as journal:
            journal.import_csv(path)
            self.assertIsNone(journal.trades()[0].r_multiple)

    def test_a_default_stop_distance_enables_r_multiples(self):
        path = self.write_csv(NT_TRADE_EXPORT)
        with Journal(self.path) as journal:
            journal.import_csv(path, default_stop_points=25.0)
            trades = journal.trades()
        self.assertAlmostEqual(trades[0].stop_price, 41000.0 - 25.0)
        self.assertAlmostEqual(trades[1].stop_price, 41010.0 + 25.0)
        self.assertIsNotNone(trades[0].r_multiple)

    def test_parenthesised_losses_are_negative(self):
        path = self.write_csv(NT_TRADE_EXPORT)
        with Journal(self.path) as journal:
            journal.import_csv(path)
            self.assertLess(journal.trades()[1].net_pnl, 0)

    def test_a_file_without_the_required_columns_is_rejected(self):
        path = self.write_csv("a,b,c\n1,2,3\n")
        with Journal(self.path) as journal:
            with self.assertRaises(ValueError):
                journal.import_csv(path)

    def test_the_backtesters_own_csv_imports_cleanly(self):
        from datetime import date
        from ym.backtest import BacktestConfig, Backtester
        from ym.data import generate_bars
        from ym.instruments import MYM
        from ym.risk import RiskLimits
        from ym.strategies import OpeningRangeBreakout

        result = Backtester(
            MYM, OpeningRangeBreakout(), 25_000,
            RiskLimits(risk_per_trade_pct=1.0), BacktestConfig(),
        ).run(generate_bars(date(2026, 6, 1), days=15, seed=11))
        out = self.write_csv("")
        result.write_trades_csv(out)
        with Journal(self.path) as journal:
            imported, _ = journal.import_csv(out, excursion_unit="points")
            restored = journal.trades()
        self.assertEqual(imported, len(result.trades))
        self.assertAlmostEqual(
            sum(t.net_pnl for t in restored),
            sum(t.net_pnl for t in result.trades),
            places=2,
        )
        self.assertTrue(all(t.r_multiple is not None for t in restored))


POSITION_HEADER = (
    "Position ID,Timestamp,Trade Date,Net Pos,Net Price,Bought,Avg. Buy,Sold,"
    "Avg. Sell,Account,Contract,Product,Product Description,Pair ID,Buy Fill ID,"
    "Sell Fill ID,Paired Qty,Buy Price,Sell Price,P/L,Currency,Bought Timestamp,"
    "Sold Timestamp\n"
)


def position_row(pair, buy_id, sell_id, buy, sell, pnl, bought, sold, qty=1):
    return (
        f"9,09/10/2026 13:01:02,2026-09-10,0,,1,,1,,1,MYMU6,MYM,Micro,{pair},"
        f"{buy_id},{sell_id},{qty},{buy},{sell},{pnl},USD,{bought},{sold}\n"
    )


# Wall-clock times are US/Pacific, as the web platform exports them.
POSITION_HISTORY = POSITION_HEADER + "".join([
    # Long, entered 16:33 PT = 19:33 ET: after the 18:00 roll, so Thursday's session.
    position_row(1, 100, 110, 52483, 52518, "17.50",
                 "09/09/2026 16:33:20", "09/09/2026 17:16:58"),
    # Short: the sell filled first.
    position_row(2, 130, 120, 52573, 52532, "(20.50)",
                 "09/10/2026 06:10:00", "09/10/2026 06:00:00"),
    # One 2-lot buy (fill 140) closed by two 1-lot sells.
    position_row(3, 140, 150, 52500, 52510, "5.00",
                 "09/10/2026 07:00:00", "09/10/2026 07:05:00"),
    position_row(4, 140, 160, 52500, 52520, "10.00",
                 "09/10/2026 07:00:00", "09/10/2026 07:06:00"),
    # Both legs in the same second: the lower fill ID came first.
    position_row(5, 170, 180, 52600, 52602, "1.00",
                 "09/10/2026 08:00:00", "09/10/2026 08:00:00"),
])


def fee_rows(fill_id, stamp, contracts=1, contract="MYMU6"):
    """$0.84 per contract per fill, split over two rows as the broker does."""
    return (
        f"1,{fill_id + 1},{stamp},2026-09-10,{-0.50 * contracts:.2f},0, Commission,USD,{contract}\n"
        f"1,{fill_id + 2},{stamp},2026-09-10,{-0.34 * contracts:.2f},0, Exchange Fee,USD,{contract}\n"
    )


CASH_HEADER = "Account,Transaction ID,Timestamp,Date,Delta,Amount,Cash Change Type,Currency,Contract\n"
CASH_HISTORY = CASH_HEADER + "".join([
    "1,1,09/09/2026 07:25:33,2026-09-09,100.00,100.00, Fund Transaction,USD,\n",
    fee_rows(100, "09/09/2026 16:33:20"),
    fee_rows(110, "09/09/2026 17:16:59"),        # stamped a second after the fill
    fee_rows(120, "09/10/2026 06:00:00"),
    fee_rows(130, "09/10/2026 06:10:00"),
    fee_rows(140, "09/10/2026 07:00:00", contracts=2),
    fee_rows(150, "09/10/2026 07:05:00"),
    fee_rows(160, "09/10/2026 07:06:00"),
    fee_rows(170, "09/10/2026 08:00:00"),
    fee_rows(180, "09/10/2026 08:00:00"),
    "1,900,09/10/2026 08:00:00,2026-09-10,13.00,0, Trade Paired,USD,MYMU6\n",
])


class TestPositionHistoryImport(JournalCase):
    def parse(self, positions=POSITION_HISTORY, cash=CASH_HISTORY, **kwargs):
        cash_path = self.write_csv(cash) if cash is not None else None
        return parse_position_history(
            self.write_csv(positions), tz="America/Los_Angeles",
            cash_history=cash_path, **kwargs,
        )

    def test_reads_every_pair_cleanly(self):
        trades, warnings = self.parse()
        self.assertEqual(len(trades), 5)
        self.assertEqual(warnings, [])
        self.assertAlmostEqual(sum(t.gross_pnl for t in trades), 13.00)

    def test_direction_comes_from_which_leg_filled_first(self):
        trades, _ = self.parse()
        self.assertEqual(
            [t.direction for t in trades],
            [Direction.LONG, Direction.SHORT, Direction.LONG, Direction.LONG,
             Direction.LONG],
        )
        short = trades[1]
        self.assertEqual((short.entry_price, short.exit_price), (52532, 52573))
        self.assertAlmostEqual(short.gross_pnl, -20.50)

    def test_pacific_times_become_eastern_and_roll_the_session(self):
        trades, _ = self.parse()
        first = trades[0]
        self.assertEqual(first.entry_time, datetime(2026, 9, 9, 19, 33, 20, tzinfo=ET))
        self.assertEqual(first.entry_time.utcoffset(), timedelta(hours=-4))
        self.assertEqual(DEFAULT_SESSION.session_day(first.entry_time).isoformat(),
                         "2026-09-10")

    def test_actual_fees_attach_including_a_shared_fill(self):
        trades, _ = self.parse()
        for trade in trades:
            self.assertAlmostEqual(trade.commission, 1.68)
        self.assertAlmostEqual(sum(t.commission for t in trades), 0.84 * 10)

    def test_fees_without_a_matching_fill_are_reported_not_charged(self):
        late = CASH_HISTORY.replace("09/10/2026 06:10:00", "09/10/2026 06:10:05")
        trades, warnings = self.parse(cash=late)
        self.assertAlmostEqual(trades[1].commission, 0.84)
        self.assertTrue(any("2 fee row(s) totalling -0.84" in w for w in warnings))

    def test_unknown_cash_types_are_reported(self):
        extra = CASH_HISTORY + "1,950,09/10/2026 09:00:00,2026-09-10,-5.00,0, Market Data Fee,USD,\n"
        _, warnings = self.parse(cash=extra)
        self.assertTrue(any("'Market Data Fee' totalling -5.00" in w for w in warnings))

    def test_realized_mismatch_flags_files_covering_different_dates(self):
        _, warnings = self.parse(cash=CASH_HISTORY.replace(",13.00,", ",20.00,"))
        self.assertTrue(any("cover different dates" in w for w in warnings))

    def test_without_cash_history_pnl_is_gross_and_says_so(self):
        trades, warnings = self.parse(cash=None)
        self.assertTrue(all(t.commission == 0 for t in trades))
        self.assertTrue(any("P&L is gross" in w for w in warnings))

    def test_default_stop_gives_r_multiples(self):
        trades, _ = self.parse(default_stop_points=20)
        self.assertEqual(trades[0].stop_price, 52463)
        self.assertEqual(trades[1].stop_price, 52552)
        self.assertIsNotNone(trades[0].r_multiple)

    def test_bad_row_is_a_warning_not_a_lost_file(self):
        broken = POSITION_HISTORY + position_row(
            6, 190, 200, "n/a", 52600, "0", "09/10/2026 09:00:00", "09/10/2026 09:01:00")
        trades, warnings = self.parse(positions=broken, cash=None)
        self.assertEqual(len(trades), 5)
        self.assertTrue(any("line 7: unreadable price" in w for w in warnings))

    def test_pnl_that_disagrees_with_prices_is_flagged(self):
        wrong = POSITION_HISTORY.replace(",17.50,", ",175.00,")
        _, warnings = self.parse(positions=wrong)
        self.assertTrue(any("file P/L 175.00 != computed 17.50" in w for w in warnings))

    def test_ambiguous_dst_time_is_flagged(self):
        fallback = POSITION_HEADER + position_row(
            1, 100, 110, 52483, 52518, "17.50",
            "11/01/2026 01:30:00", "11/01/2026 02:30:00")
        _, warnings = self.parse(positions=fallback, cash=None)
        self.assertTrue(any("DST fall-back" in w for w in warnings))

    def test_missing_columns_name_what_was_found(self):
        with self.assertRaises(ValueError) as caught:
            self.parse(positions="Account,Contract\n1,MYMU6\n", cash=None)
        self.assertIn("pair id", str(caught.exception))


class TestExport(JournalCase):
    def test_export_writes_one_row_per_trade(self):
        out = self.write_csv("")
        with Journal(self.path) as journal:
            journal.record(sample_trade())
            journal.record(sample_trade(day=9))
            count = journal.export_csv(out)
        self.assertEqual(count, 2)
        with open(out) as handle:
            lines = handle.read().strip().splitlines()
        self.assertEqual(len(lines), 3)  # header plus two trades
        self.assertIn("r_multiple", lines[0])


if __name__ == "__main__":
    unittest.main()
