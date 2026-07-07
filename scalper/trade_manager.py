"""Open-position management: break-even, ATR trailing stop, time stop."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .config import ManagementConfig

log = logging.getLogger(__name__)


class TradeManager:
    def __init__(self, cfg: ManagementConfig, client) -> None:
        self.cfg = cfg
        self.client = client

    def manage(self, position, atr_value: float, point: float) -> None:
        """Apply break-even / trailing / time-stop rules to one open position."""
        import MetaTrader5 as mt5  # local import: module stays importable off-Windows

        tick = self.client.tick(position.symbol)
        is_buy = position.type == mt5.POSITION_TYPE_BUY
        price = tick.bid if is_buy else tick.ask  # exit side

        entry = position.price_open
        sl = position.sl
        risk = abs(entry - sl) if sl else 0.0

        # --- time stop: scalps that stall get closed -------------------------
        opened = datetime.fromtimestamp(position.time, tz=timezone.utc)
        age_min = (datetime.now(timezone.utc) - opened).total_seconds() / 60.0
        if age_min >= self.cfg.max_hold_minutes:
            log.info(
                "Time stop: closing %s #%s after %.1f min",
                position.symbol, position.ticket, age_min,
            )
            self.client.close_position(position)
            return

        if risk <= 0:
            return

        profit_dist = (price - entry) if is_buy else (entry - price)
        buffer = self.cfg.breakeven_buffer_points * point

        new_sl = None
        be_level = entry + buffer if is_buy else entry - buffer

        # --- break-even: at +trigger*R lock the entry in --------------------
        at_breakeven = (sl >= be_level) if is_buy else (sl <= be_level)
        if not at_breakeven and profit_dist >= self.cfg.breakeven_trigger_rr * risk:
            new_sl = be_level

        # --- ATR trail: once past break-even, follow price -------------------
        if at_breakeven and self.cfg.trail_atr_mult > 0:
            trail = price - self.cfg.trail_atr_mult * atr_value if is_buy \
                else price + self.cfg.trail_atr_mult * atr_value
            if (is_buy and trail > sl) or (not is_buy and trail < sl):
                new_sl = trail

        if new_sl is not None:
            digits = 8
            info = mt5.symbol_info(position.symbol)
            if info is not None:
                digits = info.digits
            new_sl = round(new_sl, digits)
            if new_sl != sl:
                log.info(
                    "Moving SL on %s #%s: %.5f -> %.5f",
                    position.symbol, position.ticket, sl, new_sl,
                )
                self.client.modify_sl_tp(position, new_sl, position.tp)

    def flatten_all(self, reason: str) -> None:
        positions = self.client.my_positions()
        if positions:
            log.info("Flattening %d position(s): %s", len(positions), reason)
        for p in positions:
            self.client.close_position(p)
