# Operating Guide (no coding required)

This guide walks you through running the scalping bot on a practice (demo)
MetaTrader 5 account, step by step. You do not need to know how to program —
if you can install an app and edit a text file, you can operate this bot.

> ⚠️ **Read this first.** This bot only works on **demo accounts** — it
> checks at startup and refuses to run on a real-money account. Demo results
> do **not** predict real results. Treat this as a learning tool.

---

## Part 1 — One-time setup (about 30 minutes)

### Step 1: Install MetaTrader 5

1. Go to <https://www.metatrader5.com/en/download> and download **MetaTrader 5
   for Windows**.
2. Run the installer and accept the defaults.
3. Open MetaTrader 5. It may open a demo account for you automatically — if
   so, note the login number shown in the top-left corner and skip to Step 3.

*You need a Windows PC. The bot's connection software is made by MetaQuotes
and only runs on Windows.*

### Step 2: Create a demo account

1. In MetaTrader 5, click **File → Open an Account**.
2. Pick a broker (the default "MetaQuotes" is fine to start) and click **Next**.
3. Choose **"Open a demo account to trade virtual money without risk"**.
4. Fill in your details. For deposit, **choose 5,000 USD** if the broker
   offers it (the bot behaves like a $5,000 account either way), leverage
   1:100 is fine.
5. When the account is created, **write down three things** — you will need
   them in Step 6:
   - **Login** (a number, e.g. `5037291845`)
   - **Password**
   - **Server** (e.g. `MetaQuotes-Demo`)

### Step 3: Allow automated trading in MetaTrader 5

1. In MetaTrader 5, click **Tools → Options**.
2. Open the **Expert Advisors** tab.
3. Tick **"Allow algorithmic trading"** and click **OK**.

### Step 4: Install Python

1. Go to <https://www.python.org/downloads/> and download the latest Python
   for Windows.
2. Run the installer. **IMPORTANT:** on the first screen, tick the box that
   says **"Add python.exe to PATH"** before clicking Install. This is the
   single most common setup mistake.
3. To check it worked: press the **Windows key**, type `powershell`, press
   Enter, then in the blue window type:
   ```
   python --version
   ```
   You should see something like `Python 3.12.4`. If you see an error,
   re-run the installer and make sure the PATH box is ticked.

### Step 5: Download the bot and install its requirements

1. Download this project to your PC (on the GitHub page: green **Code**
   button → **Download ZIP**), then right-click the ZIP → **Extract All**.
   Extract it somewhere easy, e.g. `C:\TradingBot`.
2. Open PowerShell (Windows key → type `powershell` → Enter) and go to the
   bot's folder:
   ```
   cd C:\TradingBot
   ```
   (adjust the path if you extracted it elsewhere — the folder that contains
   `run_bot.py` is the one you want)
3. Install the bot's requirements:
   ```
   pip install -r requirements.txt
   ```
   This takes a minute or two. Warnings in yellow are fine; red errors are not.

### Step 6: Tell the bot about your demo account

1. In the bot's folder, right-click **`config.yaml`** → **Open with** →
   **Notepad**.
2. Near the top, find the `account:` section and fill in the three things you
   wrote down in Step 2. Keep the quotes:
   ```yaml
   account:
     login: 5037291845
     password: "your-demo-password"
     server: "MetaQuotes-Demo"
   ```
3. Save the file (**Ctrl+S**) and close Notepad.

**Shortcut:** if you leave `login: 0`, the bot simply attaches to whatever
account the MetaTrader 5 terminal is currently logged into — just make sure
that's your demo account.

---

## Part 2 — Running the bot

### Starting it

1. Make sure MetaTrader 5 is installed (it doesn't have to be open — the bot
   will start it).
2. Open PowerShell, go to the bot folder, and start it:
   ```
   cd C:\TradingBot
   python run_bot.py
   ```
3. You should see a line like:
   ```
   Connected to DEMO account 5037291845 (MetaQuotes-Demo), balance=5000.00 USD
   Bot running: symbols=['EURUSD', 'GBPUSD'] corpus=5000 risk/trade=0.50% daily-stop=2.00%
   ```
4. Leave the PowerShell window open. The bot is now watching the market.
   **The PC must stay on** — the bot stops when the window closes or the PC
   sleeps (tip: Settings → System → Power → set "Sleep" to Never while it runs).

### What you'll see while it runs

| Log line | Meaning |
|---|---|
| `Signal: BUY EURUSD (...)` | The strategy spotted a possible trade |
| `OPENED BUY EURUSD 0.08 lots @ ...` | A trade was placed, with its stop-loss and target |
| `Skip EURUSD: spread 25 > 20 points` | A trade was skipped because conditions were bad |
| `Skip EURUSD: learned rule: ...` | Skipped because this setup **lost money before** |
| `Moving SL on EURUSD #123...` | Protecting profit on an open trade |
| `Journal: closed #123 ... pnl=-24.80 (-0.99R, stop_loss)` | A trade finished and was recorded |
| `Daily loss limit hit ... no more entries today` | Safety brake: ~$100 lost today, trading paused until tomorrow |

Long silences are **normal**. The bot only trades during London/New York
hours (see `session:` in the config) and only when its conditions line up.
Zero trades on a quiet day is expected behaviour, not a fault. The market is
closed on weekends.

### Watching it in MetaTrader 5

Open MetaTrader 5 while the bot runs:

- The **Trade** tab (bottom panel, View → Toolbox if hidden) shows open
  trades, each with its stop-loss (SL) and take-profit (TP) already attached.
