"""Scalping signal engines.

Two engines share the same risk plumbing (ATR stops, spike guard, ATR floor):

* ScalpStrategy ("crossover") — M1 EMA9/21 crossover filtered by M5 trend.
* TripleConfirmationStrategy ("triple") — pullback entry requiring three
  simultaneous confirmations on the entry timeframe:
    1. trend:    price above the 50 EMA (longs) / below (shorts)
    2. momentum: RSI recovering upward after dipping into oversold
                 (mirror from overbought for shorts)
    3. trigger:  MACD line crossing its signal line in the trade direction

Original crossover engine docstring:

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
from .indicators import atr, ema, macd, rsi


@dataclass
class Signal:
    symbol: str
    direction: str        # "buy" | "sell"
    sl_distance: float    # price units
    tp_distance: float    # price units
    atr_value: float
    rsi_value: float = 0.0
    note: str = ""


def common_gates(m1: pd.DataFrame, cfg: StrategyConfig, point: float) -> Optional[float]:
    """Volatility gates shared by all engines: returns current ATR, or None
    when the market is too quiet or a spike candle just happened."""
    cur_atr = float(atr(m1, cfg.atr_period).iloc[-1])
    if cur_atr < cfg.min_atr_points * point:
        return None  # market too quiet to scalp
    if cfg.max_candle_atr_mult > 0 and cfg.spike_lookback_bars > 0:
        recent = m1.tail(cfg.spike_lookback_bars)
        max_range = float((recent["high"] - recent["low"]).max())
        if max_range > cfg.max_candle_atr_mult * cur_atr:
            return None  # abnormal candle — stand aside
    return cur_atr


def sl_tp_distances(cfg: StrategyConfig, cur_atr: float, point: float) -> tuple[float, float]:
    sl = max(cfg.sl_atr_mult * cur_atr, cfg.min_sl_points * point)
    return sl, cfg.reward_risk * sl


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
        cur_atr = common_gates(m1, self.cfg, point)
        if cur_atr is None:
            return None

        crossed_up = fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]
        crossed_down = fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]

        direction = None
        if trend > 0 and crossed_up and self.cfg.rsi_long_min <= cur_rsi <= self.cfg.rsi_long_max:
            direction = "buy"
        elif trend < 0 and crossed_down and self.cfg.rsi_short_min <= cur_rsi <= self.cfg.rsi_short_max:
            direction = "sell"
        if direction is None:
            return None

        sl_distance, tp_distance = sl_tp_distances(self.cfg, cur_atr, point)
        return Signal(
            symbol=symbol,
            direction=direction,
            sl_distance=sl_distance,
            tp_distance=tp_distance,
            atr_value=cur_atr,
            rsi_value=float(cur_rsi),
            note=f"trend={'up' if trend > 0 else 'down'} rsi={cur_rsi:.1f} atr={cur_atr:.5f}",
        )


class TripleConfirmationStrategy:
    """Pullback engine: EMA trend + RSI recovery from oversold + MACD trigger.

    All three conditions must hold on the last CLOSED entry-timeframe candle.
    The higher-timeframe dataframe is accepted for interface compatibility
    but not used — the 50 EMA on the entry timeframe is the trend filter.
    """

    def __init__(self, cfg: StrategyConfig) -> None:
        self.cfg = cfg

    def min_bars(self) -> int:
        return max(
            3 * self.cfg.entry_trend_ema,               # EMA50 convergence
            self.cfg.macd_slow + 3 * self.cfg.macd_signal,
            self.cfg.rsi_period + self.cfg.rsi_dip_lookback,
            self.cfg.atr_period,
        ) + 5

    def evaluate(
        self, symbol: str, m1: pd.DataFrame, m5: pd.DataFrame, point: float
    ) -> Optional[Signal]:
        if len(m1) < self.min_bars():
            return None
        cur_atr = common_gates(m1, self.cfg, point)
        if cur_atr is None:
            return None

        close = m1["close"]
        trend_ema = ema(close, self.cfg.entry_trend_ema).iloc[-1]
        rsi_v = rsi(close, self.cfg.rsi_period)
        line, sig_line = macd(
            close, self.cfg.macd_fast, self.cfg.macd_slow, self.cfg.macd_signal
        )

        cur_rsi = float(rsi_v.iloc[-1])
        rsi_recent = rsi_v.iloc[-1 - self.cfg.rsi_dip_lookback:-1]
        rising = rsi_v.iloc[-1] > rsi_v.iloc[-2]
        falling = rsi_v.iloc[-1] < rsi_v.iloc[-2]
        between = self.cfg.rsi_oversold < cur_rsi < self.cfg.rsi_overbought
        cross_up = line.iloc[-2] <= sig_line.iloc[-2] and line.iloc[-1] > sig_line.iloc[-1]
        cross_down = line.iloc[-2] >= sig_line.iloc[-2] and line.iloc[-1] < sig_line.iloc[-1]

        direction = None
        if (close.iloc[-1] > trend_ema and cross_up and between and rising
                and float(rsi_recent.min()) <= self.cfg.rsi_oversold):
            direction = "buy"
        elif (close.iloc[-1] < trend_ema and cross_down and between and falling
                and float(rsi_recent.max()) >= self.cfg.rsi_overbought):
            direction = "sell"
        if direction is None:
            return None

        sl_distance, tp_distance = sl_tp_distances(self.cfg, cur_atr, point)
        return Signal(
            symbol=symbol,
            direction=direction,
            sl_distance=sl_distance,
            tp_distance=tp_distance,
            atr_value=cur_atr,
            rsi_value=cur_rsi,
            note=f"triple: ema50={'above' if direction == 'buy' else 'below'} "
                 f"rsi={cur_rsi:.1f} macd-cross atr={cur_atr:.5f}",
        )


def build_strategy(cfg: StrategyConfig):
    """Factory: pick the signal engine named by cfg.engine."""
    engines = {
        "crossover": ScalpStrategy,
        "triple": TripleConfirmationStrategy,
    }
    if cfg.engine not in engines:
        raise ValueError(
            f"Unknown strategy engine '{cfg.engine}' (choose from {sorted(engines)})"
        )
    return engines[cfg.engine](cfg)
