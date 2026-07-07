"""Thin wrapper around the MetaTrader5 package.

Everything that touches the terminal lives here so the strategy/risk modules
stay platform-independent and testable. The MetaTrader5 package only works on
Windows (or under Wine) with a running MT5 terminal.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pandas as pd

from .config import AccountConfig, BotConfig
from .risk import SymbolSpec

try:  # pragma: no cover - import only succeeds on Windows
    import MetaTrader5 as mt5
except ImportError:  # keep non-Windows imports working (tests, backtests)
    mt5 = None

log = logging.getLogger(__name__)

TIMEFRAMES = {}
if mt5 is not None:
    TIMEFRAMES = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
    }


class MT5Error(RuntimeError):
    pass


class MT5Client:
    def __init__(self, account: AccountConfig, bot: BotConfig) -> None:
        if mt5 is None:
            raise MT5Error(
                "The MetaTrader5 package is not available. It only runs on "
                "Windows (or Wine) with an installed MT5 terminal."
            )
        self.account = account
        self.bot = bot

    # ---- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        kwargs = {}
        if self.account.terminal_path:
            kwargs["path"] = self.account.terminal_path
        if self.account.login:
            kwargs.update(
                login=int(self.account.login),
                password=self.account.password,
                server=self.account.server,
            )
        if not mt5.initialize(**kwargs):
            raise MT5Error(f"MT5 initialize failed: {mt5.last_error()}")

        info = mt5.account_info()
        if info is None:
            raise MT5Error(f"account_info failed: {mt5.last_error()}")
        # Refuse to run on a live account: this bot is demo-only by design.
        if info.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            mt5.shutdown()
            raise MT5Error(
                f"Account {info.login} is not a DEMO account "
                f"(trade_mode={info.trade_mode}). Refusing to trade."
            )
        log.info(
            "Connected to DEMO account %s (%s), balance=%.2f %s",
            info.login, info.server, info.balance, info.currency,
        )

    def shutdown(self) -> None:
        mt5.shutdown()

    # ---- account / symbol info ---------------------------------------------

    def equity(self) -> float:
        info = mt5.account_info()
        if info is None:
            raise MT5Error(f"account_info failed: {mt5.last_error()}")
        return float(info.equity)

    def ensure_symbol(self, symbol: str) -> None:
        info = mt5.symbol_info(symbol)
        if info is None:
            raise MT5Error(f"Unknown symbol {symbol}")
        if not info.visible and not mt5.symbol_select(symbol, True):
            raise MT5Error(f"Failed to enable symbol {symbol} in Market Watch")

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        info = mt5.symbol_info(symbol)
        if info is None:
            raise MT5Error(f"symbol_info({symbol}) failed")
        return SymbolSpec(
            name=symbol,
            point=info.point,
            tick_size=info.trade_tick_size or info.point,
            tick_value=info.trade_tick_value,
            volume_min=info.volume_min,
            volume_max=info.volume_max,
            volume_step=info.volume_step,
        )

    def spread_points(self, symbol: str) -> Optional[int]:
        info = mt5.symbol_info(symbol)
        return None if info is None else info.spread

    def tick(self, symbol: str):
        t = mt5.symbol_info_tick(symbol)
        if t is None:
            raise MT5Error(f"symbol_info_tick({symbol}) failed")
        return t

    # ---- market data ---------------------------------------------------------

    def closed_candles(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """Return the last `count` CLOSED candles (drops the forming one)."""
        rates = mt5.copy_rates_from_pos(symbol, TIMEFRAMES[timeframe], 0, count + 1)
        if rates is None or len(rates) < 2:
            raise MT5Error(f"copy_rates_from_pos({symbol},{timeframe}) failed")
        df = pd.DataFrame(rates[:-1])  # last row is the still-forming candle
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df

    # ---- positions & history ---------------------------------------------------

    def my_positions(self, symbol: Optional[str] = None) -> List:
        pos = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        return [p for p in (pos or []) if p.magic == self.bot.magic]

    def todays_stats(self) -> tuple[float, int]:
        """(realized pnl today, entries today) for this bot's magic number."""
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        deals = mt5.history_deals_get(start, datetime.now(timezone.utc) + timedelta(minutes=5))
        pnl, entries = 0.0, 0
        for d in deals or []:
            if d.magic != self.bot.magic:
                continue
            pnl += d.profit + d.swap + d.commission
            if d.entry == mt5.DEAL_ENTRY_IN:
                entries += 1
        return pnl, entries

    # ---- orders ---------------------------------------------------------------

    def _filling_mode(self, symbol: str) -> int:
        info = mt5.symbol_info(symbol)
        modes = info.filling_mode if info else 0
        if modes & mt5.SYMBOL_FILLING_IOC:
            return mt5.ORDER_FILLING_IOC
        if modes & mt5.SYMBOL_FILLING_FOK:
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    def market_order(
        self, symbol: str, direction: str, lots: float, sl: float, tp: float
    ):
        tick = self.tick(symbol)
        buy = direction == "buy"
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lots,
            "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
            "price": tick.ask if buy else tick.bid,
            "sl": sl,
            "tp": tp,
            "deviation": self.bot.deviation_points,
            "magic": self.bot.magic,
            "comment": self.bot.comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(symbol),
        }
        return self._send(request)

    def modify_sl_tp(self, position, sl: float, tp: float):
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": position.ticket,
            "symbol": position.symbol,
            "sl": sl,
            "tp": tp,
            "magic": self.bot.magic,
        }
        return self._send(request)

    def close_position(self, position):
        tick = self.tick(position.symbol)
        closing_buy = position.type == mt5.POSITION_TYPE_SELL
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": position.ticket,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": mt5.ORDER_TYPE_BUY if closing_buy else mt5.ORDER_TYPE_SELL,
            "price": tick.ask if closing_buy else tick.bid,
            "deviation": self.bot.deviation_points,
            "magic": self.bot.magic,
            "comment": f"{self.bot.comment}-close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(position.symbol),
        }
        return self._send(request)

    def _send(self, request: dict, retries: int = 2):
        for attempt in range(retries + 1):
            result = mt5.order_send(request)
            if result is None:
                log.error("order_send returned None: %s", mt5.last_error())
            elif result.retcode == mt5.TRADE_RETCODE_DONE:
                return result
            elif result.retcode in (
                mt5.TRADE_RETCODE_REQUOTE,
                mt5.TRADE_RETCODE_PRICE_CHANGED,
                mt5.TRADE_RETCODE_PRICE_OFF,
            ) and attempt < retries:
                log.warning("Retryable retcode %s, retrying...", result.retcode)
                time.sleep(0.5)
                # refresh price for deal requests
                if request["action"] == mt5.TRADE_ACTION_DEAL:
                    tick = self.tick(request["symbol"])
                    request["price"] = (
                        tick.ask if request["type"] == mt5.ORDER_TYPE_BUY else tick.bid
                    )
                continue
            else:
                log.error(
                    "Order failed: retcode=%s comment=%s request=%s",
                    result.retcode, result.comment, request,
                )
                return result
        return result
