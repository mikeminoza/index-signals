# IndexPullback signal bot and trade calendar

Every weekday morning this bot checks six stock indices and sends you a Telegram message:
what to buy, what to sell, or "No action today". You place the trades yourself on the
MT5 phone app. The bot keeps a journal of every signal, fill, stop and result, and
publishes it as a calendar page you can open on your phone.

It runs free on GitHub (GitHub Actions + GitHub Pages). Your PC can stay off.

| When (Philippine time) | What happens |
|---|---|
| About 9:05 am, Tue-Sat | Morning message with the day's signals (Saturday's covers Monday's open) |
| 9:35, 10:35 and 11:05 am | Backups in case GitHub delayed a run or prices were late. They stay quiet if the morning message already went out |
| About 8:20 pm, daily | Confirms your replies and reminds you about signals you haven't reported a price for |

If a run fails, the bot sends "⚠ today's bot run failed" with a link. If the morning runs all
missed and the evening run catches up, its message starts with a "Late update" warning.

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
The site has four pages: **Calendar** (your trades), **How it works** (live market
watch for the six indices, the rules, the bot's schedule and your routine), **Enter data**
(send fills, skips, notes and your balance without Telegram) and **Settings** (a system
check of the bot, its secrets and data, the current settings, a form that saves a new
`config.json`, a setup checklist and troubleshooting).

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

Once your broker's contract specs are in `config.json` (see Settings), the message gives the
exact volume, for example `Volume 0.7 lots (risk ≈ $50 per normal day)`, and a signal that
would be below your broker's minimum volume is marked as skipped.

Until then the message gives a size **per point**, for example `Size ≈ $0.71 per point`. In
MT5, open the index's **Specification** (Market Watch → hold the symbol → Specification) and
find the **contract size**: the money per point for 1 lot (often 1). Then:

> lots = size per point ÷ contract size, rounded **down** to the broker's volume step

Example: $0.71 per point, contract size 1 → 0.7 lots. If the result is below the minimum
volume, skip the trade or use a larger demo balance.

### Warnings in the message

- **⚠ Borderline**: a signal that only just met a rule (for example RSI 4.6 against the 5
  level). Your broker's prices can differ slightly from the bot's, so check your MT5 chart.
- **Close calls**: no signal on the bot's data, but close enough that your broker's chart
  might show one. Check it before doing anything.
- **Monthly check**: once a month, reply `balance AMOUNT` with your demo balance so the sizes
  match your account.

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

## Entering data on the website instead

The **Enter data** page has forms for the same things (fill, skip, note, balance), for when
you can't use Telegram. The **Settings** page can also save `config.json` with one click.
Both need this browser connected once:

1. On GitHub: your photo → **Settings** → **Developer settings** → **Personal access tokens**
   → **Fine-grained tokens** → **Generate new token**.
2. Name `website index-signals`, an expiry of up to 1 year, **Only select repositories** →
   `index-signals`, and Repository permissions → **Contents: Read and write**.
3. Copy the token, open the **Enter data** page, paste it and click **Connect**.

The token is kept in that browser only, never in the repository. It can change files in this
one repository and can't read your Telegram secrets. Click **Disconnect this browser** on a
shared device, and revoke the token on GitHub if you ever lose the device.

Each entry is saved as a small file in `state/inbox/`. The bot applies and removes them at its
next run and confirms them in its next Telegram message, marked "Website:".

## Settings (`config.json`)

| Setting | Default | Meaning |
|---|---|---|
| `start_equity` | 10000 | starting balance for the calendar and for sizing |
| `risk_pct` | 0.5 | % of balance per normal daily move, per index |
| `max_gross` | 2.0 | cap on total exposure, as a multiple of balance |
| `stop_atr` | 3.0 | stop distance in ATRs |
| `exit_rsi` | 70 | sell when the 2-day RSI closes above this (`null` = sell on a close above the 5-day average instead) |
| `markets[].name` | US500 ... | the names used in messages and replies; change them to your broker's (for example `NAS100`) |
| `markets[].contract_size` | `null` | from your broker's Specification: money per point for 1 lot. Set it to get exact lots in the messages |
| `markets[].min_lot` | `null` | the broker's minimum volume for that index |
| `markets[].lot_step` | `null` | the broker's volume step (for example 0.1 or 0.01) |

To change a setting, open `config.json` on GitHub, click the pencil icon, edit and commit.
The next run uses it.

Change the rules themselves only if you understand the backtest: the numbers above are
what was tested.

## Optional: start the bot at exactly 9:05 with cron-job.org

GitHub's own timer can start runs late or skip them. A free outside timer can start the bot
on time instead. The GitHub timer keeps running as a backup, and extra runs are harmless.

**1. Create a limited GitHub token**
1. On GitHub, click your photo → **Settings** → **Developer settings** → **Personal access
   tokens** → **Fine-grained tokens** → **Generate new token**.
2. Name: `cron-job index-signals`. Expiration: 1 year (you can renew it later).
3. Repository access: **Only select repositories** → `index-signals`.
4. Permissions → Repository permissions → **Actions: Read and write**. Leave everything else
   at "No access".
5. Click **Generate token** and copy it. GitHub only shows it once.

This token can only start or view this repository's workflow runs. It can't read your
secrets or change your code. You can delete it any time on the same page.

**2. Create the timer on cron-job.org**
1. Sign up free at cron-job.org. In your account settings, set the time zone to
   **Asia/Manila**.
2. **Create cronjob**:
   - Title: `IndexPullback morning`
   - URL: `https://api.github.com/repos/<your-username>/index-signals/actions/workflows/daily.yml/dispatches`
   - Schedule: custom, **Tuesday to Saturday at 09:05**
3. Under **Advanced**:
   - Request method: **POST**
   - Headers:
     - `Accept: application/vnd.github+json`
     - `Authorization: Bearer <your token>`
     - `X-GitHub-Api-Version: 2022-11-28`
   - Request body: `{"ref":"main"}`
4. Save, then click **Test run**. A good result is **HTTP 204**, and a new run appears in your
   repository's **Actions** tab within a few seconds.
5. Optional: a second job, every day at **20:20**, for your evening replies and reminders.

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
