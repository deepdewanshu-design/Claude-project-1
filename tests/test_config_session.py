from pathlib import Path

from scalper.config import SessionConfig, load_config


def test_load_default_config():
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    assert cfg.corpus == 5000.0
    assert cfg.risk.risk_per_trade_pct == 0.5
    assert cfg.risk.max_daily_loss_pct == 2.0
    assert "EURUSD" in cfg.symbols
    assert cfg.strategy.entry_timeframe == "M1"
    assert cfg.strategy.trend_timeframe == "M5"


def test_session_windows_parsing():
    s = SessionConfig(trade_hours_utc=["07:00-11:00", "12:30-16:30"])
    assert s.windows() == [(420, 660), (750, 990)]


def test_empty_session_means_always():
    assert SessionConfig().windows() == []
