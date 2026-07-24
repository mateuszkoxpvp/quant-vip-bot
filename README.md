# quant-vip-bot

Telegram bot for presenting Stripe Payment Links for VIP access.

Stage 4 stores verified Stripe Checkout completions in PostgreSQL. The bot still
uses Telegram long polling. There is no Telegram webhook, automatic group
invite, or member removal yet.

The FastAPI app only receives and verifies Stripe webhooks. Stripe event
idempotency and storage live in `stripe_service.py`; plan resolution and
subscription writes live in `subscription_service.py`.

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

The application opens a database connection pool on startup, but it does not
create tables automatically. Apply schema changes with Alembic before starting a
new deployment:

```bash
alembic upgrade head
```

## Required variables

```text
BOT_TOKEN=
DATABASE_URL=
PAYMENT_LINK_MONTHLY=
PAYMENT_LINK_3_MONTHS=
PAYMENT_LINK_6_MONTHS=
PAYMENT_LINK_LIFETIME=
STRIPE_WEBHOOK_SECRET=
```

Use Railway's PostgreSQL `DATABASE_URL` for the database connection. Use Stripe
Payment Link URLs for the `PAYMENT_LINK_*` variables. Use the webhook signing
secret for `STRIPE_WEBHOOK_SECRET`; it starts with `whsec_`. Do not commit real
tokens, secrets, database URLs, or private payment links to the repository.

`DATABASE_URL` values starting with `postgres://` or `postgresql://` are
normalized to SQLAlchemy's `postgresql+psycopg://` driver URL.

On Railway, the web service must reference the Postgres plugin variable, for
example:

```text
DATABASE_URL=${{Postgres.DATABASE_URL}}
```

Use the equivalent Railway-generated reference if your Postgres service has a
different name.

## Endpoints

```text
GET  /health
POST /stripe/webhook
```

`/health` returns `200` only when the application is running and the Telegram
polling task exists and has not stopped. It returns `503` if the polling task is
missing, cancelled, stopped, or failed.

`/stripe/webhook` verifies the Stripe signature using the raw request body,
stores the verified event in `stripe_events`, and processes
`checkout.session.completed` events by linking the Stripe Checkout Session to the
Telegram user from `client_reference_id`. This stage only writes database rows;
it does not grant Telegram group access yet.

The bot appends `client_reference_id=<telegram_id>` to every Payment Link URL.
Stripe sends that value back in the `checkout.session.completed` webhook, which
lets the app create or update:

```text
users
plans
subscriptions
```

For clean plan names and subscription durations, set metadata on every Stripe
Payment Link:

```text
plan_code=monthly
plan_name=Monthly VIP
duration_days=30
```

Recommended values:

```text
monthly:   plan_code=monthly,   duration_days=30
3 months:  plan_code=3_months,  duration_days=90
6 months:  plan_code=6_months,  duration_days=180
lifetime:  plan_code=lifetime
```

If `plan_code` is missing, the app tries to resolve the plan from the Payment
Link and then from a Stripe price ID already stored on a row in `plans`. Unknown
plans are left unprocessed instead of creating subscriptions with guessed data.

## Database

The first migration creates:

```text
users
plans
subscriptions
stripe_events
```

Run migrations locally or on Railway with:

```bash
alembic upgrade head
```

## Railway

Railway runs one process with the `Procfile` command:

```text
web: python app.py
```

Keep exactly one Railway replica and one Uvicorn worker. Do not start a separate
process for the Telegram bot.

Before deploying code that needs the database schema, run:

```bash
alembic upgrade head
```

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
