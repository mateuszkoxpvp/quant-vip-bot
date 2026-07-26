from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Literal

from aiogram import Bot
from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.orm import Session, aliased

import db as db_module
from bot import Settings
from models import Subscription, User
from telegram_access_service import (
    TelegramAccessResult,
    grant_group_access,
    revoke_group_access,
)

logger = logging.getLogger(__name__)

AccessAction = Literal["grant", "revoke"]

ACTIVE_SUBSCRIPTION_STATUSES = {"active", "trialing"}
REVOKE_SUBSCRIPTION_STATUSES = {
    "canceled",
    "cancelled",
    "incomplete_expired",
    "unpaid",
}
ACCESS_RETRY_DELAY_SECONDS = 300
ACCESS_CLAIM_TIMEOUT_SECONDS = 300
INVITE_LINK_TTL_DAYS = 7
GRANT_CLAIM_STATUS = "pending"
REVOKE_CLAIM_STATUS = "revoke_pending"
INVITE_SENT_STATUS = "invite_sent"
UNBAN_PENDING_STATUS = "unban_pending"
NON_ERROR_ACCESS_STATUSES = {
    "covered_by_active_subscription",
    "invite_sent",
    "join_request_approved",
    "member",
    "not_member",
    "revoked",
}


def normalize_subscription_status(status: str | None) -> str:
    return str(status or "").strip().lower()


def current_subscription_condition(model, now: datetime):
    return and_(
        model.status.in_(ACTIVE_SUBSCRIPTION_STATUSES),
        or_(model.ends_at.is_(None), model.ends_at > now),
    )


def revoked_subscription_condition(model, now: datetime):
    return or_(
        model.status.in_(REVOKE_SUBSCRIPTION_STATUSES),
        and_(
            model.status.in_(ACTIVE_SUBSCRIPTION_STATUSES),
            model.ends_at.is_not(None),
            model.ends_at <= now,
        ),
    )


def retry_due_condition(model, now: datetime):
    return or_(
        model.telegram_access_retry_at.is_(None),
        model.telegram_access_retry_at <= now,
    )


def valid_invite_condition(model, now: datetime):
    return and_(
        model.telegram_access_status == INVITE_SENT_STATUS,
        model.telegram_invite_sent_at.is_not(None),
        model.telegram_invite_sent_at > now - timedelta(days=INVITE_LINK_TTL_DAYS),
    )


def has_current_subscription(
    db: Session,
    user_id: int,
    now: datetime,
    exclude_subscription_id: int | None = None,
) -> bool:
    query = select(Subscription.id).where(
        Subscription.user_id == user_id,
        current_subscription_condition(Subscription, now),
    )
    if exclude_subscription_id is not None:
        query = query.where(Subscription.id != exclude_subscription_id)

    return db.scalar(query.limit(1)) is not None


def subscriptions_needing_grant(
    db: Session,
    now: datetime,
    limit: int = 100,
) -> list[tuple[int, int, int]]:
    rows = db.execute(
        select(Subscription.id, Subscription.user_id, User.telegram_id)
        .join(User, Subscription.user_id == User.id)
        .where(
            current_subscription_condition(Subscription, now),
            retry_due_condition(Subscription, now),
            ~valid_invite_condition(Subscription, now),
            or_(
                Subscription.telegram_access_granted_at.is_(None),
                Subscription.telegram_access_revoked_at.is_not(None),
            ),
        )
        .order_by(Subscription.id)
        .limit(limit)
    )
    return [(row[0], row[1], row[2]) for row in rows]


def subscriptions_needing_revoke(
    db: Session,
    now: datetime,
    limit: int = 100,
) -> list[tuple[int, int, int]]:
    current_subscription = aliased(Subscription)
    current_access_exists = exists(
        select(current_subscription.id).where(
            current_subscription.user_id == Subscription.user_id,
            current_subscription.id != Subscription.id,
            current_subscription_condition(current_subscription, now),
        )
    )
    rows = db.execute(
        select(Subscription.id, Subscription.user_id, User.telegram_id)
        .join(User, Subscription.user_id == User.id)
        .where(
            Subscription.telegram_access_revoked_at.is_(None),
            retry_due_condition(Subscription, now),
            or_(
                revoked_subscription_condition(Subscription, now),
                Subscription.telegram_access_status == UNBAN_PENDING_STATUS,
            ),
            ~current_access_exists,
        )
        .order_by(Subscription.id)
        .limit(limit)
    )
    return [(row[0], row[1], row[2]) for row in rows]


