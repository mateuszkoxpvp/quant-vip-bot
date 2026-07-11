# quant-vip-bot

Telegram bot for presenting Stripe Payment Links for VIP access.

This first stage keeps the bot in long polling mode. It does not yet include
FastAPI, Stripe webhooks, PostgreSQL, Telegram webhooks, or automatic member
management.

## Local setup

1. Create and activate a virtual environment.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env`.
4. Fill in all required variables in `.env`.
5. Start the bot:

   ```bash
   python bot.py
   ```

## Required variables

```text
BOT_TOKEN=
PAYMENT_LINK_MONTHLY=
PAYMENT_LINK_3_MONTHS=
PAYMENT_LINK_6_MONTHS=
PAYMENT_LINK_LIFETIME=
```

Use Stripe Payment Link URLs for the `PAYMENT_LINK_*` variables. Do not commit
real tokens, secrets, or private payment links to the repository.

## Railway

Railway runs the bot with the `Procfile` command:

```text
web: python bot.py
```

Set all required variables in Railway service variables. The local `.env` file is
only for development and is ignored by Git.

## Important warning

Do not run the bot locally with the same `BOT_TOKEN` while the Railway deployment
is running. This bot currently uses Telegram long polling, and two active
instances with the same token can cause Telegram conflict errors.
