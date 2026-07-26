import asyncio
import logging
import os
from contextlib import asynccontextmanager

import stripe
import uvicorn
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from bot import create_bot, get_settings, load_settings, run_polling, set_settings
from db import close_db, get_db_session, init_db_from_env
from stripe_service import InvalidStripeEventError, process_verified_event
from subscription_access_service import (
    apply_subscription_access_action,
    run_subscription_access_scheduler,
)

logger = logging.getLogger(__name__)


def is_polling_healthy(app: FastAPI) -> tuple[bool, str]:
    return task_health(getattr(app.state, "polling_task", None))


def is_scheduler_healthy(app: FastAPI) -> tuple[bool, str]:
    return task_health(getattr(app.state, "access_scheduler_task", None))


def task_health(task: asyncio.Task[None] | None) -> tuple[bool, str]:
    if task is None:
        return False, "missing"

    if task.cancelled():
        return False, "cancelled"

    if task.done():
        try:
            exception = task.exception()
        except asyncio.CancelledError:
            return False, "cancelled"

        if exception is not None:
            return False, f"failed: {type(exception).__name__}"
        return False, "stopped"

    return True, "running"


async def stop_polling_task(polling_task: asyncio.Task[None]) -> None:
    if not polling_task.done():
        polling_task.cancel()

    try:
        await polling_task
    except asyncio.CancelledError:
        logger.info("Telegram polling task cancelled.")
    except Exception:
        logger.exception("Telegram polling task stopped with an unexpected error.")


async def stop_background_task(task: asyncio.Task[None], task_name: str) -> None:
    if not task.done():
        task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        logger.info("%s task cancelled.", task_name)
    except Exception:
        logger.exception("%s task stopped with an unexpected error.", task_name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        app_settings = load_settings()
        init_db_from_env()
    except RuntimeError as error:
        logger.error("%s", error)
        raise

    set_settings(app_settings)

    bot = create_bot(app_settings)
    polling_task = asyncio.create_task(run_polling(bot), name="telegram-polling")
    access_scheduler_task = asyncio.create_task(
        run_subscription_access_scheduler(bot, app_settings),
        name="subscription-access-scheduler",
    )
    app.state.bot = bot
    app.state.polling_task = polling_task
    app.state.access_scheduler_task = access_scheduler_task

    try:
        yield
    finally:
        await stop_polling_task(polling_task)
        await stop_background_task(access_scheduler_task, "Subscription access scheduler")
        await bot.session.close()
        app.state.bot = None
        app.state.polling_task = None
        app.state.access_scheduler_task = None
        close_db()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    healthy, polling_status = is_polling_healthy(request.app)
    scheduler_healthy, scheduler_status = is_scheduler_healthy(request.app)
    app_healthy = healthy and scheduler_healthy
    status_code = 200 if app_healthy else 503
    status = "ok" if app_healthy else "unhealthy"
    return JSONResponse(
        status_code=status_code,
        content={
            "status": status,
            "polling": polling_status,
            "scheduler": scheduler_status,
        },
    )


@app.post("/stripe/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    db: Session = Depends(get_db_session),
) -> JSONResponse:
    if not stripe_signature:
        logger.warning("Stripe webhook rejected: missing Stripe-Signature header.")
        return JSONResponse(
            status_code=400,
            content={"detail": "Missing Stripe-Signature header."},
        )

    payload = await request.body()
    webhook_secret = get_settings().stripe_webhook_secret

    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, webhook_secret
        )
    except ValueError:
        logger.warning("Stripe webhook rejected: invalid payload.")
        return JSONResponse(status_code=400, content={"detail": "Invalid payload."})
    except stripe.error.SignatureVerificationError:
        logger.warning("Stripe webhook rejected: invalid signature.")
        return JSONResponse(status_code=400, content={"detail": "Invalid signature."})

    event_id = event.get("id")
    event_type = event.get("type")

    try:
        result = process_verified_event(db, event, get_settings())
    except InvalidStripeEventError:
        db.rollback()
        logger.warning("Stripe webhook rejected: verified event is missing id or type.")
        return JSONResponse(status_code=400, content={"detail": "Invalid event."})
    except SQLAlchemyError:
        db.rollback()
        logger.error("Stripe webhook failed while processing event id=%s.", event_id)
        return JSONResponse(
            status_code=500,
            content={"detail": "Could not process Stripe event."},
        )

    logger.info(
        "Verified Stripe event id=%s type=%s processed=%s reason=%s",
        event_id,
        event_type,
        result.processed,
        result.reason,
    )

    if result.processed and result.subscription_id and result.access_action:
        try:
            await apply_subscription_access_action(
                db=db,
                bot=getattr(request.app.state, "bot", None),
                settings=get_settings(),
                subscription_id=result.subscription_id,
                action=result.access_action,
            )
        except Exception:
            logger.exception(
                "Telegram access sync failed for subscription_id=%s.",
                result.subscription_id,
            )

    return JSONResponse(
        status_code=200,
        content={
            "received": True,
            "processed": result.processed,
            "reason": result.reason,
        },
    )


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1)


if __name__ == "__main__":
    main()
