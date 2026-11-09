"""IndexPullback signal bot: daily RSI(2) pullback signals for six stock indices.

Runs on GitHub Actions twice a day using only the Python standard library.
Each run:
  1. reads your Telegram replies (filled / skip / note / balance / help)
  2. tops up the daily price history from Dukascopy's public datafeed
  3. walks every newly completed daily bar through the rules, keeping a journal
     of signals, fills, stops and closed trades
  4. sends the morning message and rebuilds the calendar page data

Rules (Connors' RSI(2), as backtested 2014-2026): buy when the daily close is
above the 200-day SMA and the 2-day RSI is below 5; sell after the 2-day RSI
closes above 70 (exit_rsi; set it to null to sell on a close above the 5-day
SMA instead); a stop 3 x ATR(20) below the signal close protects every trade.
Fills are assumed at the next day's open plus half the spread, unless you
report your own price.

usage: python3 bot.py [--dry-run] [--no-fetch] [--state DIR] [--docs DIR]
                      [--replay-from YYYY-MM-DD] [--until YYYY-MM-DD] [--updates FILE]
"""
import argparse
import csv
import html
import json
import lzma
import math
import os
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
FX_PAIRS = {"EUR": ("EURUSD", False), "GBP": ("GBPUSD", False), "JPY": ("USDJPY", True)}
DIVISOR = {"EURUSD": 100000, "GBPUSD": 100000}  # everything else here is stored in thousandths
SYMBOL = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}
EXPECT_WIN, EXPECT_AVG = 75.0, 0.53  # backtest 2014-2026 with the RSI > 70 exit: win rate %, average trade %
DATAFEED = ("https://datafeed.dukascopy.com/datafeed", "http://datafeed.dukascopy.com/datafeed")

HELP = ("Replies I understand (I read them twice a day):\n"
        "filled US100 19940 - the price you actually got (buy or sell)\n"
        "skip US100 - you did not take this signal\n"
        "note US100 entered late - a note on a trade (or just: note text)\n"
        "balance 10250 - size new signals from this account balance\n"
        "help - this list")


# --- price history ---------------------------------------------------------------

def load_bars(path):
    bars = {}
    if os.path.exists(path):
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                bars[r["date"]] = [float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])]
    return bars


def save_bars(path, bars):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close"])
        for d in sorted(bars):
            w.writerow([d] + [round(v, 5) for v in bars[d]])


_https_blocked = False


def fetch(part):
    """One datafeed file (b"" when it doesn't exist). HTTPS first, plain HTTP if HTTPS is blocked."""
    global _https_blocked
    last = None
    for base in DATAFEED:
        if base.startswith("https") and _https_blocked:
            continue
        for attempt in range(6):
            try:
                req = urllib.request.Request(f"{base}/{part}", headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20 if base.startswith("https") else 60) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return b""
                last = e
            except Exception as e:  # timeouts, resets
                last = e
                if base.startswith("https"):
                    _https_blocked = True  # this network blocks it: stop trying for the rest of the run
                    break
            time.sleep(min(5 * 2 ** attempt, 40))  # the datafeed answers 503 when busy
    raise RuntimeError(f"{part}: {last}")


def hourly_days(code, months):
    """UTC-day OHLC from the hourly BID candles of the given (year, month) list."""
    div = DIVISOR.get(code, 1000)
    days = {}
    for y, m in months:
        raw = fetch(f"{code}/{y}/{m - 1:02d}/BID_candles_hour_1.bi5")
        if not raw:
            continue
        base = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp())
        # Each record: seconds from month start, open, close, low, high, volume
        for sec, o, c, l, h, v in struct.iter_unpack(">5if", lzma.decompress(raw, format=lzma.FORMAT_ALONE)):
            if v <= 0:
                continue
            t = base + sec
            d = t - t % 86400
            bar = days.get(d)
            if bar is None:
                days[d] = [o / div, h / div, l / div, c / div]
            else:
                bar[1], bar[2], bar[3] = max(bar[1], h / div), min(bar[2], l / div), c / div
    return days


def weekday_bars(days, before):
    """Fold weekend slivers into the next weekday (as the backtest did); complete days only."""
    out, carry = {}, None
    for d in sorted(days):
        o, h, l, c = days[d]
        if carry:
            o, h, l = carry[0], max(h, carry[1]), min(l, carry[2])
            carry = None
        day = datetime.fromtimestamp(d, timezone.utc).date()
        if day.weekday() >= 5:
            carry = (o, h, l)
            continue
        if day < before:
            out[day.isoformat()] = [o, h, l, c]
    return out


def minute_day(code, day):
    """One UTC day's [open, high, low, close] from its 1-minute file, or None if nothing traded."""
    div = DIVISOR.get(code, 1000)
    raw = fetch(f"{code}/{day.year}/{day.month - 1:02d}/{day.day:02d}/BID_candles_min_1.bi5")
    if not raw:
        return None
    o = h = l = c = None
    for _sec, op, cl, lo, hi, v in struct.iter_unpack(">5if", lzma.decompress(raw, format=lzma.FORMAT_ALONE)):
        if v <= 0:
            continue
        if o is None:
            o, h, l = op, hi, lo
        h, l, c = max(h, hi), min(l, lo), cl
    return None if o is None else [o / div, h / div, l / div, c / div]


