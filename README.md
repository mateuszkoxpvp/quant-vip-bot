# quant-vip-bot

Telegram bot for presenting Stripe Payment Links for VIP access.

Stage 2 adds a FastAPI service with a health endpoint and a Stripe webhook
receiver. The bot still uses Telegram long polling. There is no PostgreSQL,
Telegram webhook, automatic group invite, or member removal yet.

## Local setup

1. Create and activate a virtual environment.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env`.
4. Fill in all required variables in `.env`.
5. Start the application:

   ```bash
   python app.py
   ```

By default the app listens on port `8000` locally. Railway provides `PORT`
automatically, and the app listens on `0.0.0.0:$PORT` there.

## Required variables

```text
BOT_TOKEN=
PAYMENT_LINK_MONTHLY=
PAYMENT_LINK_3_MONTHS=
PAYMENT_LINK_6_MONTHS=
PAYMENT_LINK_LIFETIME=
STRIPE_WEBHOOK_SECRET=
```

Use Stripe Payment Link URLs for the `PAYMENT_LINK_*` variables. Use the webhook
signing secret for `STRIPE_WEBHOOK_SECRET`; it starts with `whsec_`. Do not
commit real tokens, secrets, or private payment links to the repository.

## Endpoints

```text
GET  /health
POST /stripe/webhook
```

`/health` returns `200` only when the application is running and the Telegram
polling task exists and has not stopped. It returns `503` if the polling task is
missing, cancelled, stopped, or failed.

`/stripe/webhook` verifies the Stripe signature using the raw request body and
logs only the verified event ID and type. It does not fulfill orders or change
Telegram group membership yet.

## Railway

Railway runs one process with the `Procfile` command:

```text
web: python app.py
```

Keep exactly one Railway replica and one Uvicorn worker. Do not start a separate
process for the Telegram bot.

## Tests

Run the minimal test suite with:

```bash
python -m unittest discover
python -m compileall .
```

The tests do not connect to Telegram or Stripe.

## Important warning

Do not run the app locally with the same `BOT_TOKEN` while the Railway deployment
is running. This bot currently uses Telegram long polling, and two active
instances with the same token can cause Telegram conflict errors.
