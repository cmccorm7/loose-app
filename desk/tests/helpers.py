"""Shared fixtures: a throwaway hub and realistic statement files."""

import csv
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.config import paths                      # noqa: E402
from hub.services import Hub                      # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "trading"))

from ym.data.trader import Habits, simulate_trader_history  # noqa: E402

NINJA_HEADER = [
    "Instrument", "Market pos.", "Quantity", "Entry price", "Exit price",
    "Entry time", "Exit time", "Entry name", "Exit name", "Commission", "MAE", "MFE",
]

BAD_HABITS = Habits(
    early_edge_r=0.4, late_edge_r=-0.5, edge_decays_after=2,
    revenge_probability=0.65, stop_overshoot=0.45,
)


def ninjatrader_csv(days: int = 60, seed: int = 42) -> bytes:
    """A NinjaTrader trade export, in the shape the platform really writes."""
    trades = simulate_trader_history(days=days, habits=BAD_HABITS, seed=seed)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(NINJA_HEADER)
    for trade in trades:
        writer.writerow([
            trade.symbol, trade.direction.value, trade.contracts,
            trade.entry_price, trade.exit_price,
            trade.entry_time.strftime("%m/%d/%Y %H:%M:%S"),
            trade.exit_time.strftime("%m/%d/%Y %H:%M:%S"),
            trade.setup, trade.exit_reason.value,
            f"${trade.commission:.2f}",
            f"${trade.mae_points * trade.point_value * trade.contracts:.2f}",
            f"${trade.mfe_points * trade.point_value * trade.contracts:.2f}",
        ])
    return buffer.getvalue().encode()


SMALL_STATEMENT = (
    b"Instrument;Market pos.;Quantity;Entry price;Exit price;Entry time;"
    b"Exit time;Entry name;Commission\n"
    b"MYM;Long;2;41000;41025;9/8/2026 9:45:00;9/8/2026 10:05:00;ORB;1.00\n"
    b"MYM;Short;2;41010;40990;9/8/2026 11:00:00;9/8/2026 11:12:00;Fade;1.00\n"
)

BAR_EXPORT = b"\n".join(
    f"20260908 09{minute:02d}00;41012;41045;41003;41038;2481".encode()
    for minute in range(30, 50)
)


class HubCase(unittest.TestCase):
    """A test case with its own throwaway data directory."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.hub = Hub(paths(self.home))

    def import_statement(self, content: bytes = SMALL_STATEMENT,
                         name: str = "statement.csv", **options) -> dict:
        uploaded = self.hub.upload_statement(name, content)
        self.assertIsNone(uploaded["error"], uploaded["error"])
        return self.hub.import_statement(uploaded["statement"]["id"], **options)