def update_cache(state, codes, today):
    """Add every complete weekday after the last stored one.

    Finished months come from their hourly file. A month's hourly file only appears once the
    month is over, so recent days come from the per-day 1-minute files instead.
    """
    month_start = date(today.year, today.month, 1)
    problems = []
    for code in codes:
        path = os.path.join(state, "bars", f"{code}.csv")
        bars = load_bars(path)
        try:
            last = date.fromisoformat(max(bars)) if bars else month_start - timedelta(days=400)
            months, m = [], date(last.year, last.month, 1)
            while m < month_start:
                months.append((m.year, m.month))
                m = (m + timedelta(days=32)).replace(day=1)
            if months:
                for d, b in weekday_bars(hourly_days(code, months), month_start).items():
                    if d > last.isoformat():
                        bars[d] = b
                last = date.fromisoformat(max(bars)) if bars else last

            day = last + timedelta(days=1)
            while day < today:
                bar = minute_day(code, day) if day.weekday() < 5 else None
                if bar and day.weekday() == 0:
                    # Fold the weekend's early trading into Monday, as the backtest did
                    pieces = [p for p in (minute_day(code, day - timedelta(days=2)),
                                          minute_day(code, day - timedelta(days=1))) if p] + [bar]
                    bar = [pieces[0][0], max(p[1] for p in pieces), min(p[2] for p in pieces), bar[3]]
                if bar:
                    bars[day.isoformat()] = bar
                day += timedelta(days=1)
        except Exception as e:
            problems.append(f"price download failed for {code} ({e})")
        save_bars(path, bars)
    return problems


# --- indicators (same definitions as the backtest) -----------------------------------

def sma(x, n):
    out, s = [None] * len(x), 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rsi(x, n):
    """Wilder RSI, as MT5's iRSI."""
    out, gain, loss = [None] * len(x), 0.0, 0.0
    for i in range(1, len(x)):
        d = x[i] - x[i - 1]
        g, ls = max(d, 0.0), max(-d, 0.0)
        if i <= n:
            gain += g / n
            loss += ls / n
        else:
            gain = (gain * (n - 1) + g) / n
            loss = (loss * (n - 1) + ls) / n
        if i >= n:
            out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def atr(h, l, c, n):
    """MT5 iATR: simple average of true range."""
    tr = [h[0] - l[0]] + [max(h[i], c[i - 1]) - min(l[i], c[i - 1]) for i in range(1, len(c))]
    return sma(tr, n)


def indicators(bars, cfg):
    dates = sorted(bars)
    o, h, l, c = ([bars[d][k] for d in dates] for k in range(4))
    return dict(dates=dates, index={d: i for i, d in enumerate(dates)}, o=o, h=h, l=l, c=c,
                trend=sma(c, cfg["trend_sma"]), exit=sma(c, cfg["exit_sma"]), rsi=rsi(c, 2),
                atr=atr(h, l, c, cfg["atr_period"]))


def fx_converter(state):
    """conv(ccy, day): USD value of one unit of ccy at that day's close (last known)."""
    series = {}
    for ccy, (pair, invert) in FX_PAIRS.items():
        bars = load_bars(os.path.join(state, "bars", f"{pair}.csv"))
        days = sorted(bars)
        series[ccy] = (days, [1 / bars[d][3] if invert else bars[d][3] for d in days])

    def conv(ccy, day):
        if ccy == "USD":
            return 1.0
        days, vals = series[ccy]
        k = bisect_right(days, day) - 1
        if k < 0:
            raise RuntimeError(f"no {ccy} rate on or before {day}")
        return vals[k]
    return conv


# --- journal and rules -----------------------------------------------------------------

def new_journal():
    return dict(version=1, start_date=None, last_bar={}, balance=None, positions={}, pending={},
                trades=[], notes=[], telegram_offset=0, next_id=1)


def sizing_equity(j, cfg):
    if j["balance"]:
        return j["balance"]
    return cfg["start_equity"] + sum(t["pnl"] for t in j["trades"] if t["taken"])


def exposure(j, conv, day):
    """Your account's exposure: signals you skipped (or that were too small) don't count."""
    gross = 0.0
    for p in j["positions"].values():
        if p["taken"]:
            gross += p["units"] * p["marks"][-1][1] * conv(p["ccy"], day)
    for q in j["pending"].values():
        if q["side"] == "buy" and q.get("taken", True):
            gross += q["units"] * q["close"] * conv(q["ccy"], day)
    return gross


def close_trade(j, code, day, price, source, reason, conv_now, ev):
    p = j["positions"].pop(code)
    pnl = (price - p["entry"]) * p["units"] * conv_now - p["financing"]
    t = dict(p, exit_date=day, exit=price, exit_source=source, reason=reason, conv=conv_now,
             ret_pct=(price / p["entry"] - 1) * 100, pnl=pnl,
             days_held=(date.fromisoformat(day) - date.fromisoformat(p["entry_date"])).days)
    j["trades"].append(t)
    ev.append(("stopped" if reason == "stop" else "sold", t))


