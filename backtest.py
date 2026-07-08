#!/usr/bin/env python3
"""Backtest the scalping strategy over historical candles.

Data source is either:
  * a CSV of candles via --csv — columns time/open/high/low/close (a "Date"
    column and MT4/MT5 export headers are handled automatically), any bar
    size (M1, M5, ...) — the time-stop and trend timeframe scale with it, or
  * the MT5 terminal history, via --symbol/--days (Windows + MT5 only).

Indicators are precomputed vectorised over the full series (identical values
to the live bot's per-candle computation, since EMA/RSI/ATR are causal), so
multi-year datasets run in seconds.

This is a sanity-check tool, not a broker simulator: fills are assumed at the
candle open after the signal, spread/commission are approximated by
--spread-points, intrabar SL/TP hits assume the worst case (SL checked before
TP when both are inside one candle's range), and the news calendar, session
windows (unless --use-sessions) and daily-loss brake are not simulated.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from scalper.config import load_config
from scalper.indicators import atr, ema, macd, rsi
from scalper.risk import RiskManager, SymbolSpec
from scalper.strategy import build_strategy


# Typical contract specs for CSV mode (no terminal to ask). Real values vary
# by broker — MT5 mode always uses the broker's actual spec instead.
SPEC_PRESETS = {
    "FX5": SymbolSpec(  # 5-digit FX major: 100k contract, $1 per point per lot
        name="FX5", point=0.00001, tick_size=0.00001, tick_value=1.0,
        volume_min=0.01, volume_max=100.0, volume_step=0.01,
    ),
    "XAUUSD": SymbolSpec(  # gold: 100 oz/lot, point 0.01 -> $1 per point per lot
        name="XAUUSD", point=0.01, tick_size=0.01, tick_value=1.0,
        volume_min=0.01, volume_max=100.0, volume_step=0.01,
    ),
    "XAGUSD": SymbolSpec(  # silver: 5000 oz/lot, point 0.001 -> $5 per point per lot
        name="XAGUSD", point=0.001, tick_size=0.001, tick_value=5.0,
        volume_min=0.01, volume_max=100.0, volume_step=0.01,
    ),
    "US30": SymbolSpec(  # index CFD: $1 per index point per lot, point 0.1
        name="US30", point=0.1, tick_size=0.1, tick_value=0.1,
        volume_min=0.1, volume_max=100.0, volume_step=0.1,
    ),
}


def preset_for(symbol: str) -> SymbolSpec:
    for key, spec in SPEC_PRESETS.items():
        if key != "FX5" and key in symbol.upper():
            return spec
    return SPEC_PRESETS["FX5"]


@dataclass
class Trade:
    direction: str
    entry_time: pd.Timestamp
    entry: float
    sl: float
    tp: float
    lots: float
    exit_time: pd.Timestamp = None
    exit: float = 0.0
    pnl: float = 0.0
    exit_reason: str = ""


def load_candles_csv(path: str) -> pd.DataFrame:
    """Load candles from CSV, tolerating common export quirks: MT4/MT5
    (BOM, capitalised headers, 'Date' column, dotted datetimes, volume) and
    Finam/HistData style (<TICKER>,<PER>,<DATE>,<TIME>,... with split
    date/time columns)."""
    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = [c.strip().lower().strip("<>") for c in df.columns]
    if "time" in df.columns and "date" in df.columns:
        # split date/time columns (e.g. 20170101 + 211000)
        df["time"] = pd.to_datetime(
            df["date"].astype(str).str.zfill(8)
            + df["time"].astype(str).str.zfill(6),
            format="%Y%m%d%H%M%S",
        )
    elif "time" not in df.columns and "date" in df.columns:
        df = df.rename(columns={"date": "time"})
    missing = {"time", "open", "high", "low", "close"} - set(df.columns)
    if missing:
        raise SystemExit(f"CSV is missing columns: {sorted(missing)}")
    if not pd.api.types.is_datetime64_any_dtype(df["time"]):
        try:
            df["time"] = pd.to_datetime(df["time"], format="%Y.%m.%d %H:%M")
        except ValueError:
            df["time"] = pd.to_datetime(df["time"])
    return (
        df[["time", "open", "high", "low", "close"]]
        .dropna()
        .drop_duplicates(subset="time")
        .sort_values("time")
        .reset_index(drop=True)
    )


def detect_bar_minutes(times: pd.Series) -> int:
    return int(times.diff().dt.total_seconds().median() // 60) or 1


def tf_minutes(tf: str) -> int:
    """'M15' -> 15, 'H1' -> 60, 'H4' -> 240, 'D1' -> 1440."""
    tf = tf.strip().upper()
    return {"M": 1, "H": 60, "D": 1440}[tf[0]] * int(tf[1:])


def resample_bars(bars: pd.DataFrame, minutes: int) -> pd.DataFrame:
    g = bars.set_index("time").resample(f"{minutes}min")
    return pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
    }).dropna().reset_index()


def _active_gates(bars: pd.DataFrame, scfg, point: float, atr_v: pd.Series) -> pd.Series:
    """ATR floor + spike guard, shared by all engines (mirrors common_gates)."""
    active = atr_v >= scfg.min_atr_points * point
    if scfg.max_candle_atr_mult > 0 and scfg.spike_lookback_bars > 0:
        recent_range = (bars["high"] - bars["low"]).rolling(
            scfg.spike_lookback_bars, min_periods=1
        ).max()
        active &= recent_range <= scfg.max_candle_atr_mult * atr_v
    return active


def precompute_signals(bars: pd.DataFrame, scfg, point: float, bar_minutes: int):
    """Vectorised signal generation for every bar (dispatches on engine).

    Returns (long_ok, short_ok, sl_distance) numpy arrays.
    """
    if scfg.engine == "triple":
        return _signals_triple(bars, scfg, point)
    return _signals_crossover(bars, scfg, point, bar_minutes)


def _signals_triple(bars: pd.DataFrame, scfg, point: float):
    """Vectorised equivalent of TripleConfirmationStrategy.evaluate."""
    close = bars["close"]
    atr_v = atr(bars, scfg.atr_period)
    trend_ema = ema(close, scfg.entry_trend_ema)
    rsi_v = rsi(close, scfg.rsi_period)
    line, sig = macd(close, scfg.macd_fast, scfg.macd_slow, scfg.macd_signal)

    cross_up = (line.shift(1) <= sig.shift(1)) & (line > sig)
    cross_down = (line.shift(1) >= sig.shift(1)) & (line < sig)
    prev = rsi_v.shift(1)
    dip_low = prev.rolling(scfg.rsi_dip_lookback, min_periods=1).min() <= scfg.rsi_oversold
    dip_high = prev.rolling(scfg.rsi_dip_lookback, min_periods=1).max() >= scfg.rsi_overbought
    rising = rsi_v > prev
    falling = rsi_v < prev
    between = (rsi_v > scfg.rsi_oversold) & (rsi_v < scfg.rsi_overbought)
    active = _active_gates(bars, scfg, point, atr_v)

    long_ok = ((close > trend_ema) & cross_up & dip_low & rising
               & between & active).to_numpy()
    short_ok = ((close < trend_ema) & cross_down & dip_high & falling
                & between & active).to_numpy()
    sl_distance = np.maximum(
        scfg.sl_atr_mult * atr_v.to_numpy(), scfg.min_sl_points * point
    )
    return long_ok, short_ok, sl_distance


def _signals_crossover(bars: pd.DataFrame, scfg, point: float, bar_minutes: int):
    """Vectorised equivalent of ScalpStrategy.evaluate.

    The trend timeframe comes from config (trend_timeframe); if that isn't
    coarser than the bars, it falls back to 5x the bar size, mirroring the
    live M1/M5 pairing.
    """
    close = bars["close"]
    fast = ema(close, scfg.ema_fast)
    slow = ema(close, scfg.ema_slow)
    rsi_v = rsi(close, scfg.rsi_period)
    atr_v = atr(bars, scfg.atr_period)

    crossed_up = (fast.shift(1) <= slow.shift(1)) & (fast > slow)
    crossed_down = (fast.shift(1) >= slow.shift(1)) & (fast < slow)

    # --- higher-timeframe trend from fully closed trend candles --------------
    try:
        trend_minutes = tf_minutes(scfg.trend_timeframe)
    except (KeyError, ValueError):
        trend_minutes = 0
    if trend_minutes <= bar_minutes:
        trend_minutes = 5 * bar_minutes
    rule = f"{trend_minutes}min"
    g = bars.set_index("time").resample(rule)
    trend_df = pd.DataFrame({"close": g["close"].last()}).dropna().reset_index()
    t_fast = ema(trend_df["close"], scfg.trend_ema_fast)
    t_slow = ema(trend_df["close"], scfg.trend_ema_slow)
    trend_df["dir"] = np.sign(t_fast - t_slow)
    trend_df["pos"] = np.arange(len(trend_df))

    # a bar at time t may only see trend candles that started before
    # floor(t, rule) — i.e. candles already fully closed (same rule as live)
    cutoff = pd.DataFrame({"cutoff": bars["time"].dt.floor(rule)})
    mapped = pd.merge_asof(
        cutoff, trend_df[["time", "dir", "pos"]],
        left_on="cutoff", right_on="time",
        direction="backward", allow_exact_matches=False,
    )
    trend = mapped["dir"].fillna(0).to_numpy()
    # need enough closed trend candles for the slow EMA to be meaningful
    trend_ready = (mapped["pos"].fillna(-1) + 1) >= scfg.trend_ema_slow + 5
    trend = np.where(trend_ready.to_numpy(), trend, 0)

    # --- common gates ----------------------------------------------------------
    active = _active_gates(bars, scfg, point, atr_v).to_numpy()

    long_ok = (trend > 0) & crossed_up.to_numpy() & active \
        & (rsi_v >= scfg.rsi_long_min).to_numpy() \
        & (rsi_v <= scfg.rsi_long_max).to_numpy()
    short_ok = (trend < 0) & crossed_down.to_numpy() & active \
        & (rsi_v >= scfg.rsi_short_min).to_numpy() \
        & (rsi_v <= scfg.rsi_short_max).to_numpy()

    sl_distance = np.maximum(
        scfg.sl_atr_mult * atr_v.to_numpy(), scfg.min_sl_points * point
    )
    return long_ok, short_ok, sl_distance


def in_session(times: pd.Series, windows) -> np.ndarray:
    minute = (times.dt.hour * 60 + times.dt.minute).to_numpy()
    ok = np.zeros(len(times), dtype=bool)
    for start, end in windows:
        ok |= (minute >= start) & (minute < end)
    return ok


def run_backtest(bars: pd.DataFrame, cfg, spec: SymbolSpec, spread_points: int,
                 use_sessions: bool = False) -> list[Trade]:
    scfg = cfg.strategy_for(spec.name)
    risk = RiskManager(cfg.corpus, cfg.risk)
    bar_minutes = detect_bar_minutes(bars["time"])
    hold_bars = max(1, round(cfg.management.max_hold_minutes / bar_minutes))
    warmup = build_strategy(scfg).min_bars()
    spread = spread_points * spec.point

    long_ok, short_ok, sl_dist = precompute_signals(bars, scfg, spec.point, bar_minutes)
    if use_sessions:
        session_ok = in_session(bars["time"], cfg.session.windows())
        long_ok &= session_ok
        short_ok &= session_ok

    time_arr = bars["time"].to_numpy()
    open_arr = bars["open"].to_numpy()
    high_arr = bars["high"].to_numpy()
    low_arr = bars["low"].to_numpy()
    close_arr = bars["close"].to_numpy()

    equity = cfg.corpus
    trades: list[Trade] = []
    t: Trade | None = None
    entry_i = -1

    for i in range(warmup, len(bars) - 1):
        if t is not None:
            if t.direction == "buy":
                hit_sl, hit_tp = low_arr[i] <= t.sl, high_arr[i] >= t.tp
            else:
                hit_sl, hit_tp = high_arr[i] >= t.sl, low_arr[i] <= t.tp
            exit_price, reason = None, ""
            if hit_sl:      # worst case first
                exit_price, reason = t.sl, "stop_loss"
            elif hit_tp:
                exit_price, reason = t.tp, "take_profit"
            elif i - entry_i >= hold_bars:
                exit_price, reason = close_arr[i], "time_stop"
            if exit_price is not None:
                sign = 1 if t.direction == "buy" else -1
                t.exit_time, t.exit, t.exit_reason = time_arr[i], exit_price, reason
                t.pnl = sign * (exit_price - t.entry) / spec.tick_size \
                    * spec.tick_value * t.lots
                equity += t.pnl
                trades.append(t)
                t = None
            else:
                continue

        if t is not None or not (long_ok[i] or short_ok[i]):
            continue

        sizing = risk.lot_size(spec, sl_dist[i], equity)
        if sizing.lots <= 0:
            continue

        direction = "buy" if long_ok[i] else "sell"
        next_open = open_arr[i + 1]
        if direction == "buy":
            entry = next_open + spread / 2
            sl, tp = entry - sl_dist[i], entry + scfg.reward_risk * sl_dist[i]
        else:
            entry = next_open - spread / 2
            sl, tp = entry + sl_dist[i], entry - scfg.reward_risk * sl_dist[i]
        t = Trade(direction, time_arr[i + 1], entry, sl, tp, sizing.lots)
        entry_i = i + 1

    return trades


def report(trades: list[Trade], corpus: float) -> None:
    if not trades:
        print("No trades generated.")
        return
    df = pd.DataFrame([vars(t) for t in trades])
    df["exit_time"] = pd.to_datetime(df["exit_time"])
    equity_curve = corpus + df["pnl"].cumsum()
    peak = equity_curve.cummax()
    max_dd = (peak - equity_curve).max()
    wins = df[df["pnl"] > 0]
    losses = df[df["pnl"] <= 0]
    gross_win = wins["pnl"].sum()
    gross_loss = -losses["pnl"].sum()
    days = max(1, (df["exit_time"].iloc[-1] - df["exit_time"].iloc[0]).days)

    print(f"Trades:         {len(df)}  (~{len(df) / days * 7:.1f}/week)")
    print(f"Win rate:       {len(wins) / len(df) * 100:.1f}%")
    print(f"Profit factor:  "
          f"{gross_win / gross_loss:.2f}" if gross_loss > 0 else "Profit factor:  inf")
    print(f"Total PnL:      {df['pnl'].sum():+.2f} "
          f"({df['pnl'].sum() / corpus * 100:+.2f}% of corpus)")
    if len(wins):
        print(f"Avg win:        {wins['pnl'].mean():+.2f}")
    if len(losses):
        print(f"Avg loss:       {losses['pnl'].mean():+.2f}")
    print(f"Max drawdown:   {max_dd:.2f} ({max_dd / corpus * 100:.1f}% of corpus)")
    print(f"Final equity:   {equity_curve.iloc[-1]:.2f}")
    print("\nBy exit reason:")
    print(df.groupby("exit_reason")["pnl"].agg(["count", "sum"]).round(2).to_string())
    if days > 400:
        yearly = df.set_index("exit_time").groupby(pd.Grouper(freq="1YE"))["pnl"] \
            .agg(["count", "sum"]).round(2)
        yearly.index = yearly.index.year
        yearly.columns = ["trades", "pnl"]
        print("\nBy year:")
        print(yearly.to_string())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--csv", help="CSV of candles: time,open,high,low,close (any bar size)")
    p.add_argument("--symbol", default="EURUSD", help="symbol (MT5 mode or CSV label)")
    p.add_argument("--days", type=int, default=10, help="days of history (MT5 mode)")
    p.add_argument("--spread-points", type=int, default=10)
    p.add_argument("--point", type=float, help="override contract point size (CSV mode)")
    p.add_argument("--tick-value", type=float,
                   help="override $ value of one point for 1 lot (CSV mode)")
    p.add_argument("--use-sessions", action="store_true",
                   help="apply config session windows to entries (assumes the "
                        "CSV timestamps are UTC — broker exports often are not!)")
    args = p.parse_args()

    cfg = load_config(args.config)

    if args.csv:
        bars = load_candles_csv(args.csv)
        spec = replace(preset_for(args.symbol), name=args.symbol)
        if args.point:
            spec = replace(spec, point=args.point, tick_size=args.point)
        if args.tick_value:
            spec = replace(spec, tick_value=args.tick_value)
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
        bars = pd.DataFrame(rates)
        bars["time"] = pd.to_datetime(bars["time"], unit="s", utc=True)
        bars = bars[["time", "open", "high", "low", "close"]]
        spec = client.symbol_spec(args.symbol)
        client.shutdown()

    # resample the data up to the configured entry timeframe if it's finer
    scfg = cfg.strategy_for(spec.name)
    bar_minutes = detect_bar_minutes(bars["time"])
    entry_minutes = tf_minutes(scfg.entry_timeframe)
    if entry_minutes > bar_minutes:
        bars = resample_bars(bars, entry_minutes)
        bar_minutes = entry_minutes
    elif entry_minutes < bar_minutes:
        print(f"NOTE: data is M{bar_minutes} but entry_timeframe is "
              f"{scfg.entry_timeframe} — using the data's bar size")

    trend_minutes = max(tf_minutes(scfg.trend_timeframe), 5 * bar_minutes) \
        if scfg.engine == "crossover" else None
    print(f"Backtesting {args.symbol} [{scfg.engine}]: {len(bars)} "
          f"M{bar_minutes} candles ({bars.time.iloc[0]} .. {bars.time.iloc[-1]})"
          + (f", trend M{trend_minutes}" if trend_minutes else "")
          + f", spread {args.spread_points} points")
    trades = run_backtest(bars, cfg, spec, args.spread_points, args.use_sessions)
    report(trades, cfg.corpus)


if __name__ == "__main__":
    main()
