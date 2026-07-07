# MT5 M1/M5 Scalping Bot (demo accounts only)

A Python trading bot that plugs into a **MetaTrader 5 demo account** and
scalps on 1-minute candles with a 5-minute trend filter. All position sizing
and loss limits are computed against a **$5,000 corpus**, regardless of the
demo account's actual balance.

> ⚠️ **Demo only.** The bot checks the account's trade mode at startup and
> refuses to run on anything that is not a demo account. Scalping strategies
> that look fine on a demo feed routinely lose money live due to spread,
> slippage, and commissions. Nothing here is financial advice.

## How it trades

**Entry (per symbol, evaluated once per closed M1 candle):**

| Check | Long | Short |
|---|---|---|
| M5 trend filter | EMA20 > EMA50 | EMA20 < EMA50 |
| M1 trigger | EMA9 crosses above EMA21 | EMA9 crosses below EMA21 |
| M1 RSI(14) | 50–70 | 30–50 |
| Volatility | ATR(14) ≥ 15 points (skip dead markets) | same |
| Spread | ≤ 20 points, else skip | same |

**Exits:** ATR-based stop (1.5 × ATR, floored at 30 points), take-profit at
1.2 × the stop distance, break-even move at +0.7R, ATR trailing stop after
break-even, and a 20-minute time stop for scalps that go nowhere.

**Risk (all against `min($5,000, live equity)`):**

- **0.5% per trade** (~$25): lot size is computed from the stop distance and
  the symbol's tick value, rounded *down* to the broker's volume step.
- **2% daily loss stop** (~$100): realized PnL is tracked from today's deal
  history; entries halt for the rest of the day once breached.
- Max 2 open positions (1 per symbol), max 30 entries/day.
- Trades only during London/NY session windows (configurable), goes flat on
  Friday evenings.
- If even the broker's minimum lot would risk more than 1.5× the target, the
  trade is skipped rather than oversized.

## Requirements

- **Windows** with the [MetaTrader 5 terminal](https://www.metatrader5.com/)
  installed (the official `MetaTrader5` Python package is Windows-only; Linux
  users can run both under Wine).
- Python 3.10+
- An MT5 **demo** account (create one in the terminal:
  *File → Open an Account → choose a broker → Demo account*).

## Setup

```powershell
pip install -r requirements.txt
```

Edit `config.yaml`:

```yaml
account:
  login: 12345678          # your demo login
  password: "your-demo-password"
  server: "MetaQuotes-Demo"
```

Or leave `login: 0` and simply keep the MT5 terminal running and logged in to
your demo account — the bot will attach to it.

## Run

```powershell
python run_bot.py                 # uses config.yaml
python run_bot.py --verbose      # debug logging
```

The terminal must allow algorithmic trading
(*Tools → Options → Expert Advisors → Allow algorithmic trading*).

Stop with `Ctrl+C`. Open positions keep their server-side SL/TP, so they are
protected even when the bot is offline.

## Backtest / sanity check

Replay the strategy over M1 history (through the terminal, or any CSV with
`time,open,high,low,close` columns — so this part also runs on Linux/macOS):

```powershell
python backtest.py --symbol EURUSD --days 10          # pull history from MT5
python backtest.py --csv my_m1_data.csv               # offline
```

This is a sanity-check tool, not a broker simulator: fills, spread, and
intrabar SL/TP ordering are approximated (worst case: stop checked first).

## Tests

The strategy, indicators, sizing math, and config parsing are pure Python and
run anywhere:

```bash
pip install pytest && python -m pytest tests/
```

## Layout

```
run_bot.py              entry point
backtest.py             offline strategy sanity check
config.yaml             all tunables (risk, strategy, sessions, symbols)
scalper/
  config.py             typed config loading
  indicators.py         EMA / RSI / ATR (pandas, no MT5 dependency)
  strategy.py           M1 entry + M5 trend filter -> Signal
  risk.py               lot sizing + daily loss / trade-count guards
  trade_manager.py      break-even, ATR trailing, time stop
  mt5_client.py         all MetaTrader5 API calls (orders, data, history)
  bot.py                main polling loop
tests/                  unit tests for the platform-independent parts
```

## Tuning

Everything lives in `config.yaml`. The most impactful knobs:

- `risk.risk_per_trade_pct` / `risk.max_daily_loss_pct` — how hard it can lose.
- `strategy.reward_risk` — 1.0–1.5 is typical for scalping; higher means fewer
  but larger winners.
- `strategy.sl_atr_mult` and `min_sl_points` — tighter stops mean bigger lots
  and more noise-outs.
- `session.trade_hours_utc` — scalping outside liquid sessions mostly donates
  spread to the broker.
- `symbols` — stick to majors with tight spreads on your demo server.