def step(j, cfg, m, ind, i, day, conv, ev):
    code, name, ccy = m["code"], m["name"], m["ccy"]
    o, l, c = ind["o"][i], ind["l"][i], ind["c"][i]
    half = m["spread_pct"] / 200
    rate = conv(ccy, day)
    pend = j["pending"].pop(code, None)
    pos = j["positions"].get(code)

    # Yesterday's signal fills at today's open, or at the price you reported
    if pend and pend["side"] == "buy" and pos is None:
        fill = pend.get("user_fill")
        pos = j["positions"][code] = dict(
            id=pend["id"], code=code, name=name, ccy=ccy, taken=pend["taken"], signal_date=pend["signal_date"],
            signal_rsi=pend["rsi"], signal_close=pend["close"], entry_date=day, entry=fill or o * (1 + half),
            assumed_entry=o * (1 + half), entry_source="you" if fill else "assumed", stop=pend["stop"],
            units=pend["units"], lots=pend.get("lots"), financing=0.0, marks=[], notes=pend.get("notes", []))
        ev.append(("bought", pos))
    elif pend and pend["side"] == "sell" and pos is not None:
        fill = pend.get("user_fill")
        pos["exit_why"] = pend.get("why", "closed above the 5-day average")
        pos["assumed_exit"] = o * (1 - half)
        close_trade(j, code, day, fill or o * (1 - half), "you" if fill else "assumed", "signal", rate, ev)
        pos = None

    # The broker-side stop works during the day
    if pos is not None and l <= pos["stop"]:
        close_trade(j, code, day, min(o, pos["stop"]), "stop", "stop", rate, ev)
        pos = None

    if pos is not None:
        if pos["marks"]:
            nights = (date.fromisoformat(day) - date.fromisoformat(pos["marks"][-1][0])).days
            pos["financing"] += pos["units"] * c * rate * cfg["financing_pct"] / 100 / 365 * nights
        pos["marks"].append([day, c])

    # Signals on today's close, for the next open
    a, trend, ex, r = ind["atr"][i], ind["trend"][i], ind["exit"][i], ind["rsi"][i]
    buy_at, exit_rsi = cfg["rsi_below"], cfg.get("exit_rsi")
    if pos is not None:
        why = None
        if exit_rsi and r is not None and r > exit_rsi:
            why = f"the 2-day RSI closed at {r:.1f}, above {exit_rsi:g}"
        elif not exit_rsi and ex is not None and c > ex:
            why = f"the close {c:,.2f} is above the 5-day average {ex:,.2f}"
        if why:
            close_call = (f"RSI {r:.1f} is only just above {exit_rsi:g}"
                          if exit_rsi and r - exit_rsi < 2 else None)
            j["pending"][code] = dict(side="sell", id=pos["id"], signal_date=day, close=c, why=why,
                                      borderline=close_call, user_fill=None)
        elif exit_rsi and r is not None and r > exit_rsi - 2:
            ev.append(("near", dict(name=name, day=day, text=f"RSI {r:.1f}, just under the {exit_rsi:g} sell level")))
    elif None not in (a, trend, r) and a > 0 and c > trend and r < buy_at:
        equity = sizing_equity(j, cfg)
        wanted = equity * cfg["risk_pct"] / 100 / (a * rate)
        room = cfg["max_gross"] * equity - exposure(j, conv, day)
        units = min(wanted, max(room, 0.0) / (c * rate))
        if units <= 0:
            ev.append(("capped", dict(name=name, signal_date=day)))
            return
        q = dict(side="buy", id=j["next_id"], signal_date=day, close=c, rsi=r, trend=trend, atr=a,
                 stop=round(c - cfg["stop_atr"] * a, 2), units=units, ccy=ccy, capped=units < wanted * 0.999,
                 taken=True, user_fill=None, notes=[], lots=None, too_small=None)
        if m.get("contract_size"):
            # Your broker's lots, rounded down so the risk never exceeds the plan
            lot_step = m.get("lot_step") or 0.01
            lots = math.floor(units / m["contract_size"] / lot_step + 1e-9) * lot_step
            if lots < (m.get("min_lot") or lot_step) - 1e-9:
                q.update(taken=False, too_small=units / m["contract_size"])
            else:
                q.update(lots=round(lots, 4), units=lots * m["contract_size"])
        q["risk_usd"] = q["units"] * a * rate
        tight = [f"RSI {r:.1f} is close to the {buy_at:g} level" if buy_at - r < 1 else None,
                 f"the close is only {(c / trend - 1) * 100:.2f}% above the 200-day average"
                 if c / trend - 1 < 0.003 else None]
        q["borderline"] = " and ".join(x for x in tight if x) or None
        j["pending"][code] = q
        j["next_id"] += 1
    elif None not in (trend, r) and c > trend * 0.997 and r < buy_at + 1.5:
        # No signal on this data, but close enough that your broker's prices might show one
        text = (f"RSI {r:.1f}, just above the {buy_at:g} buy level" if c > trend
                else f"RSI {r:.1f} but the close is just under the 200-day average")
        if r < buy_at or c > trend:
            ev.append(("near", dict(name=name, day=day, text=text)))


