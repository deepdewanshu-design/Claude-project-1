"""Trade journal: records every trade with its full entry context.

Two files live in the journal directory:
  * open_trades.json — context of positions that are still open, keyed by ticket
  * trades.csv       — one row per CLOSED trade (entry context + outcome)

The CSV is deliberately plain so a non-coder can open it in Excel, and the
learning engine mines it for loss patterns.
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

CSV_COLUMNS = [
    "ticket", "symbol", "direction", "open_time", "close_time",
    "entry_price", "exit_price", "lots", "sl_price", "tp_price",
    "sl_points", "spread_points", "atr_points", "rsi",
    "hour_utc", "weekday", "risk_multiplier", "planned_risk",
    "exit_reason", "pnl", "r_multiple",
]


class TradeJournal:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.csv_path = self.dir / "trades.csv"
        self.open_path = self.dir / "open_trades.json"

    # ---- open positions ----------------------------------------------------

    def _load_open(self) -> dict:
        if not self.open_path.exists():
            return {}
        try:
            return json.loads(self.open_path.read_text())
        except json.JSONDecodeError:
            log.warning("Corrupt open_trades.json — starting fresh")
            return {}

    def _save_open(self, data: dict) -> None:
        self.open_path.write_text(json.dumps(data, indent=2))

    def record_open(self, ticket: int, context: dict) -> None:
        data = self._load_open()
        data[str(ticket)] = context
        self._save_open(data)
        log.info("Journal: recorded open trade #%s", ticket)

    def open_tickets(self) -> dict[str, dict]:
        return self._load_open()

    def forget_open(self, ticket: int) -> None:
        data = self._load_open()
        if data.pop(str(ticket), None) is not None:
            self._save_open(data)

    # ---- closed trades -------------------------------------------------------

    def record_close(
        self, ticket: int, exit_price: float, pnl: float,
        exit_reason: str, close_time: str,
    ) -> dict | None:
        """Move a trade from open to closed; returns the full CSV row."""
        data = self._load_open()
        ctx = data.pop(str(ticket), None)
        if ctx is None:
            log.warning("Journal: close for unknown ticket #%s ignored", ticket)
            return None
        self._save_open(data)

        planned_risk = float(ctx.get("planned_risk", 0.0) or 0.0)
        row = {c: ctx.get(c, "") for c in CSV_COLUMNS}
        row.update(
            ticket=ticket,
            close_time=close_time,
            exit_price=exit_price,
            exit_reason=exit_reason,
            pnl=round(pnl, 2),
            r_multiple=round(pnl / planned_risk, 3) if planned_risk > 0 else 0.0,
        )
        new_file = not self.csv_path.exists()
        with self.csv_path.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)
        log.info(
            "Journal: closed #%s %s %s pnl=%.2f (%.2fR, %s)",
            ticket, row["symbol"], row["direction"], pnl,
            row["r_multiple"], exit_reason,
        )
        return row

    def load_closed(self) -> pd.DataFrame:
        if not self.csv_path.exists():
            return pd.DataFrame(columns=CSV_COLUMNS)
        df = pd.read_csv(self.csv_path)
        for col in ("pnl", "r_multiple", "spread_points", "atr_points", "rsi",
                    "hour_utc", "weekday", "planned_risk"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["close_time"] = pd.to_datetime(df["close_time"], utc=True, errors="coerce")
        return df
