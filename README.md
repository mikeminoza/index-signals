# IndexPullback signal bot and trade calendar

Every weekday morning this bot checks six stock indices and sends you a Telegram message:
what to buy, what to sell, or "No action today". You place the trades yourself on the
MT5 phone app. The bot keeps a journal of every signal, fill, stop and result, and
publishes it as a calendar page you can open on your phone.

It runs free on GitHub (GitHub Actions + GitHub Pages). Your PC can stay off.

| When (Philippine time) | What happens |
|---|---|
| About 9:05 am, Tue-Sat | Morning message with the day's signals (Saturday's covers Monday's open) |
| About 10:35 am | Retry, only if the morning prices were late (otherwise it stays quiet) |
| About 8:20 pm, daily | Reads your replies and confirms them; updates the calendar |

GitHub sometimes starts scheduled jobs a few minutes late.

## The strategy

Connors' RSI(2) pullback, as backtested on 2014-2026 data:

- **Buy** an index at the open when its last daily close is above its 200-day average
  (uptrend) and its 2-day RSI is below 5 (a sharp dip).
- **Sell** at the open after the 2-day RSI closes above 70 (the bounce has happened).
- **Stop loss** 3 x ATR(20) below the signal close, placed with the order.
- **Size** so a normal day's move (1 ATR) is 0.5% of your balance.

Backtest (July 2014 - October 2026): about +4.5% a year, 13% worst drawdown, 75% of
trades won. The RSI-above-70 exit was chosen on 2014-2020 data and then held up on
2021-2026. Demo first.

---

## Setup (about 15 minutes, once)

### 1. Create your Telegram bot

1. In Telegram, open a chat with **@BotFather** and send `/newbot`.
2. Pick a name and a username ending in `bot` (for example `mike_index_signals_bot`).
3. BotFather replies with a **token** like `123456789:AAH...`. Keep it private.
4. Open a chat with your new bot and send it `hi`.
5. In a browser, open `https://api.telegram.org/bot<TOKEN>/getUpdates` with your token in
   place of `<TOKEN>`. Find `"chat":{"id":` and copy the number after it. That's your **chat ID**.

### 2. Put the bot on GitHub

1. Create a free account at github.com if you don't have one.
2. Click **New repository**. Name it, for example `index-signals`, choose **Public**
   (free GitHub Pages needs a public repository), and click **Create repository**.
3. Click **uploading an existing file**, drag in everything inside this `signal-bot`
   folder (`bot.py`, `config.json`, `README.md`, the `docs` and `state` folders), and
   click **Commit changes**.
4. Check that `.github/workflows/daily.yml` arrived. Windows can skip folders that start
   with a dot. If it's missing: **Add file → Create new file**, type the name
   `.github/workflows/daily.yml`, paste in the contents of that file, and commit.

### 3. Add your Telegram details as secrets

In the repository: **Settings → Secrets and variables → Actions → New repository secret**.
Add two secrets:

| Name | Value |
|---|---|
| `TELEGRAM_TOKEN` | the token from BotFather |
| `TELEGRAM_CHAT_ID` | your chat ID |

Secrets stay hidden, even in a public repository.

### 4. Turn on the calendar page

**Settings → Pages → Build and deployment**: Source **Deploy from a branch**, branch
**main**, folder **/docs**, then **Save**. After a minute the page is live at
`https://<your-username>.github.io/<repository-name>/`. Bookmark it on your phone.
The site has two pages: **Calendar** (your trades) and **How it works** (live market
watch for the six indices, the rules, the bot's schedule and your routine).

### 5. Start the bot

Open the **Actions** tab, allow workflows if GitHub asks, click **Daily signals →
Run workflow**. Within a minute you get your first Telegram message. From then on it
runs by itself.

---

## Every morning

1. Read the message. Most days it says "No action today".
2. For a **BUY**: in the MT5 app, open the index, tap **Trade**, choose **Buy by market**,
   enter the volume (below) and the **stop loss** from the message, and send.
3. For a **SELL**: close that position.
4. Optional: reply with the price you actually got, for example `filled US100 19940`, so
   the calendar records your real result.

### Working out the volume (lots)

The message gives a size **per point**, for example `Size ≈ $0.71 per point`. In MT5, open
the index's **Specification** (Market Watch → hold the symbol → Specification) and find
the **contract size**: the money per point for 1 lot (often 1). Then:

> lots = size per point ÷ contract size, rounded **down** to the broker's volume step

Example: $0.71 per point, contract size 1 → 0.7 lots. If the result is below the minimum
volume, skip the trade or use a larger demo balance.

## Replies the bot understands

| Reply | Meaning |
|---|---|
| `filled US100 19940` | the price you actually got, for a buy or a sell |
| `skip US100` | you didn't take this signal (it shows as skipped on the calendar) |
| `note US100 entered late` | a note on a trade |
| `note away all week` | a general note for today |
| `balance 10250` | size new signals from this balance |
| `help` | the list of replies |

The bot reads replies at about 9:05 am and 8:20 pm and confirms what it recorded.

## Settings (`config.json`)

| Setting | Default | Meaning |
|---|---|---|
| `start_equity` | 10000 | starting balance for the calendar and for sizing |
| `risk_pct` | 0.5 | % of balance per normal daily move, per index |
| `max_gross` | 2.0 | cap on total exposure, as a multiple of balance |
| `stop_atr` | 3.0 | stop distance in ATRs |
| `exit_rsi` | 70 | sell when the 2-day RSI closes above this (`null` = sell on a close above the 5-day average instead) |
| `markets[].name` | US500 ... | the names used in messages and replies; change them to your broker's (for example `NAS100`) |

Change the rules themselves only if you understand the backtest: the numbers above are
what was tested.

## Good to know

- **Prices** come from Dukascopy's free datafeed, the same source as the backtest. Your
  broker's prices can differ slightly, so check borderline signals on your MT5 chart.
- **The calendar** assumes you bought or sold at the day's opening price plus half the
  spread unless you reply `filled`. It subtracts an estimate of overnight financing.
- **The repository is public**, so anyone with the link can see the demo trade journal.
  Your Telegram token is never shown.
- **To pause the bot**: Actions → Daily signals → **⋯ → Disable workflow**.
- **No message?** Open the Actions tab and click the latest run to see its log.
  - "TELEGRAM_CHAT_ID is not set": the log lists the chat IDs that messaged your bot.
  - "Permission denied" when saving: Settings → Actions → General → Workflow permissions →
    **Read and write permissions**.

This is a research tool for demo testing, not financial advice.