- The **History** tab shows finished trades.
- You can close any trade manually at any time (right-click → Close) — the
  bot will notice, record the result, and learn from it like any other trade.

### Stopping the bot

Click on the PowerShell window and press **Ctrl+C**.

Any open trades **keep their stop-loss and take-profit on the broker's
server**, so they stay protected even with the bot (or your PC) off. They
will simply close at the stop or the target on their own. If you want to be
completely flat, close them in MetaTrader 5 before walking away.

To start again later: `cd C:\TradingBot` then `python run_bot.py`. The bot
remembers everything it learned (that's stored in the `journal` folder).

---

## Part 3 — How the bot learns from its losses

Every trade is written to a diary: **`journal\trades.csv`** (you can open it
in Excel). For each trade it records the conditions at entry — time of day,
direction, spread, volatility — and how it ended.

After every closed trade the bot re-examines the last 30 days and adjusts:

1. **Bad hours are switched off.** If a symbol has lost money consistently
   in a given hour (at least 8 trades averaging clearly negative), the bot
   stops trading that symbol in that hour.
2. **Bad directions are switched off.** If, say, GBPUSD sell-trades keep
   losing, only buy-trades remain allowed on GBPUSD.
3. **Expensive conditions are avoided.** If trades entered when the spread
   was wide lost money, the bot tightens its own spread limit for that symbol.
4. **Losing streaks cut the trade size.** After 3 losses in a row, the next
   trades risk half the normal amount (then a quarter). One winner resets it.

Blocks are not forever: rules are recomputed over a rolling 30-day window,
so once the losing trades age out, the pattern gets another chance.

### See what it has learned

In PowerShell, in the bot folder:

```
python analyze_trades.py
```

This prints a plain-English report: win rate, profit by hour / symbol /
direction, and every rule currently active, each with the numbers that
justify it. The same information is stored in `journal\learned_rules.json`.

### Reset its memory

Want a clean slate? Stop the bot and delete the `journal` folder. It will
start learning again from zero.

---

## Part 4 — Changing settings

All settings live in `config.yaml` (right-click → Open with → Notepad).
**Stop the bot, edit, save, start it again** — it reads the file only at
startup. The ones you're most likely to touch:

| Setting | What it does | Default |
|---|---|---|
| `risk_per_trade_pct` | % of the $5k risked per trade | `0.5` (= ~$25) |
| `max_daily_loss_pct` | Daily stop: halt after losing this % | `2.0` (= ~$100) |
| `symbols` | Which markets to trade | EURUSD, GBPUSD |
| `trade_hours_utc` | When it's allowed to trade (UTC!) | London + NY |
| `corpus` | The capital it sizes against | `5000.0` |
| `learning.enabled` | Turn the learning feature on/off | `true` |

Keep the file's exact spacing/indentation — YAML files are picky about it.

### Trading gold, silver, or an index (like US30)

The bot only trades what you list under `symbols:` — it never chooses on its
own. To add gold:

1. In MetaTrader 5, find the exact name in the **Market Watch** panel
   (Ctrl+M). Brokers name gold differently: `XAUUSD`, `GOLD`, `XAUUSD.x`...
   If you can't see it, right-click the panel → **Show All**.
2. Add that exact name to the `symbols:` list in `config.yaml`:
   ```yaml
   symbols:
     - EURUSD
     - XAUUSD
   ```
3. Gold, silver and indices move in much bigger numbers than currency pairs,
   so they need their own limits. `config.yaml` already contains a
   `symbol_overrides:` section with starting values for `XAUUSD`, `XAGUSD`
   and `US30` — if your broker uses a different name, rename that entry to
   match. Without an override the bot would apply forex-sized limits, skip
   most trades for "spread too wide", and use stops that are far too tight.
4. Restart the bot. Money risked per trade stays the same (~$25): the bot
   reads each instrument's contract details from the broker and sizes the
   position accordingly.

---

## Part 5 — Troubleshooting

| Problem | Fix |
|---|---|
| `python` is not recognized | Python isn't on PATH. Re-run the Python installer, tick **Add python.exe to PATH**. |
| `The MetaTrader5 package is not available` | You're not on Windows, or Step 5's `pip install` failed. Re-run it and read the red text. |
| `MT5 initialize failed` | MetaTrader 5 isn't installed, or login/password/server in `config.yaml` are wrong (check for typos; the server name must match exactly what MT5 shows). |
| `Account ... is not a DEMO account. Refusing to trade.` | Working as designed — log the terminal into your **demo** account. |
| Bot runs but never trades | Usually: outside session hours (they're in **UTC**, not your local time), weekend (market closed), spread too wide on your broker's demo feed, or simply no signal yet. Run with `python run_bot.py --verbose` to see more detail. |
| `Order failed: retcode=10027` | Algorithmic trading is disabled — redo Part 1, Step 3, and also check the "Algo Trading" button in MT5's toolbar is green/enabled. |
| Trades much smaller than expected | Normal after losses: the daily loss brake and the loss-streak throttle both shrink or pause trading. `python analyze_trades.py` shows why. |
| I closed a trade by hand — is that OK? | Yes. The bot records it as "manual" and learns from it like any other trade. |

## Quick daily routine

1. Morning: check the PowerShell window is still running (or start it).
2. Glance at MetaTrader 5's History tab to see yesterday's trades.
3. Once a week: run `python analyze_trades.py` and skim the report.
4. That's it. Let the sample size build for a few weeks before judging
   whether the settings need changing.
