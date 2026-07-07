"""Main bot loop: poll for closed M1 candles, evaluate signals, manage trades."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import pandas as pd

from .config import Config
from .indicators import atr
from .mt5_client import MT5Client
from .risk import RiskManager
from .strategy import ScalpStrategy, Signal
from .trade_manager import TradeManager

log = logging.getLogger(__name__)


class ScalpingBot:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = MT5Client(cfg.account, cfg.bot)
        self.strategy = ScalpStrategy(cfg.strategy)
        self.risk = RiskManager(cfg.corpus, cfg.risk)
        self.manager = TradeManager(cfg.management, self.client)
        self._last_candle_time: Dict[str, pd.Timestamp] = {}
        self._session_windows = cfg.session.windows()

    # ---- session gating -----------------------------------------------------

    def in_session(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if now.weekday() == 4 and now.hour >= self.cfg.session.friday_flat_hour_utc:
            return False
        if not self._session_windows:
            return True
        minute = now.hour * 60 + now.minute
        return any(start <= minute < end for start, end in self._session_windows)

    def friday_flat(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return now.weekday() == 4 and now.hour >= self.cfg.session.friday_flat_hour_utc

    # ---- main loop ------------------------------------------------------------

    def run(self) -> None:
        self.client.connect()
        try:
            for symbol in self.cfg.symbols:
                self.client.ensure_symbol(symbol)
            log.info(
                "Bot running: symbols=%s corpus=%.0f risk/trade=%.2f%% "
                "daily-stop=%.2f%%",
                self.cfg.symbols, self.cfg.corpus,
                self.cfg.risk.risk_per_trade_pct, self.cfg.risk.max_daily_loss_pct,
            )
            while True:
                try:
                    self._tick()
                except Exception:  # keep the loop alive on transient errors
                    log.exception("Error in main loop iteration")
                time.sleep(self.cfg.bot.poll_seconds)
        except KeyboardInterrupt:
            log.info("Interrupted by user — shutting down (positions left open).")
        finally:
            self.client.shutdown()

    def _tick(self) -> None:
        if self.friday_flat():
            self.manager.flatten_all("Friday flat time")
            return

        # 1) manage what's already open (every poll, regardless of session)
        for pos in self.client.my_positions():
            m1 = self.client.closed_candles(
                pos.symbol, self.cfg.strategy.entry_timeframe, self.strategy.min_bars()
            )
            cur_atr = float(atr(m1, self.cfg.strategy.atr_period).iloc[-1])
            spec = self.client.symbol_spec(pos.symbol)
            self.manager.manage(pos, cur_atr, spec.point)

        # 2) look for new entries only inside the session windows
        if not self.in_session():
            return
        for symbol in self.cfg.symbols:
            self._maybe_enter(symbol)

    def _maybe_enter(self, symbol: str) -> None:
        m1 = self.client.closed_candles(
            symbol, self.cfg.strategy.entry_timeframe, self.strategy.min_bars() + 10
        )
        last_time = m1["time"].iloc[-1]
        if self._last_candle_time.get(symbol) == last_time:
            return  # no new closed candle yet — evaluate once per candle
        self._last_candle_time[symbol] = last_time

        spec = self.client.symbol_spec(symbol)
        signal = self.strategy.evaluate(
            symbol,
            m1,
            self.client.closed_candles(
                symbol, self.cfg.strategy.trend_timeframe,
                self.cfg.strategy.trend_ema_slow + 10,
            ),
            spec.point,
        )
        if signal is None:
            return
        log.info("Signal: %s %s (%s)", signal.direction.upper(), symbol, signal.note)

        # ---- risk gates ------------------------------------------------------
        spread = self.client.spread_points(symbol)
        if spread is not None and spread > self.cfg.risk.max_spread_points:
            log.info("Skip %s: spread %s > %s points", symbol, spread,
                     self.cfg.risk.max_spread_points)
            return

        equity = self.client.equity()
        pnl_today, trades_today = self.client.todays_stats()
        ok, why = self.risk.can_open(
            open_positions_total=len(self.client.my_positions()),
            open_positions_symbol=len(self.client.my_positions(symbol)),
            trades_today=trades_today,
            todays_pnl=pnl_today,
            equity=equity,
        )
        if not ok:
            log.info("Skip %s: %s", symbol, why)
            return

        sizing = self.risk.lot_size(spec, signal.sl_distance, equity)
        if sizing.lots <= 0:
            log.info("Skip %s: %s", symbol, sizing.reason)
            return

        self._place(symbol, signal, sizing.lots, spec.point)

    def _place(self, symbol: str, signal: Signal, lots: float, point: float) -> None:
        import MetaTrader5 as mt5

        tick = self.client.tick(symbol)
        info = mt5.symbol_info(symbol)
        digits = info.digits if info else 5

        if signal.direction == "buy":
            entry = tick.ask
            sl = round(entry - signal.sl_distance, digits)
            tp = round(entry + signal.tp_distance, digits)
        else:
            entry = tick.bid
            sl = round(entry + signal.sl_distance, digits)
            tp = round(entry - signal.tp_distance, digits)

        result = self.client.market_order(symbol, signal.direction, lots, sl, tp)
        if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(
                "OPENED %s %s %.2f lots @ %.5f sl=%.5f tp=%.5f",
                signal.direction.upper(), symbol, lots, result.price, sl, tp,
            )
