from datetime import datetime, timedelta, timezone

import pytest

from scalper.config import LearningConfig
from scalper.journal import TradeJournal
from scalper.learning import LearningEngine


def add_trade(journal, ticket, symbol="EURUSD", direction="buy", hour=9,
              r=-1.0, spread=10.0, days_ago=1):
    """Record one open+close round trip with a given R outcome."""
    close_dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    journal.record_open(ticket, {
        "symbol": symbol, "direction": direction,
        "open_time": (close_dt - timedelta(minutes=10)).isoformat(),
        "entry_price": 1.1, "lots": 0.05, "sl_price": 1.099, "tp_price": 1.102,
        "sl_points": 100, "spread_points": spread, "atr_points": 20.0,
        "rsi": 55.0, "hour_utc": hour, "weekday": close_dt.weekday(),
        "risk_multiplier": 1.0, "planned_risk": 25.0,
    })
    journal.record_close(
        ticket, exit_price=1.099, pnl=r * 25.0,
        exit_reason="stop_loss" if r < 0 else "take_profit",
        close_time=close_dt.isoformat(),
    )


@pytest.fixture
def setup(tmp_path):
    journal = TradeJournal(tmp_path / "journal")
    cfg = LearningConfig(journal_dir=str(tmp_path / "journal"),
                         min_trades_per_bucket=5)
    return journal, LearningEngine(cfg, journal)


def test_journal_roundtrip(tmp_path):
    journal = TradeJournal(tmp_path / "j")
    add_trade(journal, 1, r=-1.0)
    df = journal.load_closed()
    assert len(df) == 1
    assert df.iloc[0]["pnl"] == pytest.approx(-25.0)
    assert df.iloc[0]["r_multiple"] == pytest.approx(-1.0)
    assert journal.open_tickets() == {}


def test_close_unknown_ticket_ignored(tmp_path):
    journal = TradeJournal(tmp_path / "j")
    assert journal.record_close(99, 1.1, -5.0, "manual", "2026-07-01T10:00:00+00:00") is None
    assert journal.load_closed().empty


def test_blocks_losing_hour(setup):
    journal, engine = setup
    # 6 losses at 09:00 (above the 5-trade minimum), split between directions
    # so only the hour pattern crosses the threshold
    for i in range(6):
        add_trade(journal, i, hour=9, direction="buy" if i % 2 else "sell", r=-1.0)
    engine.refresh()
    ok, why = engine.allows("EURUSD", "buy", 9, 10.0)
    assert not ok and "09:00" in why
    assert engine.allows("EURUSD", "buy", 14, 10.0)[0]  # other hours still fine


def test_small_sample_not_blocked(setup):
    journal, engine = setup
    for i in range(4):  # below min_trades_per_bucket=5
        add_trade(journal, i, hour=9, r=-1.0)
    engine.refresh()
    assert engine.allows("EURUSD", "buy", 9, 10.0)[0]


def test_blocks_losing_direction(setup):
    journal, engine = setup
    for i in range(5):
        add_trade(journal, i, direction="sell", hour=8 + i, r=-1.0)
    engine.refresh()
    ok, why = engine.allows("EURUSD", "sell", 14, 10.0)
    assert not ok and "sell" in why
    assert engine.allows("EURUSD", "buy", 14, 10.0)[0]


def test_profitable_pattern_not_blocked(setup):
    journal, engine = setup
    for i in range(10):
        add_trade(journal, i, hour=9, r=1.2 if i % 2 else -1.0)  # net positive
    engine.refresh()
    assert engine.allows("EURUSD", "buy", 9, 10.0)[0]


def test_spread_cap_learned(setup):
    journal, engine = setup
    # cheap-spread trades win, expensive ones lose
    for i in range(5):
        add_trade(journal, i, hour=8 + i, r=1.0, spread=5.0)
    for i in range(5, 10):
        add_trade(journal, i, hour=8 + i, r=-1.0, spread=25.0)
    engine.refresh()
    assert "EURUSD" in engine.spread_caps
    ok, why = engine.allows("EURUSD", "buy", 14, 25.0)
    assert not ok and "spread" in why
    assert engine.allows("EURUSD", "buy", 14, 5.0)[0]


def test_loss_streak_throttles_risk(setup):
    journal, engine = setup
    add_trade(journal, 0, hour=8, r=1.0, days_ago=5)
    for i in range(1, 4):  # 3 consecutive losses, most recent last
        add_trade(journal, i, hour=8 + i, r=-1.0, days_ago=4 - i)
    engine.refresh()
    assert engine.loss_streak == 3
    assert engine.risk_multiplier() == pytest.approx(0.5)


def test_win_resets_streak(setup):
    journal, engine = setup
    for i in range(3):
        add_trade(journal, i, hour=8 + i, r=-1.0, days_ago=3)
    add_trade(journal, 10, hour=12, r=1.0, days_ago=1)  # most recent = win
    engine.refresh()
    assert engine.loss_streak == 0
    assert engine.risk_multiplier() == 1.0


def test_old_losses_age_out(setup):
    journal, engine = setup
    for i in range(6):
        add_trade(journal, i, hour=9, r=-1.0, days_ago=45)  # beyond 30-day window
    engine.refresh()
    assert engine.allows("EURUSD", "buy", 9, 10.0)[0]


def test_rules_persisted(setup):
    journal, engine = setup
    for i in range(6):
        add_trade(journal, i, hour=9, r=-1.0)
    engine.refresh()
    assert engine.rules_path.exists()
    snap = engine.snapshot()
    assert any(r["rule"] == "block_hour" for r in snap["rules"])
