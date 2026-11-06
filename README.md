# MEXC / Binance Telegram Signal Bot

Telegram-only alert bot for BTCUSDT and ETHUSDT on **5-minute candles only** (never 1-minute).
It does **not** place, modify, or cancel exchange orders.

## Behavior

- MEXC USDT-M perpetual futures REST klines are the default source (`BTC_USDT`, `ETH_USDT`); Binance USDⓈ-M perpetual futures klines are the fallback.
- Polls the currently forming candle every 4 seconds.
- Does not send warning messages for forming candles; candidate patterns are stored internally.
- Confirms only after that exact candle has closed in the required color.
- Tracks the next six closed candles and reports WIN/LOSS.
- SQLite stores internal candidates, prevents duplicate signals, and stores pending results.
- All Telegram text and source files are UTF-8.

## Deploy to Railway

1. Create a Telegram bot with `@BotFather` and copy its token.
2. Get your Telegram numeric chat ID (for example, message the bot and use a chat-ID helper bot).
3. Push this folder to a GitHub repository.
4. In Railway, create a project from that GitHub repository.
5. Add Variables:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `SYMBOLS=BTCUSDT,ETHUSDT`
   - `PRIMARY_EXCHANGE=mexc`
   - `POLL_SECONDS=4`
   - `MIN_SERIES_LENGTH=2`
6. Deploy. Railway runs `python -u main.py` as a worker.

Do not commit `.env` or your Telegram token to GitHub.

## Important interpretation notes

The pattern list contains shorthand/ellipsis and some ambiguous numbering. This initial implementation makes these explicit assumptions:

- “серія” means a run of at least `MIN_SERIES_LENGTH` consecutive candles of that color.
- In the first and fourth series patterns, unspecified positions between the numbered candles are treated as any color.
- The fixed SHORT patterns are matched as consecutive candles ending on the listed final candle.
- Rule №1 and №2 trigger on the final numbered red candle; after confirmation, a LONG result is WIN if at least one of the next six closed candles is green.
- For other LONG rules, WIN means at least one of the next six candles is green; for SHORT, at least one is red. If none is favorable by candle six, result is LOSS.
- Doji candles are neither green nor red.

Please validate these interpretations against the source strategy before using live alerts. The percentages supplied by the user are stored as reference notes only and are not independently verified.

## Data and timing

The bot uses public futures market-data REST endpoints only (MEXC `/api/v1/contract/kline/{symbol}` and Binance `/fapi/v1/klines`). It does not request trading permissions or submit orders. It polls every few seconds; only confirmed signals and their check results are sent. A closed-candle confirmation is based on exchange candle close timestamps. If both data sources are unavailable, the bot logs the error and retries on the next polling cycle.

## Storage

SQLite file `signals.sqlite3` stores internal candidate records and signals. For persistence across Railway redeploys, attach a Railway Volume and set `DB_PATH` to a path on that volume (for example `/data/signals.sqlite3`).

## UTF-8 check

All project text files are written as UTF-8. `main.py` also declares UTF-8 explicitly.
