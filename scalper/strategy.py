"""Scalping signal logic: M1 EMA crossover entries filtered by M5 trend.

Long setup:
  * M5 trend up   (EMA20 > EMA50 on the trend timeframe)
  * M1 EMA9 crosses above EMA21 on the last CLOSED candle
  * M1 RSI(14) in [rsi_long_min, rsi_long_max]  (momentum, not yet overbought)
  * ATR above a floor (market is actually moving)

Short setup is the mirror image. Stops/targets are ATR-based:
  SL = sl_atr_mult * ATR (floored at min_sl_points), TP = reward_risk * SL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .config import StrategyConfig
from .indicators import atr, ema, rsi


@dataclass
class Signal:
    symbol: str
    direction: str        # "buy" | "sell"
    sl_distance: float    # price units
    tp_distance: float    # price units
    atr_value: float
    rsi_value: float = 0.0
    note: str = ""


class ScalpStrategy:
    def __init__(self, cfg: StrategyConfig) -> None:
        self.cfg = cfg

    def min_bars(self) -> int:
        return max(
            self.cfg.ema_slow,
            self.cfg.trend_ema_slow,
            self.cfg.rsi_period,
            self.cfg.atr_period,
        ) + 5

    def trend_direction(self, m5: pd.DataFrame) -> int:
        """+1 uptrend, -1 downtrend, 0 no clear trend (on closed M5 candles)."""
        fast = ema(m5["close"], self.cfg.trend_ema_fast).iloc[-1]
        slow = ema(m5["close"], self.cfg.trend_ema_slow).iloc[-1]
        if fast > slow:
            return 1
        if fast < slow:
            return -1
        return 0

    def evaluate(
        self, symbol: str, m1: pd.DataFrame, m5: pd.DataFrame, point: float
    ) -> Optional[Signal]:
        """Evaluate the last closed M1 candle. DataFrames must contain only
        closed candles with columns open/high/low/close."""
        if len(m1) < self.min_bars() or len(m5) < self.cfg.trend_ema_slow + 5:
            return None

        trend = self.trend_direction(m5)
        if trend == 0:
            return None

        close = m1["close"]
        fast = ema(close, self.cfg.ema_fast)
        slow = ema(close, self.cfg.ema_slow)
        cur_rsi = rsi(close, self.cfg.rsi_period).iloc[-1]
        cur_atr = float(atr(m1, self.cfg.atr_period).iloc[-1])

        if cur_atr < self.cfg.min_atr_points * point:
            return None  # market too quiet to scalp

        crossed_up = fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]
        crossed_down = fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]

        direction = None
        if trend > 0 and crossed_up and self.cfg.rsi_long_min <= cur_rsi <= self.cfg.rsi_long_max:
            direction = "buy"
        elif trend < 0 and crossed_down and self.cfg.rsi_short_min <= cur_rsi <= self.cfg.rsi_short_max:
            direction = "sell"
        if direction is None:
            return None

        sl_distance = max(self.cfg.sl_atr_mult * cur_atr, self.cfg.min_sl_points * point)
        tp_distance = self.cfg.reward_risk * sl_distance
        return Signal(
            symbol=symbol,
            direction=direction,
            sl_distance=sl_distance,
            tp_distance=tp_distance,
            atr_value=cur_atr,
            rsi_value=float(cur_rsi),
            note=f"trend={'up' if trend > 0 else 'down'} rsi={cur_rsi:.1f} atr={cur_atr:.5f}",
        )
