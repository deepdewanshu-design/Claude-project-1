"""Typed configuration loaded from config.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Tuple

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
    # which signal engine to run: "crossover" (M1 EMA cross + M5 trend) or
    # "triple" (EMA50 trend + RSI recovering from oversold + MACD cross)
    engine: str = "crossover"
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
    # spike guard ("other factors"): skip entries if any of the last
    # spike_lookback_bars candles has a range > max_candle_atr_mult x ATR —
    # catches surprise news / flash moves the calendar doesn't list. 0 = off.
    max_candle_atr_mult: float = 3.0
    spike_lookback_bars: int = 10
    # --- "triple" engine parameters ---
    entry_trend_ema: int = 50      # trade with price relative to this EMA
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    rsi_oversold: float = 30.0     # RSI must have dipped below this recently...
    rsi_overbought: float = 70.0   # ...(mirror for shorts)
    rsi_dip_lookback: int = 10     # ...within this many bars before the trigger


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
class NewsConfig:
    enabled: bool = True
    block_minutes_before: float = 15.0
    block_minutes_after: float = 15.0
    impacts: List[str] = field(default_factory=lambda: ["High"])
    flatten_before_news: bool = True
    flatten_minutes_before: float = 5.0
    refresh_hours: float = 6.0
    fail_closed: bool = False
    cache_dir: str = "cache"
    # symbol -> list of currencies whose news moves it (FX pairs are derived
    # automatically from the symbol name; metals/indices need this map)
    currency_map: Dict[str, list] = field(default_factory=lambda: {
        "XAUUSD": ["USD"],
        "XAGUSD": ["USD"],
        "US30": ["USD"],
    })


@dataclass
class LearningConfig:
    enabled: bool = True
    journal_dir: str = "journal"
    lookback_days: int = 30
    min_trades_per_bucket: int = 8
    block_expectancy_r: float = -0.15
    streak_throttle_after: int = 3
    min_risk_multiplier: float = 0.25


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
    news: NewsConfig = field(default_factory=NewsConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    bot: BotConfig = field(default_factory=BotConfig)
    # Per-symbol overrides for point-scaled settings, e.g.
    #   symbol_overrides: {XAUUSD: {min_sl_points: 80, max_spread_points: 45}}
    symbol_overrides: Dict[str, dict] = field(default_factory=dict)

    def strategy_for(self, symbol: str) -> StrategyConfig:
        """Strategy config with this symbol's overrides applied (any
        StrategyConfig field can be overridden per symbol)."""
        overrides = {
            k: v for k, v in (self.symbol_overrides.get(symbol) or {}).items()
            if k in StrategyConfig.__dataclass_fields__
        }
        return replace(self.strategy, **overrides) if overrides else self.strategy

    def max_spread_for(self, symbol: str) -> float:
        override = (self.symbol_overrides.get(symbol) or {}).get("max_spread_points")
        return float(override if override is not None else self.risk.max_spread_points)


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
        news=_build(NewsConfig, raw.get("news", {})),
        learning=_build(LearningConfig, raw.get("learning", {})),
        bot=_build(BotConfig, raw.get("bot", {})),
        symbol_overrides=dict(raw.get("symbol_overrides", {}) or {}),
    )
