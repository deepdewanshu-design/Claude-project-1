#!/usr/bin/env python3
"""Quick-and-dirty backtest of the scalping strategy over M1 history.

Data source is either:
  * a CSV with columns time,open,high,low,close (M1 candles), via --csv, or
  * the MT5 terminal history, via --symbol/--days (Windows + MT5 only).

This is a sanity-check tool, not a broker simulator: fills are assumed at the
candle open after the signal, spread/commission are approximated by
--spread-points, and intrabar SL/TP hits assume the worst case (SL checked
before TP when both are inside one candle's range).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import pandas as pd

from scalper.config import load_config
from scalper.risk import RiskManager, SymbolSpec
from scalper.strategy import ScalpStrategy


@dataclass
class Trade:
    direction: str
    entry_idx: int
    entry: float
    sl: float
    tp: float
    lots: float
    exit_idx: int = -1
    exit: float = 0.0
    pnl: float = 0.0


def resample_m5(m1: pd.DataFrame) -> pd.DataFrame:
    g = m1.set_index("time").resample("5min")
    m5 = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
    }).dropna().reset_index()
    return m5


def run_backtest(m1: pd.DataFrame, cfg, spec: SymbolSpec, spread_points: int) -> None:
    strategy = ScalpStrategy(cfg.strategy)
    risk = RiskManager(cfg.corpus, cfg.risk)
    equity = cfg.corpus
    spread = spread_points * spec.point

    trades: list[Trade] = []
    open_trade: Trade | None = None
    warmup = strategy.min_bars()
    m5_all = resample_m5(m1)

    for i in range(warmup, len(m1) - 1):
        candle = m1.iloc[i]

        # --- manage open trade against this candle's range -------------------
        if open_trade is not None:
            t = open_trade
            hit_sl = candle.low <= t.sl if t.direction == "buy" else candle.high >= t.sl
            hit_tp = candle.high >= t.tp if t.direction == "buy" else candle.low <= t.tp
            exit_price = None
            if hit_sl:      # worst case first
                exit_price = t.sl
            elif hit_tp:
                exit_price = t.tp
            elif i - t.entry_idx >= cfg.management.max_hold_minutes:
                exit_price = candle.close
            if exit_price is not None:
                sign = 1 if t.direction == "buy" else -1
                t.exit_idx, t.exit = i, exit_price
                t.pnl = sign * (exit_price - t.entry) / spec.tick_size * spec.tick_value * t.lots
                equity += t.pnl
                trades.append(t)
                open_trade = None

        if open_trade is not None:
            continue

        # --- look for a signal on candles closed up to i ----------------------
        window = m1.iloc[: i + 1]
        cutoff = candle.time.floor("5min")  # only fully closed M5 candles
        m5 = m5_all[m5_all["time"] < cutoff]
        sig = strategy.evaluate(spec.name, window, m5, spec.point)
        if sig is None:
            continue

        sizing = risk.lot_size(spec, sig.sl_distance, equity)
        if sizing.lots <= 0:
            continue

        next_open = m1.iloc[i + 1].open
        if sig.direction == "buy":
            entry = next_open + spread / 2
            sl, tp = entry - sig.sl_distance, entry + sig.tp_distance
        else:
            entry = next_open - spread / 2
            sl, tp = entry + sig.sl_distance, entry - sig.tp_distance
        open_trade = Trade(sig.direction, i + 1, entry, sl, tp, sizing.lots)

    # --- report ---------------------------------------------------------------
    if not trades:
        print("No trades generated.")
        return
    pnl = sum(t.pnl for t in trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    print(f"Trades:        {len(trades)}")
    print(f"Win rate:      {len(wins) / len(trades) * 100:.1f}%")
    print(f"Total PnL:     {pnl:+.2f} ({pnl / cfg.corpus * 100:+.2f}% of corpus)")
    print(f"Avg win:       {sum(t.pnl for t in wins) / len(wins):+.2f}" if wins else "Avg win:       n/a")
    print(f"Avg loss:      {sum(t.pnl for t in losses) / len(losses):+.2f}" if losses else "Avg loss:      n/a")
    print(f"Final equity:  {equity:.2f}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--csv", help="CSV of M1 candles: time,open,high,low,close")
    p.add_argument("--symbol", default="EURUSD", help="symbol (MT5 mode or CSV label)")
    p.add_argument("--days", type=int, default=10, help="days of history (MT5 mode)")
    p.add_argument("--spread-points", type=int, default=10)
    args = p.parse_args()

    cfg = load_config(args.config)

    if args.csv:
        m1 = pd.read_csv(args.csv, parse_dates=["time"])
        spec = SymbolSpec(  # generic 5-digit FX defaults for CSV mode
            name=args.symbol, point=0.00001, tick_size=0.00001,
            tick_value=1.0, volume_min=0.01, volume_max=100.0, volume_step=0.01,
        )
    else:
        import MetaTrader5 as mt5
        from datetime import datetime, timedelta, timezone

        from scalper.mt5_client import MT5Client
        client = MT5Client(cfg.account, cfg.bot)
        client.connect()
        end = datetime.now(timezone.utc)
        rates = mt5.copy_rates_range(
            args.symbol, mt5.TIMEFRAME_M1, end - timedelta(days=args.days), end
        )
        m1 = pd.DataFrame(rates)
        m1["time"] = pd.to_datetime(m1["time"], unit="s", utc=True)
        spec = client.symbol_spec(args.symbol)
        client.shutdown()

    m1 = m1[["time", "open", "high", "low", "close"]].sort_values("time").reset_index(drop=True)
    print(f"Backtesting {args.symbol}: {len(m1)} M1 candles "
          f"({m1.time.iloc[0]} .. {m1.time.iloc[-1]})")
    run_backtest(m1, cfg, spec, args.spread_points)


if __name__ == "__main__":
    main()