def process(j, cfg, data, conv, until):
    """Run every complete bar after last_bar through the rules, oldest first across markets."""
    ev = []
    days = sorted({d for m in cfg["markets"] for d in data[m["code"]]["dates"]
                   if j["last_bar"].get(m["code"], "") < d < until})
    for day in days:
        for m in cfg["markets"]:
            ind = data[m["code"]]
            i = ind["index"].get(day)
            if i is None or day <= j["last_bar"].get(m["code"], ""):
                continue
            step(j, cfg, m, ind, i, day, conv, ev)
            j["last_bar"][m["code"]] = day
    return ev, days


# --- Telegram replies ------------------------------------------------------------------

def telegram(token, method, **params):
    body = urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (list, dict)) else v
                                   for k, v in params.items()}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=body)
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.load(r)
    if not out.get("ok"):
        raise RuntimeError(f"Telegram {method}: {out}")
    return out["result"]


def parse_price(s):
    try:
        v = float(s.replace(",", ""))
    except ValueError:
        return None
    return v if v > 0 else None


def apply_command(j, cfg, text, local_day):
    words = text.split()
    if not words:
        return None
    cmd = words[0].lower().lstrip("/")
    names = {m["name"].lower(): m["code"] for m in cfg["markets"]}
    names.update({m["code"].lower(): m["code"] for m in cfg["markets"]})
    code = names.get(words[1].lower()) if len(words) > 1 else None
    name = next((m["name"] for m in cfg["markets"] if m["code"] == code), None)
    recent = (date.fromisoformat(local_day) - timedelta(days=7)).isoformat()

    if cmd in ("help", "start"):
        return HELP
    if cmd == "balance" and len(words) > 1 and parse_price(words[1]):
        j["balance"] = parse_price(words[1])
        return f"New signals will be sized from a balance of ${j['balance']:,.2f}."
    if cmd == "note" and len(words) > 1:
        note = " ".join(words[2:] if code else words[1:])
        j["notes"].append(dict(date=local_day, name=name, text=note))
        target = (j["positions"].get(code) or j["pending"].get(code)) if code else None
        if target is not None:
            target.setdefault("notes", []).append(note)
        return f"Note saved{' for ' + name if name else ''}."
    if cmd in ("filled", "fill") and code and len(words) > 2:
        price = parse_price(words[2])
        if price is None:
            return f"I couldn't read the price in \"{text}\"."
        q, p = j["pending"].get(code), j["positions"].get(code)
        t = next((t for t in reversed(j["trades"]) if t["code"] == code), None)
        market = q["close"] if q is not None else p["marks"][-1][1] if p is not None else t["exit"] if t else None
        if market and abs(price / market - 1) > 0.05:
            return (f"{name}: {price:,.2f} is more than 5% away from the market ({market:,.2f}), so I didn't "
                    "record it. Check the price and send it again.")
        if q is not None:
            q["user_fill"] = price
            return f"{name}: your {q['side']} at {price:,.2f} will be recorded at the next update."
        if p is not None and p["entry_source"] == "assumed" and p["entry_date"] >= recent:
            p.setdefault("assumed_entry", p["entry"])
            p["entry"], p["entry_source"] = price, "you"
            return f"{name}: entry updated to your price {price:,.2f}."
        if t is not None and t["exit_source"] == "assumed" and t["exit_date"] >= recent:
            t.setdefault("assumed_exit", t["exit"])
            t["exit"], t["exit_source"] = price, "you"
            t["ret_pct"] = (price / t["entry"] - 1) * 100
            t["pnl"] = (price - t["entry"]) * t["units"] * t["conv"] - t["financing"]
            return f"{name}: exit updated to your price {price:,.2f} ({t['ret_pct']:+.2f}%)."
        return f"{name}: no recent trade to attach {price:,.2f} to."
    if cmd == "skip" and code:
        q, p = j["pending"].get(code), j["positions"].get(code)
        target = q if q is not None and q["side"] == "buy" else p
        if target is None:
            t = next((t for t in reversed(j["trades"]) if t["code"] == code and t["exit_date"] >= recent), None)
            target = t
        if target is None:
            return f"{name}: no recent signal to skip."
        target["taken"] = False
        return f"{name}: marked as not taken. It stays in the strategy record but not in yours."
    return f"Not sure what \"{text[:40]}\" means. Send help for the list of replies."


def handle_replies(j, cfg, updates, chat_id, offset_hours):
    acks = []
    for u in updates:
        j["telegram_offset"] = max(j["telegram_offset"], u["update_id"] + 1)
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(chat_id):
            continue  # only you can drive the journal
        sent = datetime.fromtimestamp(msg.get("date", 0), timezone.utc) + timedelta(hours=offset_hours)
        ack = apply_command(j, cfg, (msg.get("text") or "").strip(), sent.date().isoformat())
        if ack:
            acks.append(ack)
    return acks


# --- reporting ----------------------------------------------------------------------------

def money(v):
    return f"{'+' if v >= 0 else '-'}${abs(v):,.0f}"


