import numpy as np
import pandas as pd
import pytest

from scalper.config import StrategyConfig
from scalper.indicators import atr, ema, rsi
from scalper.strategy import ScalpStrategy

POINT = 0.00001


def make_df(closes, spread=0.0003):
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "time": pd.date_range("2026-01-05 08:00", periods=len(closes), freq="1min", tz="UTC"),
        "open": closes,
        "high": closes + spread,
        "low": closes - spread,
        "close": closes,
    })


def test_ema_converges_to_constant():
    s = pd.Series([5.0] * 100)
    assert ema(s, 10).iloc[-1] == pytest.approx(5.0)


def test_rsi_bounds():
    up = rsi(pd.Series(np.linspace(1, 2, 100)), 14)
    assert up.iloc[-1] == pytest.approx(100.0)
    flat = rsi(pd.Series([1.0] * 50), 14)
    assert flat.iloc[-1] == pytest.approx(50.0)


def test_atr_positive():
    df = make_df(np.linspace(1.10, 1.11, 60))
    assert atr(df, 14).iloc[-1] > 0


def test_no_signal_without_enough_bars():
    strat = ScalpStrategy(StrategyConfig())
    df = make_df([1.1] * 10)
    assert strat.evaluate("EURUSD", df, df, POINT) is None


def test_long_signal_on_crossover_in_uptrend():
    cfg = StrategyConfig(rsi_long_max=100.0, min_atr_points=1)
    strat = ScalpStrategy(cfg)

    # M5 uptrend
    m5 = make_df(np.linspace(1.0950, 1.1050, 80))
    # M1: decline (fast EMA below slow), then a sharp rally forcing a cross
    # on the final candle.
    down = np.linspace(1.1050, 1.1000, 60)
    up = np.linspace(1.1000, 1.1080, 3)
    m1 = make_df(np.concatenate([down, up]))

    sig = strat.evaluate("EURUSD", m1, m5, POINT)
    assert sig is not None and sig.direction == "buy"
    assert sig.sl_distance >= cfg.min_sl_points * POINT
    assert sig.tp_distance == pytest.approx(cfg.reward_risk * sig.sl_distance)


def test_no_long_signal_against_downtrend():
    cfg = StrategyConfig(rsi_long_max=100.0, min_atr_points=1)
    strat = ScalpStrategy(cfg)
    m5 = make_df(np.linspace(1.1150, 1.1050, 80))  # M5 downtrend
    down = np.linspace(1.1050, 1.1000, 60)
    up = np.linspace(1.1000, 1.1080, 3)
    m1 = make_df(np.concatenate([down, up]))       # bullish cross on M1
    assert strat.evaluate("EURUSD", m1, m5, POINT) is None


def test_quiet_market_filtered_by_atr_floor():
    cfg = StrategyConfig(min_atr_points=500)
    strat = ScalpStrategy(cfg)
    m5 = make_df(np.linspace(1.0950, 1.1050, 80))
    down = np.linspace(1.1050, 1.1000, 60)
    up = np.linspace(1.1000, 1.1080, 3)
    m1 = make_df(np.concatenate([down, up]), spread=0.00001)
    assert strat.evaluate("EURUSD", m1, m5, POINT) is None