def claim_subscription_access_action(
    db: Session,
    subscription_id: int,
    action: AccessAction,
    now: datetime,
) -> bool:
    retry_at = now + timedelta(seconds=ACCESS_CLAIM_TIMEOUT_SECONDS)
    if action == "grant":
        action_condition = and_(
            current_subscription_condition(Subscription, now),
            ~valid_invite_condition(Subscription, now),
            or_(
                Subscription.telegram_access_granted_at.is_(None),
                Subscription.telegram_access_revoked_at.is_not(None),
            ),
        )
        claim_status = GRANT_CLAIM_STATUS
    else:
        action_condition = or_(
            revoked_subscription_condition(Subscription, now),
            Subscription.telegram_access_status == UNBAN_PENDING_STATUS,
        )
        claim_status = REVOKE_CLAIM_STATUS

    result = db.execute(
        update(Subscription)
        .where(
            Subscription.id == subscription_id,
            retry_due_condition(Subscription, now),
            action_condition,
        )
        .values(
            telegram_access_status=claim_status,
            telegram_access_checked_at=now,
            telegram_access_retry_at=retry_at,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def record_telegram_access_result(
    db: Session,
    subscription_id: int,
    result: TelegramAccessResult,
    now: datetime,
) -> None:
    subscription = db.get(Subscription, subscription_id)
    if subscription is None:
        return

    subscription.telegram_access_status = result.status
    subscription.telegram_access_checked_at = now
    subscription.telegram_access_error = (
        None
        if (
            result.granted
            or result.revoked
            or result.invite_sent
            or result.status in NON_ERROR_ACCESS_STATUSES
        )
        else result.status
    )
    if result.retryable:
        subscription.telegram_access_retry_at = now + timedelta(
            seconds=ACCESS_RETRY_DELAY_SECONDS
        )
    elif result.invite_sent:
        subscription.telegram_access_retry_at = now + timedelta(days=INVITE_LINK_TTL_DAYS)
    else:
        subscription.telegram_access_retry_at = None

    if result.invite_sent:
        subscription.telegram_invite_sent_at = now

    if result.granted:
        subscription.telegram_access_granted_at = now
        subscription.telegram_access_revoked_at = None
        subscription.telegram_invite_sent_at = None

    if result.revoked:
        subscription.telegram_access_revoked_at = now

    if result.revoked and normalize_subscription_status(subscription.status) == "active":
        subscription.status = "expired"

    db.flush()


async def apply_subscription_access_action(
    db: Session,
    bot: Bot | None,
    settings: Settings,
    subscription_id: int,
    action: AccessAction,
) -> TelegramAccessResult | None:
    if bot is None:
        logger.warning(
            "Telegram access action skipped for subscription_id=%s: bot unavailable.",
            subscription_id,
        )
        return None

    now = datetime.now(UTC)
    subscription = db.get(Subscription, subscription_id)
    if subscription is None:
        logger.warning(
            "Telegram access action skipped for missing subscription_id=%s.",
            subscription_id,
        )
        db.rollback()
        return None

    telegram_id = subscription.user.telegram_id
    user_id = subscription.user_id

    if not claim_subscription_access_action(db, subscription_id, action, now):
        logger.info(
            "Telegram access action=%s subscription_id=%s was already claimed or skipped.",
            action,
            subscription_id,
        )
        return None

    if action == "revoke" and has_current_subscription(
        db,
        user_id=user_id,
        now=now,
        exclude_subscription_id=subscription_id,
    ):
        result = TelegramAccessResult(
            status="covered_by_active_subscription",
            revoked=True,
        )
    else:
        if action == "grant":
            result = await grant_group_access(bot, settings, telegram_id)
        else:
            result = await revoke_group_access(bot, settings, telegram_id)

    try:
        record_telegram_access_result(db, subscription_id, result, now)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "Failed to store Telegram access result for subscription_id=%s.",
            subscription_id,
        )

    logger.info(
        "Telegram access action=%s subscription_id=%s telegram_id=%s status=%s.",
        action,
        subscription_id,
        telegram_id,
        result.status,
    )
    return result


async def process_due_subscription_access(
    db: Session,
    bot: Bot,
    settings: Settings,
    now: datetime | None = None,
    limit: int = 100,
) -> int:
    now = now or datetime.now(UTC)
    processed = 0

    grant_candidates = subscriptions_needing_grant(db, now=now, limit=limit)
    db.rollback()
    granted_user_ids: set[int] = set()
    for subscription_id, _user_id, telegram_id in grant_candidates:
        if not claim_subscription_access_action(db, subscription_id, "grant", now):
            continue

        try:
            if _user_id in granted_user_ids:
                result = TelegramAccessResult(
                    status="covered_by_active_subscription",
                    granted=True,
                )
            else:
                result = await grant_group_access(bot, settings, telegram_id)
                if result.granted or result.invite_sent:
                    granted_user_ids.add(_user_id)
        except Exception:
            logger.exception(
                "Telegram grant action failed for subscription_id=%s.",
                subscription_id,
            )
            result = TelegramAccessResult(status="failed", retryable=True)

        try:
            record_telegram_access_result(db, subscription_id, result, now)
            db.commit()
            processed += 1
        except Exception:
            db.rollback()
            logger.exception(
                "Failed to store Telegram grant result for subscription_id=%s.",
                subscription_id,
            )

    revoke_candidates = subscriptions_needing_revoke(db, now=now, limit=limit)
    db.rollback()
    revoked_user_ids: set[int] = set()
    for subscription_id, _user_id, telegram_id in revoke_candidates:
        if not claim_subscription_access_action(db, subscription_id, "revoke", now):
            continue

        try:
            if _user_id in revoked_user_ids:
                result = TelegramAccessResult(status="revoked", revoked=True)
            else:
                result = await revoke_group_access(bot, settings, telegram_id)
                if result.revoked:
                    revoked_user_ids.add(_user_id)
        except Exception:
            logger.exception(
                "Telegram revoke action failed for subscription_id=%s.",
                subscription_id,
            )
            result = TelegramAccessResult(status="failed", retryable=True)

        try:
            record_telegram_access_result(db, subscription_id, result, now)
            db.commit()
            processed += 1
        except Exception:
            db.rollback()
            logger.exception(
                "Failed to store Telegram revoke result for subscription_id=%s.",
                subscription_id,
            )

    return processed


async def run_subscription_access_scheduler(bot: Bot, settings: Settings) -> None:
    logger.info(
        "Starting subscription access scheduler every %s seconds.",
        settings.access_check_interval_seconds,
    )
    while True:
        try:
            if db_module.SessionLocal is None:
                logger.warning("Subscription access scheduler skipped: database unavailable.")
            else:
                with db_module.SessionLocal() as session:
                    await process_due_subscription_access(session, bot, settings)
        except Exception:
            logger.exception("Subscription access scheduler iteration failed.")

        await asyncio.sleep(settings.access_check_interval_seconds)
