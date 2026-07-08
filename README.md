# MT5 M1/M5 Scalping Bot (demo accounts only)

A Python trading bot that plugs into a **MetaTrader 5 demo account** and
scalps on 1-minute candles with a 5-minute trend filter. All position sizing
and loss limits are computed against a **$5,000 corpus**, regardless of the
demo account's actual balance.

> ⚠️ **Demo only.** The bot checks the account's trade mode at startup and
> refuses to run on anything that is not a demo account. Scalping strategies
> that look fine on a demo feed routinely lose money live due to spread,
> slippage, and commissions. Nothing here is financial advice.

**New to this?** See **[OPERATING_GUIDE.md](OPERATING_GUIDE.md)** for
step-by-step instructions that assume no coding knowledge.

## How it trades

Two signal engines are available (`strategy.engine` in `config.yaml`, also
switchable per symbol via `symbol_overrides`):

- **`triple`** — pullback engine: price above the 50 EMA (trend), RSI
  recovering upward after dipping oversold (momentum), MACD line crossing
  its signal line (trigger); all three on the last closed entry candle,
  mirrored for shorts. Highly selective (a few trades/week).
- **`crossover`** — the original: M1 EMA9/21 cross filtered by M5 trend,
  detailed below. Trades far more often.

Both share the same risk plumbing: ATR stops/targets, spike guard, ATR
floor, spread/news/session/learning gates. **Backtests on real EURUSD-M1
and XAUUSD-M5 history show neither engine has positive expectancy with
default parameters** (see the backtester) — treat the defaults as a
framework to iterate on, not a proven edge.

**Crossover entry (per symbol, evaluated once per closed M1 candle):**

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

## News & market-condition protection

Scalping through a high-impact release is how tight stops get skipped by
multiples, so two independent guards run in front of every entry:

- **Economic calendar** (`scalper/news.py`) — the bot downloads the free
  Forex Factory weekly calendar (no API key), caches it under `cache/`, and
  refreshes on a TTL. It blocks new entries ±15 min around High-impact events
  affecting a symbol's currencies (FX pairs derive their currencies from the
  symbol name; metals/indices use `news.currency_map`), and closes open
  positions 5 minutes before such events. If the feed is unreachable it keeps
  trading and warns (`fail_closed: true` halts entries instead). Windows,
  impact levels, and the flatten behaviour are tunable under `news:` in
  `config.yaml`.
- **Spike guard** (in the strategy) — no entries while any of the last 10
  M1 candles has a range above 3× ATR. This catches what the calendar can't:
  surprise headlines, flash moves, fat-finger candles. Being market-derived,
  it also works fully offline. Tune via `strategy.max_candle_atr_mult` /
  `spike_lookback_bars` (per-symbol overridable like everything else).

The backtester does not simulate either guard's calendar side — another
reason live/demo trade counts will differ from backtests.

## Learning from losing trades

Every trade is journaled with its entry context (`journal/trades.csv`:
hour, direction, spread, ATR, RSI, exit reason, R-multiple). After each
closed trade the learning engine re-mines a rolling 30-day window and
enforces explainable rules — no black-box ML:

- **Hour blocks** — a (symbol, hour) bucket with ≥ 8 trades averaging worse
  than −0.15R stops being traded in that hour.
- **Direction blocks** — same test per (symbol, direction).
- **Spread caps** — if the expensive half of a symbol's entries (by spread)
  loses, the accepted spread is capped at that symbol's observed median.
- **Loss-streak throttle** — after 3 consecutive losses, risk per trade is
  halved (then quartered) until a winner resets it.

Blocked patterns are re-allowed automatically once their losing trades age
out of the lookback window. Current rules (with the stats justifying each)
live in `journal/learned_rules.json`, and

```powershell
python analyze_trades.py
```

prints a readable report: performance by symbol / direction / hour / exit
reason plus every active rule. Thresholds are tunable under `learning:` in
`config.yaml`; delete the `journal/` folder to reset the bot's memory.

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
analyze_trades.py       journal report + active learned rules
config.yaml             all tunables (risk, strategy, sessions, learning, symbols)
OPERATING_GUIDE.md      step-by-step instructions for non-coders
scalper/
  config.py             typed config loading
  indicators.py         EMA / RSI / ATR (pandas, no MT5 dependency)
  strategy.py           M1 entry + M5 trend filter -> Signal
  risk.py               lot sizing + daily loss / trade-count guards
  trade_manager.py      break-even, ATR trailing, time stop
  journal.py            per-trade diary (entry context + outcome, CSV)
  learning.py           mines the journal for loss patterns -> rules
  news.py               economic-calendar fetch/cache + entry/flatten gates
  mt5_client.py         all MetaTrader5 API calls (orders, data, history)
  bot.py                main polling loop
journal/                created at runtime: trades.csv, learned_rules.json
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

## Choosing instruments (FX, gold, silver, indices)

The bot never picks instruments itself — it trades exactly the `symbols:`
list in `config.yaml`, using each symbol's contract spec (point size, tick
value, lot steps) straight from the broker, so position sizing stays correct
across asset classes.

Metals and indices have very different point scales from 5-digit FX, so the
point-denominated settings (`min_sl_points`, `min_atr_points`,
`max_spread_points`) must be overridden per symbol under `symbol_overrides:`.
`config.yaml` ships with starting values for `XAUUSD`, `XAGUSD`, and `US30` —
verify the symbol name and typical spread on *your* broker's demo (gold may be
listed as `GOLD` or `XAUUSD.x`) and adjust. The backtester has matching
contract presets: e.g.
`python backtest.py --csv gold_m1.csv --symbol XAUUSD --spread-points 30`
(override with `--point` / `--tick-value` if your broker's contract differs).
