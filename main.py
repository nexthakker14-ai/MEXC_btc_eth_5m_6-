# -*- coding: utf-8 -*-
"""
Telegram-only crypto signal bot.
No exchange orders are ever submitted.
"""
import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
INTERVAL = "5m"  # Fixed timeframe: 5 minutes only; do not change to 1m.
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "4"))
DB_PATH = os.getenv("DB_PATH", "signals.sqlite3")
PRIMARY = os.getenv("PRIMARY_EXCHANGE", "mexc").lower()
MIN_SERIES = int(os.getenv("MIN_SERIES_LENGTH", "2"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("signal-bot")

GREEN = "G"
RED = "R"
ANY = "*"

# Each rule describes the CLOSED candles before the signal candle.
# The current, still-forming candle must have signal_color for a WARNING.
# The same candle is confirmed only after its close.
#
# Series rules require a preceding run of at least MIN_SERIES_LENGTH candles.
RULES = [
    {
        "id": "red_run_g-r-g_to_g7",
        "name": "Червона серія → зелена 1, червона 2, зелена 3 … зелена 7",
        "side": "LONG", "signal_color": GREEN,
        "prefix": [GREEN, RED, GREEN, ANY, ANY, ANY],
        "series_before": RED,
        "reported_horizon": 5,
        "reported_win_rate": "98,64%",
        "reported_sample": 297,
        "alternate_reported_horizon": 6,
        "alternate_reported_win_rate": "99,66%",
    },
    {
        "id": "green_run_r-any-r-g-r",
        "name": "Зелена серія → червона 1, будь-яка 2, червона 3, зелена 4, червона 5",
        "side": "LONG", "signal_color": RED,
        "prefix": [RED, ANY, RED, GREEN],
        "series_before": GREEN,
        "reported_horizon": 6,
        "reported_win_rate": "98,10%",
        "reported_sample": 315,
    },
    {
        "id": "red_run_g-any-any-r-g-r-g",
        "name": "Червона серія → зелена 1, будь-які 2–3, червона 4, зелена 5, червона 6, зелена 7",
        "side": "SHORT", "signal_color": GREEN,
        "prefix": [GREEN, ANY, ANY, RED, GREEN, RED],
        "series_before": RED,
        "reported_horizon": 6,
        "reported_win_rate": "98,74%",
        "reported_sample": 159,
    },

    # Four additional short setups supplied in the task.
    {"id": "short_r-g-g-r", "name": "🔴🟢🟢🔴", "side": "SHORT",
     "signal_color": RED, "prefix": [RED, GREEN, GREEN],
     "reported_win_rate": "99,29%"},
    {"id": "short_g-g-r-g", "name": "🟢🟢🔴🟢", "side": "SHORT",
     "signal_color": GREEN, "prefix": [GREEN, GREEN, RED],
     "reported_win_rate": "99,26%"},
    {"id": "short_g-g-r", "name": "🟢🟢🔴", "side": "SHORT",
     "signal_color": RED, "prefix": [GREEN, GREEN],
     "reported_win_rate": "99,17%"},
    {"id": "short_g-r-g", "name": "🟢🔴🟢", "side": "SHORT",
     "signal_color": GREEN, "prefix": [GREEN, RED],
     "reported_win_rate": "99,17%"},
    {"id": "short_g-r-g-g", "name": "🟢🔴🟢🟢", "side": "SHORT",
     "signal_color": GREEN, "prefix": [GREEN, RED, GREEN],
     "reported_win_rate": "99,15%"},

    # Special numbered rules. The signal candle is the final numbered candle.
    {"id": "rule1_green_run_11", "name": "Правило №1 — попередня серія зелена",
     "side": "LONG", "signal_color": RED,
     "prefix": [RED, ANY, ANY, ANY, ANY, GREEN, RED, ANY, ANY, RED],
     "series_before": GREEN, "reported_win_rate": "99,68%",
     "reported_horizon": 6, "outcome_mode": "at_least_one_green"},
    {"id": "rule2_previous_green_7", "name": "Правило №2 — попередня свічка зелена",
     "side": "LONG", "signal_color": RED,
     "prefix": [RED, GREEN, ANY, ANY, ANY, RED, RED],
     "previous_color": GREEN, "reported_win_rate": "99,64%",
     "reported_horizon": 6, "outcome_mode": "at_least_one_green"},
]


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    close_time: int

    @property
    def color(self):
        if self.close > self.open:
            return GREEN
        if self.close < self.open:
            return RED
        return "D"  # Doji is not treated as green or red.


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            rule_id TEXT NOT NULL,
            candle_open_time INTEGER NOT NULL,
            side TEXT NOT NULL,
            status TEXT NOT NULL,
            signal_color TEXT NOT NULL,
            entry_close REAL,
            outcome TEXT,
            checked_count INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL,
            UNIQUE(symbol, rule_id, candle_open_time)
        )
        """)
        db.execute("""
        CREATE TABLE IF NOT EXISTS warnings (
            symbol TEXT NOT NULL,
            rule_id TEXT NOT NULL,
            candle_open_time INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY(symbol, rule_id, candle_open_time)
        )
        """)


async def telegram(session, message):
    if not BOT_TOKEN or not CHAT_ID:
        log.warning("Telegram credentials are missing; message was not sent.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }
    async with session.post(url, json=payload, timeout=15) as response:
        body = await response.text()
        if response.status != 200:
            log.error("Telegram API error %s: %s", response.status, body)
            return
        log.info("Telegram message sent.")


def format_time(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%d.%m.%Y %H:%M:%S UTC")


async def fetch_klines(session, symbol, exchange):
    if exchange == "mexc":
        # MEXC USDT-M perpetual futures. Contract symbols use underscores.
        contract_symbol = symbol.replace("USDT", "_USDT")
        url = f"https://api.mexc.com/api/v1/contract/kline/{contract_symbol}"
        params = {"interval": "Min5"}  # MEXC futures 5-minute candles
        async with session.get(url, params=params, timeout=15) as response:
            response.raise_for_status()
            payload = await response.json()

        if not payload.get("success") or not isinstance(payload.get("data"), dict):
            raise RuntimeError(f"Unexpected MEXC futures response: {payload}")

        data = payload["data"]
        candles = []
        for i, timestamp_sec in enumerate(data.get("time", [])):
            open_time_ms = int(timestamp_sec) * 1000
            candles.append(Candle(
                open_time=open_time_ms,
                open=float(data["open"][i]),
                high=float(data["high"][i]),
                low=float(data["low"][i]),
                close=float(data["close"][i]),
                close_time=open_time_ms + 5 * 60 * 1000 - 1,
            ))
        return sorted(candles, key=lambda c: c.open_time)

    # Binance USDⓈ-M perpetual futures (fapi), not Spot.
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {"symbol": symbol, "interval": "5m", "limit": 100}  # Binance futures 5-minute candles
    async with session.get(url, params=params, timeout=15) as response:
        response.raise_for_status()
        raw = await response.json()

    candles = []
    for row in raw:
        candles.append(Candle(
            open_time=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            close_time=int(row[6]),
        ))
    return sorted(candles, key=lambda c: c.open_time)


async def get_klines(session, symbol):
    order = [PRIMARY, "binance" if PRIMARY == "mexc" else "mexc"]
    last_error = None
    for exchange in order:
        try:
            return await fetch_klines(session, symbol, exchange), exchange
        except Exception as exc:
            last_error = exc
            log.warning("%s data fetch failed for %s: %s", exchange, symbol, exc)
    raise RuntimeError(f"Both exchanges failed for {symbol}: {last_error}")


def series_matches(closed, required_color):
    count = 0
    for candle in reversed(closed):
        if candle.color != required_color:
            break
        count += 1
    return count >= MIN_SERIES


def rule_matches(rule, closed):
    prefix = rule.get("prefix", [])
    if len(closed) < len(prefix):
        return False
    sample = closed[-len(prefix):] if prefix else []
    for expected, candle in zip(prefix, sample):
        if expected != ANY and candle.color != expected:
            return False
    if "series_before" in rule:
        earlier = closed[:-len(prefix)] if prefix else closed
        if not series_matches(earlier, rule["series_before"]):
            return False
    if "previous_color" in rule:
        # The candle immediately before the numbered pattern must match.
        if len(closed) < len(prefix) + 1 or closed[-len(prefix)-1].color != rule["previous_color"]:
            return False
    return True


def candle_is_closed(candle, now_ms):
    return candle.close_time < now_ms


async def add_warning(session, symbol, rule, current):
    with sqlite3.connect(DB_PATH) as db:
        try:
            db.execute(
                "INSERT INTO warnings VALUES (?, ?, ?, ?)",
                (symbol, rule["id"], current.open_time, int(time.time()))
            )
        except sqlite3.IntegrityError:
            return
    side_ua = "LONG (зростання)" if rule["side"] == "LONG" else "SHORT (падіння)"
    color_ua = "зеленою 🟢" if current.color == GREEN else "червоною 🔴"
    # Telegram warning notification disabled; preserve candidate tracking.\n


async def confirm_signal(session, symbol, rule, candle):
    with sqlite3.connect(DB_PATH) as db:
        try:
            db.execute("""
                INSERT INTO signals
                (symbol, rule_id, candle_open_time, side, status, signal_color,
                 entry_close, created_at)
                VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?)
            """, (symbol, rule["id"], candle.open_time, rule["side"],
                  candle.color, candle.close, int(time.time())))
        except sqlite3.IntegrityError:
            return

    side_ua = "LONG 🟢" if rule["side"] == "LONG" else "SHORT 🔴"
    await telegram(session,
        f"✅ ПІДТВЕРДЖЕНИЙ СИГНАЛ\n"
        f"Монета: {symbol}\nТаймфрейм: 5 хвилин\n"
        f"Патерн: {rule['name']}\n"
        f"Напрямок: {side_ua}\n"
        f"Сигнальна свічка закрилася потрібним кольором.\n"
        f"Ціна закриття: {candle.close}\n"
        f"Час закриття: {format_time(candle.close_time)}\n\n"
        f"⏳ Починається перевірка наступних 6 закритих свічок."
    )


async def check_pending(session, symbol, closed):
    with sqlite3.connect(DB_PATH) as db:
        rows = db.execute("""
            SELECT id, side, candle_open_time, checked_count, rule_id
            FROM signals WHERE symbol=? AND status='PENDING'
        """, (symbol,)).fetchall()

    for signal_id, side, signal_open, checked, rule_id in rows:
        future = [c for c in closed if c.open_time > signal_open]
        future = future[:6]
        if len(future) <= checked:
            continue

        new_candles = future[checked:]
        updated = checked
        won = False
        for candle in new_candles:
            updated += 1
            # The requested outcome rule: at least one candle in the next six
            # closes in the forecast direction. Rule 1/2 explicitly use this.
            if side == "LONG" and candle.color == GREEN:
                won = True
            elif side == "SHORT" and candle.color == RED:
                won = True

        if won:
            outcome = "WIN"
        elif updated >= 6:
            outcome = "LOSS"
        else:
            outcome = None

        with sqlite3.connect(DB_PATH) as db:
            if outcome:
                db.execute("UPDATE signals SET status=?, outcome=?, checked_count=? WHERE id=?",
                           (outcome, outcome, updated, signal_id))
            else:
                db.execute("UPDATE signals SET checked_count=? WHERE id=?", (updated, signal_id))

        if outcome:
            emoji = "🏆" if outcome == "WIN" else "❌"
            await telegram(session,
                f"{emoji} РЕЗУЛЬТАТ: {outcome}\n"
                f"Монета: {symbol}\nНапрямок: {side}\n"
                f"Патерн: {rule_id}\n"
                f"Перевірено закритих свічок: {updated}/6\n"
                f"Сигнал: {format_time(signal_open)}"
            )


async def process_symbol(session, symbol):
    candles, exchange = await get_klines(session, symbol)
    now_ms = int(time.time() * 1000)
    closed = [c for c in candles if candle_is_closed(c, now_ms)]
    forming = [c for c in candles if not candle_is_closed(c, now_ms)]

    # Evaluate outcomes using only fully closed candles.
    await check_pending(session, symbol, closed)

    if not forming or not closed:
        return

    current = forming[-1]
    if current.color not in (GREEN, RED):
        return

    for rule in RULES:
        if current.color != rule["signal_color"]:
            continue
        if not rule_matches(rule, closed):
            continue

        # Warning once per rule and current candle.
        await add_warning(session, symbol, rule, current)

        # A signal is never confirmed while the candle is forming.
        # Once this candle closes, the next poll sees it in `closed`.
        # Match the exact same pattern using candles ending with this candle.
        # We persist candidate IDs below via warning row; confirmation happens
        # when this candle moves from forming to closed on a later poll.
        # See confirm_closed_candidates().
    log.debug("%s fetched from %s", symbol, exchange)


async def confirm_closed_candidates(session, symbol, closed):
    with sqlite3.connect(DB_PATH) as db:
        warnings = db.execute(
            "SELECT rule_id, candle_open_time FROM warnings WHERE symbol=?",
            (symbol,)
        ).fetchall()

    candle_by_open = {c.open_time: c for c in closed}
    for rule_id, open_time in warnings:
        candle = candle_by_open.get(open_time)
        if not candle:
            continue
        rule = next((r for r in RULES if r["id"] == rule_id), None)
        if not rule or candle.color != rule["signal_color"]:
            continue
        # Re-check the full pattern with the signal candle removed from prefix
        # and current signal candle verified as the last bar.
        preceding = [c for c in closed if c.open_time < open_time]
        if rule_matches(rule, preceding):
            await confirm_signal(session, symbol, rule, candle)


async def worker():
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Railway variables.")
    init_db()
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await telegram(session,
            "🤖 Бот сигналів запущено.\n"
            "BTCUSDT та ETHUSDT · 5m\n"
            f"Основне джерело: {PRIMARY.upper()}, резервне: Binance/MEXC\n"
            "Автоматичні угоди вимкнені."
        )
        while True:
            for symbol in SYMBOLS:
                try:
                    candles, _ = await get_klines(session, symbol)
                    now_ms = int(time.time() * 1000)
                    closed = [c for c in candles if candle_is_closed(c, now_ms)]
                    await check_pending(session, symbol, closed)
                    await confirm_closed_candidates(session, symbol, closed)
                    forming = [c for c in candles if not candle_is_closed(c, now_ms)]
                    if forming and closed:
                        current = forming[-1]
                        if current.color in (GREEN, RED):
                            for rule in RULES:
                                if current.color == rule["signal_color"] and rule_matches(rule, closed):
                                    await add_warning(session, symbol, rule, current)
                except Exception:
                    log.exception("Processing error for %s", symbol)
            await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(worker())