def stats(trades):
    n = len(trades)
    eq = peak = dd = 0.0
    for t in trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return dict(n=n, wins=wins, win_rate=wins / n * 100 if n else 0.0,
                avg_ret=sum(t["ret_pct"] for t in trades) / n if n else 0.0,
                total=sum(t["pnl"] for t in trades), max_dd=dd)


def verdict(s):
    if s["n"] < 10:
        return f"Too early to judge ({s['n']} of 10 trades)"
    if s["win_rate"] >= 65 and s["avg_ret"] >= 0.15:
        return "On track with the backtest"
    if s["avg_ret"] > 0:
        return "Profitable but below the backtest"
    return "Below the backtest: review costs and fills"


def next_weekday(day):
    d = date.fromisoformat(day) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def fill_quality(j):
    """How your reported prices compared with the assumed ones: (fills, average cost %, positive = worse)."""
    costs = []
    for t in j["trades"] + list(j["positions"].values()):
        if t.get("entry_source") == "you" and t.get("assumed_entry"):
            costs.append((t["entry"] / t["assumed_entry"] - 1) * 100)
        if t.get("exit_source") == "you" and t.get("assumed_exit"):
            costs.append((1 - t["exit"] / t["assumed_exit"]) * 100)
    return len(costs), (sum(costs) / len(costs) if costs else 0.0)


def reminders_due(j, cfg, today_local):
    """Today's signals you haven't reported a price for (or skipped)."""
    names = {m["code"]: m["name"] for m in cfg["markets"]}
    out = []
    for code, q in j["pending"].items():
        if q.get("taken", True) and not q.get("user_fill") and next_weekday(q["signal_date"]) == today_local:
            out.append(f"{names[code]}: you haven't reported your {q['side']} price yet. Reply "
                       f"filled {names[code]} PRICE, or skip {names[code]} if you didn't trade it.")
    return out


def compose(j, cfg, ev, now_local, acks, problems, page_url, processed, reminders):
    today_local = now_local.date()
    footer = ["", f"Calendar: {page_url}" if page_url else "",
              "Reply: filled NAME PRICE · skip NAME · note TEXT · help"]
    if not processed:
        # Evening or manual run with no new trading day: only what needs your attention
        lines = [f"<b>IndexPullback · {today_local:%a %d %b}, update</b>"]
        if reminders:
            lines += ["", "<b>Reminders</b>"] + [html.escape(r, quote=False) for r in reminders]
        if acks:
            lines += ["", "<b>Your replies</b>"] + [html.escape(a, quote=False) for a in acks]
        if problems:
            lines += ["", "⚠ " + "; ".join(html.escape(p, quote=False) for p in problems)]
        return "\n".join(x for x in lines + footer if x is not None)

    by_code = {m["code"]: m for m in cfg["markets"]}
    lines = [f"<b>IndexPullback · {today_local:%a %d %b %Y}</b>", ""]
    if now_local.hour >= 12:
        lines += ["⚠ Late update: the morning run didn't go through (GitHub delay). These signals were "
                  "meant for this morning's open, so entering now is later than tested.", ""]
    actions = []
    for code, q in j["pending"].items():
        m = by_code[code]
        when = next_weekday(q["signal_date"])
        at = "at the open" if when <= today_local else f"at the open on {when:%a %d %b}"
        check = f" ⚠ Borderline: {q['borderline']}. Check your MT5 chart." if q.get("borderline") else ""
        if q["side"] == "buy" and q.get("too_small"):
            actions.append(f"⚪ {m['name']} buy signal (RSI {q['rsi']:.1f}), but {q['too_small']:.2f} lots is "
                           f"below your broker's {m.get('min_lot') or m.get('lot_step')} minimum, so it's skipped.")
        elif q["side"] == "buy":
            cut = ", reduced to stay under the exposure cap" if q.get("capped") else ""
            size = (f"Volume {q['lots']:g} lots (risk ≈ ${q['risk_usd']:,.0f} per normal day{cut})" if q.get("lots")
                    else f"Size ≈ {SYMBOL[m['ccy']]}{q['units']:.2f} per point{cut}")
            actions.append(f"🟢 <b>BUY {m['name']}</b> {at}. Stop loss {q['stop']:,.2f}. {size}. "
                           f"(RSI {q['rsi']:.1f}){check}")
        elif j["positions"].get(code, {}).get("taken", True):
            actions.append(f"🔴 <b>SELL {m['name']}</b> {at} ({q.get('why', 'the sell rule triggered')}).{check}")
    for kind, t in ev:
        if kind in ("stopped", "sold", "bought") and not t["taken"]:
            continue  # a signal you didn't take: it stays in the strategy record on the calendar
        if kind == "stopped":
            actions.append(f"⛔ {t['name']} hit its stop at {t['exit']:,.2f}: {t['ret_pct']:+.2f}% ({money(t['pnl'])})")
        elif kind == "sold":
            actions.append(f"✅ {t['name']} sold at {t['exit']:,.2f}: {t['ret_pct']:+.2f}% ({money(t['pnl'])})")
        elif kind == "bought":
            how = "your price" if t["entry_source"] == "you" else "assumed open, reply filled to correct"
            actions.append(f"• {t['name']} bought at {t['entry']:,.2f} ({how})")
        elif kind == "capped":
            actions.append(f"• {t['name']} signal skipped: total exposure already at the cap")
    lines += actions or ["No action today."]

    latest = max(j["last_bar"].values(), default="")
    near = [t for kind, t in ev if kind == "near" and t["day"] == latest]
    if near:
        lines += ["", "Close calls, no signal on my data. Your broker's chart can differ slightly, so check MT5:"]
        lines += [f"• {t['name']}: {t['text']}" for t in near]

    holding = [p for c, p in j["positions"].items() if c not in j["pending"] and p["taken"]]
    if holding:
        lines.append("")
        for p in holding:
            change = round((p["marks"][-1][1] / p["entry"] - 1) * 100, 2) + 0.0  # + 0.0 turns -0.0 into 0.0
            lines.append(f"Holding {p['name']}: {change:+.2f}% since "
                         f"{date.fromisoformat(p['entry_date']):%d %b}, stop {p['stop']:,.2f}")
    skipped = [p["name"] for p in j["positions"].values() if not p["taken"]]
    if skipped:
        lines.append(f"Skipped signals still tracked for the record (not in your account): {', '.join(skipped)}")
    busy = set(j["pending"]) | set(j["positions"])
    idle = [m["name"] for m in cfg["markets"] if m["code"] not in busy]
    if idle:
        lines.append(f"No signal: {', '.join(idle)}")

    mine = [t for t in j["trades"] if t["taken"]]
    s = stats(mine)
    month = today_local.strftime("%Y-%m")
    month_pnl = sum(t["pnl"] for t in mine if t["exit_date"].startswith(month))
    lines += ["", f"Your record: {s['n']} trades · {s['wins']} wins ({s['win_rate']:.0f}%) · "
                  f"avg {s['avg_ret']:+.2f}% · total {money(s['total'])}",
              f"This month: {money(month_pnl)} · {verdict(s)}"]
    fills, cost = fill_quality(j)
    if fills:
        word = "better" if cost <= 0 else "worse"
        lines.append(f"Your fills: {abs(cost):.2f}% {word} than the assumed prices on average "
                     f"({fills} fill{'s' if fills != 1 else ''})")
    if j.get("balance_month") != month:
        lines.append(f"Monthly check: reply balance AMOUNT with your demo balance so sizes match "
                     f"(now sizing from ${sizing_equity(j, cfg):,.0f}).")
    if cfg.get("token_expires"):
        left = (date.fromisoformat(cfg["token_expires"]) - today_local).days
        if left <= 14:
            when = f"expires in {left} days" if left >= 0 else "has expired"
            lines.append(f"⚠ Your cron-job.org GitHub token {when} ({cfg['token_expires']}). Regenerate it on GitHub, "
                         "paste it into cron-job.org, then update token_expires in config.json.")
    if acks:
        lines += ["", "<b>Your replies</b>"] + [html.escape(a, quote=False) for a in acks]
    if problems:
        lines += ["", "⚠ " + "; ".join(html.escape(p, quote=False) for p in problems)]
    return "\n".join(x for x in lines + footer if x is not None)


