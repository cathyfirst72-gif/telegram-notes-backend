# Telegram Notes Backend

Railway webhook receiver and durable inbox for the `telegram-notes` skill.

## Data flow

Telegram sends webhook updates to `/telegram/webhook`. The backend verifies Telegram's secret header, inserts the complete update into SQLite on a Railway Volume, and returns HTTP 200 only after the transaction commits. `update_id` is unique, so Telegram retries cannot create duplicate records. A single ordered delivery connection avoids advancing the consumer past an update that arrived late.

The skill reads batches from `GET /api/updates` using a separate backend API key. It classifies text and locally transcribed voice messages, writes the Markdown notes idempotently, then acknowledges the batch at `POST /api/ack`. If local processing fails before acknowledgement, the same batch remains available for retry; duplicate `update_id` markers prevent duplicate Markdown entries.

Voice responses include unguessable capability URLs on this backend. The backend retrieves the file from Telegram using `TELEGRAM_BOT_TOKEN` in its server environment and streams it back. Telegram's token is never included in a response, browser URL, log, or repository file.

## Railway setup

Create the `telegram-notes-backend` service, attach a Railway Volume at `/data`, and set these Variables on that service:

- `TELEGRAM_BOT_TOKEN` — the bot token; Railway only.
- `TELEGRAM_WEBHOOK_SECRET` — random secret used to authenticate Telegram webhook requests.
- `TELEGRAM_NOTES_API_KEY` — random secret used by the local notes skill to access the inbox.
- `PUBLIC_BASE_URL` — the HTTPS Railway domain, without a trailing slash.

Deploy the service, create a Railway public domain, set `PUBLIC_BASE_URL`, then run inside the remote service:

```sh
python app.py connect-telegram
```

This command checks the bot's current webhook and refuses to overwrite another URL. It calls `setWebhook` with `drop_pending_updates=false`, a secret header, and one delivery connection.

## Local verification

```sh
python -m unittest discover -s tests -v
```

`GET /` is a simple human-readable health page. `GET /health` returns JSON. Both check that the persistent database is available.
