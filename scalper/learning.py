"""Learn from losing trades and adapt the bot's behaviour.

This is deliberately NOT a black box. It mines the trade journal for
persistent loss patterns and turns them into explainable rules:

  1. Hour blocks      — if a (symbol, hour-of-day) bucket has enough trades
                        and a clearly negative average R, stop trading that
                        symbol in that hour.
  2. Direction blocks — same test for (symbol, direction): if e.g. GBPUSD
                        shorts keep losing, only longs remain allowed.
  3. Spread caps      — if the more expensive half of a symbol's trades
                        (by spread at entry) loses money, cap the accepted
                        spread at that symbol's observed median.
  4. Loss-streak throttle — after N consecutive losses, risk per trade is
                        halved (and halved again as the streak grows) until
                        a winner resets it.

Rules are recomputed from a rolling lookback window after every closed
trade, so a blocked bucket is re-allowed once its losing trades age out of
the window. Everything learned is written to learned_rules.json in the
journal directory with the statistics that justify each rule.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .config import LearningConfig
from .journal import TradeJournal

log = logging.getLogger(__name__)


class LearningEngine:
    def __init__(self, cfg: LearningConfig, journal: TradeJournal) -> None:
        self.cfg = cfg
        self.journal = journal
        self.rules_path = journal.dir / "learned_rules.json"
        self.blocked_hours: set[tuple[str, int]] = set()
        self.blocked_directions: set[tuple[str, str]] = set()
        self.spread_caps: dict[str, float] = {}
        self.loss_streak: int = 0
        self._details: list[dict] = []

    # ---- rule computation ------------------------------------------------------

    def _window(self) -> pd.DataFrame:
        df = self.journal.load_closed()
        if df.empty:
            return df
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.cfg.lookback_days)
        df = df[df["close_time"] >= cutoff]
        return df.dropna(subset=["r_multiple"])

    def refresh(self) -> None:
        df = self._window()
        self.blocked_hours.clear()
        self.blocked_directions.clear()
        self.spread_caps.clear()
        self._details = []
        self.loss_streak = self._trailing_losses(df)

        if len(df) >= self.cfg.min_trades_per_bucket:
            self._learn_hour_blocks(df)
            self._learn_direction_blocks(df)
            self._learn_spread_caps(df)

        self._save(df)

    def _learn_hour_blocks(self, df: pd.DataFrame) -> None:
        for (symbol, hour), g in df.groupby(["symbol", "hour_utc"]):
            if len(g) >= self.cfg.min_trades_per_bucket \
                    and g["r_multiple"].mean() < self.cfg.block_expectancy_r:
                self.blocked_hours.add((symbol, int(hour)))
                self._details.append({
                    "rule": "block_hour", "symbol": symbol, "hour_utc": int(hour),
                    "trades": len(g), "avg_r": round(g["r_multiple"].mean(), 3),
                    "why": f"{len(g)} trades averaging "
                           f"{g['r_multiple'].mean():.2f}R in this hour",
                })

    def _learn_direction_blocks(self, df: pd.DataFrame) -> None:
        for (symbol, direction), g in df.groupby(["symbol", "direction"]):
            if len(g) >= self.cfg.min_trades_per_bucket \
                    and g["r_multiple"].mean() < self.cfg.block_expectancy_r:
                self.blocked_directions.add((symbol, direction))
                self._details.append({
                    "rule": "block_direction", "symbol": symbol,
                    "direction": direction,
                    "trades": len(g), "avg_r": round(g["r_multiple"].mean(), 3),
                    "why": f"{len(g)} {direction} trades averaging "
                           f"{g['r_multiple'].mean():.2f}R",
                })

    def _learn_spread_caps(self, df: pd.DataFrame) -> None:
        for symbol, g in df.groupby("symbol"):
            if len(g) < 2 * self.cfg.min_trades_per_bucket:
                continue
            median = float(g["spread_points"].median())
            expensive = g[g["spread_points"] > median]
            if len(expensive) >= self.cfg.min_trades_per_bucket \
                    and expensive["r_multiple"].mean() < self.cfg.block_expectancy_r:
                self.spread_caps[symbol] = median
                self._details.append({
                    "rule": "spread_cap", "symbol": symbol,
                    "max_spread_points": median,
                    "trades": len(expensive),
                    "avg_r": round(expensive["r_multiple"].mean(), 3),
                    "why": f"trades entered above {median:.0f} points spread "
                           f"average {expensive['r_multiple'].mean():.2f}R",
                })

    @staticmethod
    def _trailing_losses(df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        pnl = df.sort_values("close_time")["pnl"]
        streak = 0
        for value in reversed(pnl.tolist()):
            if value < 0:
                streak += 1
            else:
                break
        return streak

    # ---- application -----------------------------------------------------------

    def allows(
        self, symbol: str, direction: str, hour_utc: int, spread_points: float
    ) -> tuple[bool, str]:
        if (symbol, direction) in self.blocked_directions:
            return False, f"learned rule: {symbol} {direction} trades are losing"
        if (symbol, hour_utc) in self.blocked_hours:
            return False, f"learned rule: {symbol} loses during {hour_utc:02d}:00 UTC"
        cap = self.spread_caps.get(symbol)
        if cap is not None and spread_points > cap:
            return False, (
                f"learned rule: {symbol} spread {spread_points:.0f} > "
                f"learned cap {cap:.0f} points"
            )
        return True, ""

    def risk_multiplier(self) -> float:
        """Risk throttle after consecutive losses (a win resets it)."""
        excess = self.loss_streak - self.cfg.streak_throttle_after
        if excess < 0:
            return 1.0
        return max(self.cfg.min_risk_multiplier, 0.5 ** (excess + 1))

    # ---- persistence / reporting -------------------------------------------------

    def _save(self, df: pd.DataFrame) -> None:
        state = self.snapshot(trades_analyzed=len(df))
        self.rules_path.write_text(json.dumps(state, indent=2))
        if self._details:
            log.info(
                "Learning: %d active rule(s), risk multiplier %.2f",
                len(self._details), self.risk_multiplier(),
            )

    def snapshot(self, trades_analyzed: int | None = None) -> dict:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "lookback_days": self.cfg.lookback_days,
            "trades_analyzed": trades_analyzed,
            "loss_streak": self.loss_streak,
            "risk_multiplier": self.risk_multiplier(),
            "blocked_hours": sorted(
                [{"symbol": s, "hour_utc": h} for s, h in self.blocked_hours],
                key=lambda x: (x["symbol"], x["hour_utc"]),
            ),
            "blocked_directions": sorted(
                [{"symbol": s, "direction": d} for s, d in self.blocked_directions],
                key=lambda x: (x["symbol"], x["direction"]),
            ),
            "spread_caps": self.spread_caps,
            "rules": self._details,
        }