def market_watch(j, cfg, data, until):
    """Each index's latest complete close against the rules, and what the bot is waiting for."""
    out = []
    for m in cfg["markets"]:
        ind = data[m["code"]]
        i = bisect_right(ind["dates"], until) - 1
        if i < 0 or ind["dates"][i] >= until:
            i -= 1
        if i < 0:
            continue
        c, trend, r = ind["c"][i], ind["trend"][i], ind["rsi"][i]
        q, p = j["pending"].get(m["code"]), j["positions"].get(m["code"])
        if q is not None:
            status = "buy-next" if q["side"] == "buy" else "sell-next"
            waiting = "Buy at the next open" if q["side"] == "buy" else "Sell at the next open"
        elif p is not None:
            status = "holding"
            waiting = (f"Sells when the 2-day RSI closes above {cfg['exit_rsi']:g}" if cfg.get("exit_rsi")
                       else "Sells on a close above the 5-day average")
        elif trend is not None and c > trend:
            status = "watching"
            waiting = f"Buys if the 2-day RSI closes below {cfg['rsi_below']:g}"
        else:
            status = "downtrend"
            waiting = "No buys while it's below the 200-day average"
        out.append(dict(name=m["name"], date=ind["dates"][i], close=round(c, 2),
                        trend=round(trend, 2) if trend is not None else None,
                        rsi=round(r, 1) if r is not None else None, status=status, waiting=waiting))
    return out


