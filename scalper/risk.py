"""Position sizing and account-level risk limits for the 5k corpus.

All sizing is computed against min(corpus, live equity), so the bot behaves
like a $5,000 account even when the demo balance is larger, and shrinks
with the account if it draws down.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class SymbolSpec:
    """The subset of MT5 symbol_info the sizing math needs."""

    name: str
    point: float          # e.g. 0.00001 for EURUSD
    tick_size: float      # price increment per tick
    tick_value: float     # account-currency value of one tick for 1.0 lot
    volume_min: float
    volume_max: float
    volume_step: float


@dataclass
class LotResult:
    lots: float
    risk_amount: float    # money actually at risk at this size
    target_risk: float    # money we wanted to risk
    reason: str = ""      # non-empty when lots == 0 (why the trade was skipped)


class RiskManager:
    def __init__(self, corpus: float, risk_cfg) -> None:
        self.corpus = corpus
        self.cfg = risk_cfg

    # ---- sizing -----------------------------------------------------------

    def risk_basis(self, equity: float) -> float:
        return min(self.corpus, equity)

    def target_risk_amount(self, equity: float) -> float:
        return self.risk_basis(equity) * self.cfg.risk_per_trade_pct / 100.0

    def lot_size(self, spec: SymbolSpec, sl_distance: float, equity: float) -> LotResult:
        """Size a position so that hitting the stop loses ~risk_per_trade_pct.

        sl_distance is the stop distance in *price* units (not points).
        """
        target = self.target_risk_amount(equity)
        if sl_distance <= 0 or spec.tick_size <= 0 or spec.tick_value <= 0:
            return LotResult(0.0, 0.0, target, "invalid stop distance or symbol spec")

        loss_per_lot = sl_distance / spec.tick_size * spec.tick_value
        raw_lots = target / loss_per_lot

        # Round DOWN to the broker's volume step, clamp to min/max.
        step = spec.volume_step or 0.01
        lots = math.floor(raw_lots / step) * step
        lots = round(lots, 8)

        if lots < spec.volume_min:
            # Can't reach the target risk with a full step; consider min lot.
            min_risk = spec.volume_min * loss_per_lot
            if min_risk <= target * self.cfg.max_risk_overshoot:
                return LotResult(spec.volume_min, min_risk, target)
            return LotResult(
                0.0, 0.0, target,
                f"min lot {spec.volume_min} would risk {min_risk:.2f} "
                f"(> {self.cfg.max_risk_overshoot}x target {target:.2f})",
            )

        lots = min(lots, spec.volume_max)
        return LotResult(lots, lots * loss_per_lot, target)

    # ---- account-level guards --------------------------------------------

    def max_daily_loss_amount(self, equity: float) -> float:
        return self.risk_basis(equity) * self.cfg.max_daily_loss_pct / 100.0

    def daily_loss_hit(self, todays_pnl: float, equity: float) -> bool:
        limit = self.max_daily_loss_amount(equity)
        if todays_pnl <= -limit:
            log.warning(
                "Daily loss limit hit: pnl=%.2f limit=-%.2f — no more entries today",
                todays_pnl, limit,
            )
            return True
        return False

    def can_open(
        self,
        open_positions_total: int,
        open_positions_symbol: int,
        trades_today: int,
        todays_pnl: float,
        equity: float,
    ) -> tuple[bool, str]:
        if self.daily_loss_hit(todays_pnl, equity):
            return False, "daily loss limit reached"
        if trades_today >= self.cfg.max_daily_trades:
            return False, "daily trade cap reached"
        if open_positions_total >= self.cfg.max_open_positions:
            return False, "max open positions reached"
        if open_positions_symbol >= self.cfg.max_positions_per_symbol:
            return False, "max positions for symbol reached"
        return True, ""
