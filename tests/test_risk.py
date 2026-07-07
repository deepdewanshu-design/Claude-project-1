import pytest

from scalper.config import RiskConfig
from scalper.risk import RiskManager, SymbolSpec

EURUSD = SymbolSpec(
    name="EURUSD", point=0.00001, tick_size=0.00001, tick_value=1.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
)


@pytest.fixture
def rm():
    return RiskManager(corpus=5000.0, risk_cfg=RiskConfig())


def test_target_risk_is_half_percent_of_corpus(rm):
    assert rm.target_risk_amount(equity=5000.0) == pytest.approx(25.0)


def test_risk_basis_capped_at_corpus(rm):
    # Demo balance of 100k must still size like a 5k account.
    assert rm.target_risk_amount(equity=100_000.0) == pytest.approx(25.0)


def test_risk_basis_shrinks_with_drawdown(rm):
    assert rm.target_risk_amount(equity=4000.0) == pytest.approx(20.0)


def test_lot_size_matches_target_risk(rm):
    # 30-pip SL = 0.0030; $25 target / ($10/pip/lot * 30 pips) = 0.083 -> 0.08
    res = rm.lot_size(EURUSD, sl_distance=0.0030, equity=5000.0)
    assert res.lots == pytest.approx(0.08)
    assert res.risk_amount <= res.target_risk


def test_lot_size_rounds_down_to_step(rm):
    res = rm.lot_size(EURUSD, sl_distance=0.0017, equity=5000.0)
    # raw = 25 / (0.0017/0.00001*1.0) = 0.147 -> floor to 0.14
    assert res.lots == pytest.approx(0.14)


def test_min_lot_used_when_close_enough(rm):
    # Huge SL: raw lots below min but min-lot risk within 1.5x target -> min lot
    res = rm.lot_size(EURUSD, sl_distance=0.0300, equity=5000.0)
    assert res.lots == pytest.approx(0.01)
    assert res.risk_amount == pytest.approx(30.0)


def test_skip_when_min_lot_risks_too_much(rm):
    # 500-pip SL: min lot risks $50 > 1.5 * $25 -> skip
    res = rm.lot_size(EURUSD, sl_distance=0.0500, equity=5000.0)
    assert res.lots == 0.0
    assert "min lot" in res.reason


def test_daily_loss_limit(rm):
    assert not rm.daily_loss_hit(todays_pnl=-99.0, equity=5000.0)
    assert rm.daily_loss_hit(todays_pnl=-100.0, equity=5000.0)


def test_can_open_gates(rm):
    ok, _ = rm.can_open(0, 0, 0, 0.0, 5000.0)
    assert ok
    assert not rm.can_open(2, 0, 0, 0.0, 5000.0)[0]      # max positions
    assert not rm.can_open(0, 1, 0, 0.0, 5000.0)[0]      # per-symbol cap
    assert not rm.can_open(0, 0, 30, 0.0, 5000.0)[0]     # daily trade cap
    assert not rm.can_open(0, 0, 0, -150.0, 5000.0)[0]   # daily loss stop
