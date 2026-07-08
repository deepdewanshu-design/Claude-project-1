import numpy as np
import pandas as pd

from backtest import detect_bar_minutes, load_candles_csv, preset_for


def test_load_mt4_export_format(tmp_path):
    p = tmp_path / "xau.csv"
    p.write_text(
        "﻿Date,Open,High,Low,Close,Volume\n"
        "2004.06.11 07:15,384,384.1,384,384,3\n"
        "2004.06.11 07:20,384.1,384.1,383.8,383.8,3\n"
        "2004.06.11 07:20,384.1,384.1,383.8,383.8,3\n"   # duplicate row
    )
    df = load_candles_csv(str(p))
    assert list(df.columns) == ["time", "open", "high", "low", "close"]
    assert len(df) == 2  # duplicate dropped
    assert df["time"].iloc[0] == pd.Timestamp("2004-06-11 07:15")


def test_load_finam_histdata_format(tmp_path):
    p = tmp_path / "eu.csv"
    p.write_text(
        "<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>\n"
        "EURUSD,1,20170101,211000,1.0527,1.0528,1.0527,1.0527,1\n"
        "EURUSD,1,20170101,211100,1.0527,1.0529,1.0526,1.0528,4\n"
    )
    df = load_candles_csv(str(p))
    assert list(df.columns) == ["time", "open", "high", "low", "close"]
    assert df["time"].iloc[0] == pd.Timestamp("2017-01-01 21:10:00")
    assert df["close"].iloc[1] == 1.0528


def test_load_plain_format(tmp_path):
    p = tmp_path / "plain.csv"
    p.write_text(
        "time,open,high,low,close\n"
        "2026-01-05 08:00,1.1,1.2,1.0,1.15\n"
        "2026-01-05 08:01,1.15,1.2,1.1,1.18\n"
    )
    df = load_candles_csv(str(p))
    assert len(df) == 2


def test_detect_bar_minutes():
    m5 = pd.Series(pd.date_range("2026-01-05", periods=50, freq="5min"))
    assert detect_bar_minutes(m5) == 5
    m1 = pd.Series(pd.date_range("2026-01-05", periods=50, freq="1min"))
    assert detect_bar_minutes(m1) == 1


def test_preset_matching():
    assert preset_for("XAUUSD").point == 0.01
    assert preset_for("XAUUSD.x").point == 0.01
    assert preset_for("EURUSD").point == 0.00001
    assert preset_for("US30").tick_value == 0.1


def test_tf_minutes():
    from backtest import tf_minutes
    assert tf_minutes("M15") == 15
    assert tf_minutes("H1") == 60
    assert tf_minutes("H4") == 240
    assert tf_minutes("D1") == 1440


def test_resample_bars():
    from backtest import resample_bars
    m1 = pd.DataFrame({
        "time": pd.date_range("2026-01-05 08:00", periods=30, freq="1min"),
        "open": np.arange(30.0),
        "high": np.arange(30.0) + 2,
        "low": np.arange(30.0) - 2,
        "close": np.arange(30.0) + 1,
    })
    m15 = resample_bars(m1, 15)
    assert len(m15) == 2
    assert m15["open"].iloc[0] == 0.0        # first bar's open
    assert m15["high"].iloc[0] == 16.0       # max high of bars 0-14
    assert m15["low"].iloc[1] == 13.0        # min low of bars 15-29
    assert m15["close"].iloc[1] == 30.0      # last bar's close
