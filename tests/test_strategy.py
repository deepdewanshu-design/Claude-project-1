import numpy as np
import pandas as pd
import pytest

from scalper.config import StrategyConfig
from scalper.indicators import atr, ema, macd, rsi
from scalper.strategy import ScalpStrategy, TripleConfirmationStrategy, build_strategy

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


def test_spike_guard_blocks_after_abnormal_candle():
    cfg = StrategyConfig(rsi_long_max=100.0, min_atr_points=1)
    strat = ScalpStrategy(cfg)
    m5 = make_df(np.linspace(1.0950, 1.1050, 80))
    down = np.linspace(1.1050, 1.1000, 60)
    up = np.linspace(1.1000, 1.1080, 3)
    m1 = make_df(np.concatenate([down, up]))
    # sanity: this setup produces a signal without the spike
    assert strat.evaluate("EURUSD", m1, m5, POINT) is not None
    # inject a news-style candle (huge range) 5 bars back -> stand aside
    spiked = m1.copy()
    idx = len(spiked) - 5
    spiked.loc[idx, "high"] = spiked.loc[idx, "close"] + 0.0100
    spiked.loc[idx, "low"] = spiked.loc[idx, "close"] - 0.0100
    assert strat.evaluate("EURUSD", spiked, m5, POINT) is None
    # spike disabled -> signal returns
    strat_off = ScalpStrategy(StrategyConfig(
        rsi_long_max=100.0, min_atr_points=1, max_candle_atr_mult=0))
    assert strat_off.evaluate("EURUSD", spiked, m5, POINT) is not None


def test_quiet_market_filtered_by_atr_floor():
    cfg = StrategyConfig(min_atr_points=500)
    strat = ScalpStrategy(cfg)
    m5 = make_df(np.linspace(1.0950, 1.1050, 80))
    down = np.linspace(1.1050, 1.1000, 60)
    up = np.linspace(1.1000, 1.1080, 3)
    m1 = make_df(np.concatenate([down, up]), spread=0.00001)
    assert strat.evaluate("EURUSD", m1, m5, POINT) is None


# ---- triple-confirmation engine ------------------------------------------------


def triple_cfg(**kw):
    return StrategyConfig(engine="triple", min_atr_points=1, **kw)


def dip_and_recover():
    """Steep uptrend, shallow sharp pullback, then a recovery turn: price
    stays above EMA50, RSI dips oversold, MACD crosses back up at the end."""
    base = np.linspace(1.0800, 1.1100, 160)          # steep uptrend
    pull = np.linspace(1.1100, 1.1070, 20)           # sharp dip (RSI oversold)
    turn = np.linspace(1.1070, 1.1090, 4)            # recovery
    return make_df(np.concatenate([base, pull, turn]))


def test_macd_indicator():
    flat = pd.Series([5.0] * 100)
    line, sig = macd(flat)
    assert abs(line.iloc[-1]) < 1e-12 and abs(sig.iloc[-1]) < 1e-12
    rising = pd.Series(np.linspace(1, 2, 100))
    line, sig = macd(rising)
    assert line.iloc[-1] > 0


def test_build_strategy_factory():
    assert isinstance(build_strategy(StrategyConfig()), ScalpStrategy)
    assert isinstance(build_strategy(triple_cfg()), TripleConfirmationStrategy)
    with pytest.raises(ValueError):
        build_strategy(StrategyConfig(engine="nonsense"))


def test_triple_long_on_pullback_recovery():
    strat = TripleConfirmationStrategy(triple_cfg())
    m1 = dip_and_recover()
    # find a bar in the recovery where all three conditions line up
    sig = None
    for end in range(len(m1) - 6, len(m1) + 1):
        sig = strat.evaluate("EURUSD", m1.iloc[:end], m1, POINT)
        if sig is not None:
            break
    assert sig is not None and sig.direction == "buy"
    assert sig.tp_distance == pytest.approx(1.2 * sig.sl_distance)


def test_triple_no_long_below_trend_ema():
    strat = TripleConfirmationStrategy(triple_cfg())
    # same dip-and-turn shape but inside a downtrend: price below EMA50
    base = np.linspace(1.1400, 1.1100, 160)
    pull = np.linspace(1.1100, 1.1070, 20)
    turn = np.linspace(1.1070, 1.1090, 4)
    m1 = make_df(np.concatenate([base, pull, turn]))
    for end in range(len(m1) - 6, len(m1) + 1):
        sig = strat.evaluate("EURUSD", m1.iloc[:end], m1, POINT)
        assert sig is None or sig.direction != "buy"


def test_triple_requires_recent_oversold_dip():
    strat = TripleConfirmationStrategy(triple_cfg(rsi_oversold=1.0))  # dip ~impossible
    m1 = dip_and_recover()
    for end in range(len(m1) - 6, len(m1) + 1):
        assert strat.evaluate("EURUSD", m1.iloc[:end], m1, POINT) is None
