from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError

from bot import Settings

logger = logging.getLogger(__name__)

INVITE_LINK_TTL_DAYS = 7
MEMBER_STATUSES = {"creator", "administrator", "member"}
NO_PENDING_JOIN_REQUEST_STATUSES = {
    "already_member",
    "no_pending_join_request",
    "not_member",
}


@dataclass(frozen=True)
class TelegramAccessResult:
    status: str
    granted: bool = False
    revoked: bool = False
    invite_sent: bool = False
    retryable: bool = False


def telegram_error_status(error: Exception) -> str:
    message = str(error).lower()

    if "bot was blocked" in message or "user is deactivated" in message:
        return "bot_blocked"

    if any(
        marker in message
        for marker in (
            "not enough rights",
            "administrator",
            "chat_admin_required",
            "not a member of the chat",
            "chat not found",
            "can't invite",
            "can't restrict",
        )
    ):
        return "missing_permissions"

    if any(
        marker in message
        for marker in (
            "user not participant",
            "user not found",
            "participant_id_invalid",
            "hide_requester_missing",
            "requester_missing",
            "join request not found",
        )
    ):
        return "not_member"

    if "already" in message and "participant" in message:
        return "already_member"

    return "failed"


def chat_member_status(member: Any) -> str:
    status = getattr(member, "status", "")
    if hasattr(status, "value"):
        status = status.value
    return str(status)


def is_active_chat_member(member: Any) -> bool:
    status = chat_member_status(member)
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return status in MEMBER_STATUSES


async def current_group_membership(
    bot: Bot,
    settings: Settings,
    telegram_id: int,
) -> str:
    try:
        member = await bot.get_chat_member(
            chat_id=settings.telegram_group_id,
            user_id=telegram_id,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        return telegram_error_status(error)
    except TelegramAPIError:
        logger.warning(
            "Telegram API failed while checking group membership for telegram_id=%s.",
            telegram_id,
        )
        return "retryable_failure"

    status = chat_member_status(member)
    if is_active_chat_member(member):
        return "member"
    if status == "kicked":
        return "kicked"
    return "not_member"


async def approve_pending_join_request(
    bot: Bot,
    settings: Settings,
    telegram_id: int,
) -> TelegramAccessResult:
    try:
        await bot.approve_chat_join_request(
            chat_id=settings.telegram_group_id,
            user_id=telegram_id,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        status = telegram_error_status(error)
        if status == "already_member":
            return TelegramAccessResult(status="member", granted=True)
        if status in NO_PENDING_JOIN_REQUEST_STATUSES:
            return TelegramAccessResult(status="no_pending_join_request")
        return TelegramAccessResult(status=status)
    except TelegramAPIError:
        logger.warning(
            "Telegram API failed while approving join request for telegram_id=%s.",
            telegram_id,
        )
        return TelegramAccessResult(status="failed", retryable=True)

    return TelegramAccessResult(status="join_request_approved", granted=True)


async def send_single_use_invite(
    bot: Bot,
    settings: Settings,
    telegram_id: int,
) -> TelegramAccessResult:
    expire_date = datetime.now(UTC) + timedelta(days=INVITE_LINK_TTL_DAYS)

    try:
        invite = await bot.create_chat_invite_link(
            chat_id=settings.telegram_group_id,
            member_limit=1,
            expire_date=expire_date,
            name=f"vip-{telegram_id}",
        )
        await bot.send_message(
            telegram_id,
            "Your VIP subscription is active. Join the Telegram group here:\n"
            f"{invite.invite_link}",
        )
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        status = telegram_error_status(error)
        return TelegramAccessResult(
            status=status,
            retryable=status in {"missing_permissions", "bot_blocked", "failed"},
        )
    except TelegramAPIError:
        logger.warning(
            "Telegram API failed while sending group invite to telegram_id=%s.",
            telegram_id,
        )
        return TelegramAccessResult(status="failed", retryable=True)

    return TelegramAccessResult(status="invite_sent", invite_sent=True)


async def grant_group_access(
    bot: Bot,
    settings: Settings,
    telegram_id: int,
) -> TelegramAccessResult:
    membership = await current_group_membership(bot, settings, telegram_id)
    if membership == "member":
        return TelegramAccessResult(status="member", granted=True)
    if membership == "missing_permissions":
        return TelegramAccessResult(status="missing_permissions", retryable=True)
    if membership == "bot_blocked":
        return TelegramAccessResult(status="bot_blocked", retryable=True)
    if membership in {"failed", "retryable_failure"}:
        return TelegramAccessResult(status="failed", retryable=True)

    approval = await approve_pending_join_request(bot, settings, telegram_id)
    if approval.granted:
        return approval
    if approval.status not in NO_PENDING_JOIN_REQUEST_STATUSES:
        return approval

    return await send_single_use_invite(bot, settings, telegram_id)


async def revoke_group_access(
    bot: Bot,
    settings: Settings,
    telegram_id: int,
) -> TelegramAccessResult:
    membership = await current_group_membership(bot, settings, telegram_id)
    if membership == "not_member":
        return TelegramAccessResult(status="not_member", revoked=True)
    if membership == "missing_permissions":
        return TelegramAccessResult(status="missing_permissions", retryable=True)
    if membership == "bot_blocked":
        return TelegramAccessResult(status="bot_blocked", retryable=True)
    if membership in {"failed", "retryable_failure"}:
        return TelegramAccessResult(status="failed", retryable=True)

    if membership == "kicked":
        try:
            await bot.unban_chat_member(
                chat_id=settings.telegram_group_id,
                user_id=telegram_id,
                only_if_banned=True,
            )
        except (TelegramBadRequest, TelegramForbiddenError) as error:
            return TelegramAccessResult(
                status="unban_pending",
                retryable=telegram_error_status(error) != "not_member",
            )
        except TelegramAPIError:
            logger.warning(
                "Telegram API failed while retrying unban for telegram_id=%s.",
                telegram_id,
            )
            return TelegramAccessResult(status="unban_pending", retryable=True)

        return TelegramAccessResult(status="revoked", revoked=True)

    try:
        await bot.ban_chat_member(chat_id=settings.telegram_group_id, user_id=telegram_id)
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        status = telegram_error_status(error)
        if status == "not_member":
            return TelegramAccessResult(status="not_member", revoked=True)
        return TelegramAccessResult(status=status, retryable=True)
    except TelegramAPIError:
        logger.warning(
            "Telegram API failed while banning telegram_id=%s during revoke.",
            telegram_id,
        )
        return TelegramAccessResult(status="failed", retryable=True)

    try:
        await bot.unban_chat_member(
            chat_id=settings.telegram_group_id,
            user_id=telegram_id,
            only_if_banned=True,
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.warning(
            "Telegram unban failed after successful ban for telegram_id=%s.",
            telegram_id,
        )
        return TelegramAccessResult(status="unban_pending", retryable=True)
    except TelegramAPIError:
        logger.warning(
            "Telegram API failed while unbanning telegram_id=%s after revoke.",
            telegram_id,
        )
        return TelegramAccessResult(status="unban_pending", retryable=True)

    return TelegramAccessResult(status="revoked", revoked=True)
