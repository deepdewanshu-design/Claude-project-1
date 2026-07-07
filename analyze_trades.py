#!/usr/bin/env python3
"""Show what the bot has learned from its trades.

Usage:  python analyze_trades.py [--config config.yaml]

Reads the trade journal (journal/trades.csv), prints a performance breakdown
by symbol / direction / hour / exit reason, and lists the rules the learning
engine is currently enforcing.
"""

from __future__ import annotations

import argparse

import pandas as pd

from scalper.config import load_config
from scalper.journal import TradeJournal
from scalper.learning import LearningEngine


def bucket_table(df: pd.DataFrame, by, label: str) -> None:
    g = df.groupby(by)
    stats = pd.DataFrame({
        "trades": g.size(),
        "win %": (g["pnl"].apply(lambda s: (s > 0).mean()) * 100).round(1),
        "total pnl": g["pnl"].sum().round(2),
        "avg R": g["r_multiple"].mean().round(2),
    })
    print(f"\n--- By {label} ---")
    print(stats.to_string())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    journal = TradeJournal(cfg.learning.journal_dir)
    df = journal.load_closed()

    print("=" * 60)
    print("TRADE JOURNAL REPORT")
    print("=" * 60)

    if df.empty:
        print("\nNo closed trades recorded yet.")
        print("The journal fills up as the bot trades; check back later.")
        return

    wins = df[df["pnl"] > 0]
    print(f"\nClosed trades:  {len(df)}")
    print(f"Win rate:       {len(wins) / len(df) * 100:.1f}%")
    print(f"Total PnL:      {df['pnl'].sum():+.2f}")
    print(f"Average R:      {df['r_multiple'].mean():+.2f} "
          f"(1R = the money risked on one trade)")
    print(f"Best trade:     {df['pnl'].max():+.2f}")
    print(f"Worst trade:    {df['pnl'].min():+.2f}")

    bucket_table(df, "symbol", "symbol")
    bucket_table(df, ["symbol", "direction"], "direction")
    bucket_table(df, "hour_utc", "hour of day (UTC)")
    bucket_table(df, "exit_reason", "exit reason")

    print("\n" + "=" * 60)
    print("ACTIVE LEARNED RULES")
    print("=" * 60)
    engine = LearningEngine(cfg.learning, journal)
    engine.refresh()
    snap = engine.snapshot(trades_analyzed=len(df))
    if not snap["rules"]:
        print("\nNo rules active — nothing is losing consistently enough yet")
        print(f"(a pattern needs {cfg.learning.min_trades_per_bucket}+ trades "
              f"averaging below {cfg.learning.block_expectancy_r}R to be blocked).")
    for rule in snap["rules"]:
        target = rule.get("direction") or (
            f"{rule['hour_utc']:02d}:00 UTC" if "hour_utc" in rule
            else f"spread > {rule.get('max_spread_points', 0):.0f} pts"
        )
        print(f"\n* {rule['rule']}: {rule['symbol']} {target}")
        print(f"    reason: {rule['why']}")
    if snap["loss_streak"] >= cfg.learning.streak_throttle_after:
        print(f"\n* risk throttle: {snap['loss_streak']} losses in a row -> "
              f"trading at {snap['risk_multiplier'] * 100:.0f}% of normal risk")
    print()


if __name__ == "__main__":
    main()