def build_page(j, cfg, now_local, path, watch=None, system=None):
    days = {}

    def add(day, item, pnl=0.0):
        x = days.setdefault(day, dict(pnl=0.0, items=[]))
        x["items"].append(item)
        x["pnl"] += pnl

    def opened(t):
        src = "your price" if t["entry_source"] == "you" else "assumed at the open"
        add(t["entry_date"], dict(kind="buy" if t["taken"] else "missed", name=t["name"],
                                  text=f"Bought at {t['entry']:,.2f} ({src}). Stop {t['stop']:,.2f}. "
                                       f"Signal: RSI {t['signal_rsi']:.1f} at the close of {t['signal_date']}."
                                       + ("" if t["taken"] else " You skipped this one.")))

    def holds(t, until=None):
        for d, c in t["marks"]:
            if d > t["entry_date"] and (until is None or d < until):
                change = round((c / t["entry"] - 1) * 100, 2) + 0.0  # + 0.0 turns -0.0 into 0.0
                add(d, dict(kind="hold", name=t["name"], text=f"Holding. Close {c:,.2f}, {change:+.2f}% so far."))

    for t in j["trades"]:
        opened(t)
        holds(t, t["exit_date"])
        why = ("it hit the stop loss" if t["reason"] == "stop"
               else t.get("exit_why", "the close was above the 5-day average"))
        src = "your price" if t["exit_source"] == "you" else "assumed"
        add(t["exit_date"], dict(kind="stop" if t["reason"] == "stop" else "sell", name=t["name"],
                                 ret=round(t["ret_pct"], 2), pnl=round(t["pnl"], 2) if t["taken"] else None,
                                 text=f"Sold at {t['exit']:,.2f} ({src}) because {why}. "
                                      f"Held {t['days_held']} days." + ("" if t["taken"] else " Not taken by you.")
                                      + "".join(f" Note: {n}" for n in t.get("notes", []))),
            t["pnl"] if t["taken"] else 0.0)
    for p in j["positions"].values():
        opened(p)
        holds(p)
    for code, q in j["pending"].items():
        name = next(m["name"] for m in cfg["markets"] if m["code"] == code)
        when = next_weekday(q["signal_date"]).isoformat()
        text = (f"Buy at the open. Stop {q['stop']:,.2f}. RSI {q['rsi']:.1f}." if q["side"] == "buy"
                else f"Sell at the open: {q.get('why', 'the sell rule triggered')}.")
        add(when, dict(kind="buy-next" if q["side"] == "buy" else "sell-next", name=name, text=text))
    for n in j["notes"]:
        add(n["date"], dict(kind="note", name=n.get("name"), text=n["text"]))

    mine = [t for t in j["trades"] if t["taken"]]
    curve, eq = [], cfg["start_equity"]
    for t in sorted(mine, key=lambda t: t["exit_date"]):
        eq += t["pnl"]
        curve.append([t["exit_date"], round(eq, 2)])
    open_pos = [dict(name=p["name"], entry_date=p["entry_date"], entry=p["entry"], last=p["marks"][-1][1],
                     ret=round((p["marks"][-1][1] / p["entry"] - 1) * 100, 2) + 0.0, stop=p["stop"], taken=p["taken"])
                for p in j["positions"].values()]
    trades = [dict(name=t["name"], taken=t["taken"], entry_date=t["entry_date"], exit_date=t["exit_date"],
                   entry=round(t["entry"], 2), exit=round(t["exit"], 2), ret=round(t["ret_pct"], 2),
                   pnl=round(t["pnl"], 2), reason=t["reason"], days=t["days_held"])
              for t in reversed(j["trades"])]
    s_mine, s_all = stats(mine), stats(j["trades"])
    out = dict(generated=f"{now_local:%d %b %Y %H:%M} {cfg['timezone_label']}", start_date=j["start_date"],
               start_equity=cfg["start_equity"], equity=round(cfg["start_equity"] + s_mine["total"], 2),
               mine=s_mine, all=s_all, verdict=verdict(s_mine), expect=dict(win=EXPECT_WIN, avg=EXPECT_AVG),
               days={d: dict(pnl=round(v["pnl"], 2), items=v["items"]) for d, v in days.items()},
               open=open_pos, trades=trades, curve=curve, watch=watch or [],
               fills=dict(zip(("n", "cost"), fill_quality(j))), system=system or {}, config=cfg,
               rules={k: cfg.get(k) for k in ("rsi_below", "exit_rsi", "exit_sma", "trend_sma", "stop_atr",
                                               "atr_period", "risk_pct", "max_gross", "start_equity")},
               markets=[dict(name=m["name"], ccy=m["ccy"]) for m in cfg["markets"]])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("window.TRACKER = " + json.dumps(out, ensure_ascii=False) + ";\n")


