"""Typed configuration loaded from config.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

import yaml


@dataclass
class AccountConfig:
    login: int = 0
    password: str = ""
    server: str = ""
    terminal_path: str = ""


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.5
    max_daily_loss_pct: float = 2.0
    max_daily_trades: int = 30
    max_open_positions: int = 2
    max_positions_per_symbol: int = 1
    max_spread_points: int = 20
    max_risk_overshoot: float = 1.5


@dataclass
class StrategyConfig:
    entry_timeframe: str = "M1"
    trend_timeframe: str = "M5"
    ema_fast: int = 9
    ema_slow: int = 21
    trend_ema_fast: int = 20
    trend_ema_slow: int = 50
    rsi_period: int = 14
    rsi_long_min: float = 50.0
    rsi_long_max: float = 70.0
    rsi_short_min: float = 30.0
    rsi_short_max: float = 50.0
    atr_period: int = 14
    min_atr_points: int = 15
    sl_atr_mult: float = 1.5
    min_sl_points: int = 30
    reward_risk: float = 1.2


@dataclass
class ManagementConfig:
    breakeven_trigger_rr: float = 0.7
    breakeven_buffer_points: int = 2
    trail_atr_mult: float = 1.0
    max_hold_minutes: int = 20


@dataclass
class SessionConfig:
    trade_hours_utc: List[str] = field(default_factory=list)
    friday_flat_hour_utc: int = 20

    def windows(self) -> List[Tuple[int, int]]:
        """Parse "HH:MM-HH:MM" windows into (start_minute, end_minute) of day."""
        out = []
        for win in self.trade_hours_utc:
            start, end = win.split("-")
            sh, sm = (int(x) for x in start.strip().split(":"))
            eh, em = (int(x) for x in end.strip().split(":"))
            out.append((sh * 60 + sm, eh * 60 + em))
        return out


@dataclass
class BotConfig:
    poll_seconds: float = 2.0
    magic: int = 510150
    deviation_points: int = 10
    comment: str = "scalper-m1m5"


@dataclass
class Config:
    account: AccountConfig = field(default_factory=AccountConfig)
    corpus: float = 5000.0
    risk: RiskConfig = field(default_factory=RiskConfig)
    symbols: List[str] = field(default_factory=lambda: ["EURUSD"])
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    management: ManagementConfig = field(default_factory=ManagementConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    bot: BotConfig = field(default_factory=BotConfig)


def _build(cls, data: dict):
    fields = {f for f in cls.__dataclass_fields__}
    return cls(**{k: v for k, v in (data or {}).items() if k in fields})


def load_config(path: str | Path = "config.yaml") -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return Config(
        account=_build(AccountConfig, raw.get("account", {})),
        corpus=float(raw.get("corpus", 5000.0)),
        risk=_build(RiskConfig, raw.get("risk", {})),
        symbols=list(raw.get("symbols", ["EURUSD"])),
        strategy=_build(StrategyConfig, raw.get("strategy", {})),
        management=_build(ManagementConfig, raw.get("management", {})),
        session=_build(SessionConfig, raw.get("session", {})),
        bot=_build(BotConfig, raw.get("bot", {})),
    )
