"""Main bot loop: poll for closed M1 candles, evaluate signals, manage trades."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import pandas as pd

from .config import Config
from .indicators import atr
from .journal import TradeJournal
from .learning import LearningEngine
from .mt5_client import MT5Client
from .news import NewsCalendar, NewsFilter
from .risk import RiskManager, SymbolSpec
from .strategy import ScalpStrategy, Signal
from .trade_manager import TradeManager

log = logging.getLogger(__name__)


class ScalpingBot:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = MT5Client(cfg.account, cfg.bot)
        self.strategies: Dict[str, ScalpStrategy] = {}
        self.risk = RiskManager(cfg.corpus, cfg.risk)
        self.manager = TradeManager(cfg.management, self.client)
        self._last_candle_time: Dict[str, pd.Timestamp] = {}
        self._session_windows = cfg.session.windows()

        self.news: Optional[NewsFilter] = None
        if cfg.news.enabled:
            calendar = NewsCalendar(cfg.news, cfg.news.cache_dir)
            self.news = NewsFilter(cfg.news, calendar)

        self.journal: Optional[TradeJournal] = None
        self.learning: Optional[LearningEngine] = None
        if cfg.learning.enabled:
            self.journal = TradeJournal(cfg.learning.journal_dir)
            self.learning = LearningEngine(cfg.learning, self.journal)
            self.learning.refresh()

    def strategy_for(self, symbol: str) -> ScalpStrategy:
        """One ScalpStrategy per symbol so per-symbol overrides (gold, indices)
        take effect."""
        if symbol not in self.strategies:
            self.strategies[symbol] = ScalpStrategy(self.cfg.strategy_for(symbol))
        return self.strategies[symbol]

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
        # 0) learn from anything that closed since the last poll
        self._reconcile_closed_trades()

        if self.friday_flat():
            self.manager.flatten_all("Friday flat time")
            return

        # 0b) news: keep the calendar fresh, get flat before releases
        if self.news is not None:
            self.news.calendar.maybe_refresh()
            for pos in self.client.my_positions():
                event = self.news.should_flatten(pos.symbol)
                if event is not None:
                    log.info(
                        "Closing %s #%s ahead of %s %s (%s) at %s",
                        pos.symbol, pos.ticket, event.currency, event.title,
                        event.impact, event.time.strftime("%H:%M UTC"),
                    )
                    self.client.close_position(pos)

        # 1) manage what's already open (every poll, regardless of session)
        for pos in self.client.my_positions():
            scfg = self.cfg.strategy_for(pos.symbol)
            m1 = self.client.closed_candles(
                pos.symbol, scfg.entry_timeframe, self.strategy_for(pos.symbol).min_bars()
            )
            cur_atr = float(atr(m1, scfg.atr_period).iloc[-1])
            spec = self.client.symbol_spec(pos.symbol)
            self.manager.manage(pos, cur_atr, spec.point)

        # 2) look for new entries only inside the session windows
        if not self.in_session():
            return
        for symbol in self.cfg.symbols:
            self._maybe_enter(symbol)

    def _reconcile_closed_trades(self) -> None:
        """Feed trades that closed (SL/TP/time/manual) into the journal and
        re-learn the rules."""
        if self.journal is None:
            return
        still_open = {p.ticket for p in self.client.my_positions()}
        closed_any = False
        for ticket in list(self.journal.open_tickets()):
            if int(ticket) in still_open:
                continue
            info = self.client.position_close_info(int(ticket))
            if info is None:
                continue  # deal history not visible yet — retry next poll
            self.journal.record_close(int(ticket), **info)
            closed_any = True
        if closed_any and self.learning is not None:
            self.learning.refresh()
            mult = self.learning.risk_multiplier()
            if mult < 1.0:
                log.info(
                    "Loss streak of %d: risk throttled to %.0f%% of normal",
                    self.learning.loss_streak, mult * 100,
                )

    def _maybe_enter(self, symbol: str) -> None:
        strategy = self.strategy_for(symbol)
        scfg = strategy.cfg
        m1 = self.client.closed_candles(
            symbol, scfg.entry_timeframe, strategy.min_bars() + 10
        )
        last_time = m1["time"].iloc[-1]
        if self._last_candle_time.get(symbol) == last_time:
            return  # no new closed candle yet — evaluate once per candle
        self._last_candle_time[symbol] = last_time

        spec = self.client.symbol_spec(symbol)
        signal = strategy.evaluate(
            symbol,
            m1,
            self.client.closed_candles(
                symbol, scfg.trend_timeframe, scfg.trend_ema_slow + 10,
            ),
            spec.point,
        )
        if signal is None:
            return
        log.info("Signal: %s %s (%s)", signal.direction.upper(), symbol, signal.note)

        # ---- news gate ---------------------------------------------------------
        if self.news is not None:
            ok, why = self.news.check_entry(symbol)
            if not ok:
                log.info("Skip %s: %s", symbol, why)
                return

        # ---- risk gates ------------------------------------------------------
        spread = self.client.spread_points(symbol)
        max_spread = self.cfg.max_spread_for(symbol)
        if spread is not None and spread > max_spread:
            log.info("Skip %s: spread %s > %s points", symbol, spread, max_spread)
            return

        # ---- learned rules (patterns that lost money before) ------------------
        risk_multiplier = 1.0
        if self.learning is not None:
            hour = datetime.now(timezone.utc).hour
            ok, why = self.learning.allows(
                symbol, signal.direction, hour, float(spread or 0)
            )
            if not ok:
                log.info("Skip %s: %s", symbol, why)
                return
            risk_multiplier = self.learning.risk_multiplier()

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

        sizing = self.risk.lot_size(
            spec, signal.sl_distance, equity, risk_multiplier=risk_multiplier
        )
        if sizing.lots <= 0:
            log.info("Skip %s: %s", symbol, sizing.reason)
            return

        self._place(symbol, signal, sizing, spec, float(spread or 0), risk_multiplier)

    def _place(
        self, symbol: str, signal: Signal, sizing, spec: SymbolSpec,
        spread_points: float, risk_multiplier: float,
    ) -> None:
        import MetaTrader5 as mt5

        tick = self.client.tick(symbol)
        info = mt5.symbol_info(symbol)
        digits = info.digits if info else 5
        lots = sizing.lots

        if signal.direction == "buy":
            entry = tick.ask
            sl = round(entry - signal.sl_distance, digits)
            tp = round(entry + signal.tp_distance, digits)
        else:
            entry = tick.bid
            sl = round(entry + signal.sl_distance, digits)
            tp = round(entry - signal.tp_distance, digits)

        result = self.client.market_order(symbol, signal.direction, lots, sl, tp)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            return
        log.info(
            "OPENED %s %s %.2f lots @ %.5f sl=%.5f tp=%.5f",
            signal.direction.upper(), symbol, lots, result.price, sl, tp,
        )
        if self.journal is not None:
            now = datetime.now(timezone.utc)
            self.journal.record_open(result.order, {
                "symbol": symbol,
                "direction": signal.direction,
                "open_time": now.isoformat(timespec="seconds"),
                "entry_price": result.price,
                "lots": lots,
                "sl_price": sl,
                "tp_price": tp,
                "sl_points": round(signal.sl_distance / spec.point, 1),
                "spread_points": spread_points,
                "atr_points": round(signal.atr_value / spec.point, 1),
                "rsi": round(signal.rsi_value, 1),
                "hour_utc": now.hour,
                "weekday": now.weekday(),
                "risk_multiplier": risk_multiplier,
                "planned_risk": round(sizing.risk_amount, 2),
            })