def write_trades_csv(j, path):
    cols = ["id", "name", "taken", "signal_date", "signal_rsi", "entry_date", "entry", "entry_source", "stop",
            "exit_date", "exit", "exit_source", "reason", "ret_pct", "pnl", "financing", "days_held", "notes"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for t in j["trades"]:
            row = dict(t, notes=" | ".join(t.get("notes", [])))
            w.writerow([round(row[c], 4) if isinstance(row[c], float) else row[c] for c in cols])


# --- main ------------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the message instead of sending it")
    ap.add_argument("--no-fetch", action="store_true", help="use the stored price history only")
    ap.add_argument("--state", default=os.path.join(ROOT, "state"))
    ap.add_argument("--docs", default=os.path.join(ROOT, "docs"))
    ap.add_argument("--replay-from", help="start a fresh journal at this date (testing)")
    ap.add_argument("--until", help="treat this date as today (testing)")
    ap.add_argument("--updates", help="read Telegram replies from this JSON file (testing)")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--local-time", help="HH:MM local time of the run, with --until (testing)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    jpath = os.path.join(args.state, "journal.json")
    j = new_journal()
    if os.path.exists(jpath):
        with open(jpath, encoding="utf-8") as f:
            j = json.load(f)

    now = datetime.now(timezone.utc)
    today = date.fromisoformat(args.until) if args.until else now.date()
    now_local = (datetime.combine(today, now.time(), timezone.utc) if args.until else now) \
        + timedelta(hours=cfg["utc_offset_hours"])
    if args.local_time:
        hh, mm = (int(x) for x in args.local_time.split(":"))
        now_local = datetime(today.year, today.month, today.day, hh, mm)
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    page_url = os.environ.get("PAGE_URL") or (
        f"https://{repo.split('/')[0].lower()}.github.io/{repo.split('/')[1]}/" if "/" in repo else "")

    # 1. Your replies since the last run
    updates = []
    if args.updates:
        with open(args.updates, encoding="utf-8") as f:
            updates = json.load(f)
    elif token and not args.dry_run:
        updates = telegram(token, "getUpdates", offset=j["telegram_offset"], timeout=0, allowed_updates=["message"])
    if updates and not chat:
        seen = {str(u.get("message", {}).get("chat", {}).get("id")) for u in updates}
        print(f"TELEGRAM_CHAT_ID is not set. Chats that messaged the bot: {', '.join(sorted(seen))}")
        sys.exit(1)
    acks = handle_replies(j, cfg, updates, chat, cfg["utc_offset_hours"])
    stamp = f"{now_local:%d %b %Y %H:%M}"
    if acks:
        j["last_reply"] = stamp

    # 2. Prices
    codes = [m["code"] for m in cfg["markets"]] + [p for p, _ in FX_PAIRS.values()]
    problems = [] if args.no_fetch else update_cache(args.state, codes, today)
    data = {m["code"]: indicators(load_bars(os.path.join(args.state, "bars", f"{m['code']}.csv")), cfg)
            for m in cfg["markets"]}
    conv = fx_converter(args.state)

    # 3. Rules: the first run starts the journal at the latest complete bar
    if j["start_date"] is None:
        latest = max(d for m in cfg["markets"] for d in data[m["code"]]["dates"] if d < today.isoformat())
        j["start_date"] = args.replay_from or latest
        for m in cfg["markets"]:
            before = [d for d in data[m["code"]]["dates"] if d < j["start_date"]]
            j["last_bar"][m["code"]] = before[-1] if before else ""
    ev, processed = process(j, cfg, data, conv, today.isoformat())
    expected = (today - timedelta(days=1))
    while expected.weekday() >= 5:
        expected -= timedelta(days=1)
    if processed:
        late = [m["name"] for m in cfg["markets"] if j["last_bar"].get(m["code"], "") < expected.isoformat()]
        if late:
            problems.append(f"no new price for {', '.join(late)} yet (market holiday or data delay)")

    # 4. Message, journal, calendar
    today_local = now_local.date()
    reminders = []
    if not processed and now_local.hour >= 18 and j.get("reminded_on") != today_local.isoformat():
        reminders = reminders_due(j, cfg, today_local)
        j["reminded_on"] = today_local.isoformat()
    if processed or acks or problems or reminders:
        msg = compose(j, cfg, ev, now_local, acks, problems, page_url, processed, reminders)
        if processed:
            j["balance_month"] = today_local.strftime("%Y-%m")
        if args.dry_run or not token or not chat:
            print(msg.replace("<b>", "").replace("</b>", ""))
            print("-" * 60)
        else:
            telegram(token, "sendMessage", chat_id=chat, text=msg, parse_mode="HTML", disable_web_page_preview=True)
            j["last_sent"] = stamp
            if processed:
                j["last_morning"] = today_local.isoformat()
    os.makedirs(args.state, exist_ok=True)
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(j, f, indent=1)
    write_trades_csv(j, os.path.join(args.state, "trades.csv"))

    # Health report for the Settings page: whether secrets exist, never their values
    run_id = os.environ.get("GITHUB_RUN_ID")
    system = dict(
        token_set=bool(token), chat_set=bool(chat), event=os.environ.get("GITHUB_EVENT_NAME", "local"),
        ran_epoch=int(now.timestamp()),
        run_url=f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{run_id}"
        if run_id and repo else None,
        repo=repo or None, last_sent=j.get("last_sent"), last_morning=j.get("last_morning"),
        last_reply=j.get("last_reply"), replies_seen=j["telegram_offset"] > 0, problems=problems,
        expected_bar=expected.isoformat(), bars={m["name"]: j["last_bar"].get(m["code"], "") for m in cfg["markets"]},
        sizing_equity=round(sizing_equity(j, cfg), 2), balance_set=bool(j.get("balance")),
        balance_month=j.get("balance_month"))
    build_page(j, cfg, now_local, os.path.join(args.docs, "data.js"), market_watch(j, cfg, data, today.isoformat()),
               system)
    print(f"processed {len(processed)} day(s), {len(ev)} event(s), {len(acks)} repl(ies)")


if __name__ == "__main__":
    main()
