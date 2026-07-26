import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import stripe
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

import app as app_module
import bot as bot_module
import subscription_access_service
import subscription_service
import telegram_access_service
from bot import Settings
from db import Base
from models import Plan, StripeEvent, Subscription, User


TEST_SETTINGS = Settings(
    bot_token="test-bot-token",
    payment_link_monthly="https://buy.stripe.com/monthly",
    payment_link_3_months="https://buy.stripe.com/three",
    payment_link_6_months="https://buy.stripe.com/six",
    payment_link_lifetime="https://buy.stripe.com/lifetime",
    stripe_webhook_secret="test-webhook-secret",
    telegram_group_id=-1001234567890,
    access_check_interval_seconds=300,
)


class FakeRequest:
    def __init__(self, body: bytes = b"", fastapi_app=app_module.app):
        self.app = fastapi_app
        self._body = body

    async def body(self) -> bytes:
        return self._body


async def wait_forever() -> None:
    await asyncio.Event().wait()


def response_json(response) -> dict:
    return json.loads(response.body)


def telegram_bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=None, message=message)


def telegram_forbidden(message: str) -> TelegramForbiddenError:
    return TelegramForbiddenError(method=None, message=message)


def fake_chat_member(status: str, is_member: bool | None = None):
    payload = {"status": status}
    if is_member is not None:
        payload["is_member"] = is_member
    return SimpleNamespace(**payload)


class PlanDurationTests(unittest.TestCase):
    def test_plan_duration_prefers_specific_project_rules(self) -> None:
        self.assertEqual(
            subscription_service.resolve_plan_duration("monthly", {}).days,
            30,
        )
        self.assertEqual(
            subscription_service.resolve_plan_duration("3_months", {}).days,
            90,
        )
        self.assertEqual(
            subscription_service.resolve_plan_duration("6_months", {}).days,
            180,
        )
        lifetime = subscription_service.resolve_plan_duration("lifetime", {})
        self.assertTrue(lifetime.is_valid)
        self.assertIsNone(lifetime.days)

    def test_invalid_duration_is_rejected(self) -> None:
        invalid = subscription_service.resolve_plan_duration(
            "monthly",
            {"duration_days": "-1"},
        )
        self.assertFalse(invalid.is_valid)
        self.assertEqual(invalid.reason, "invalid_duration_days")


class SettingsTests(unittest.TestCase):
    def test_load_settings_uses_group_id_fallback(self) -> None:
        env = {
            "BOT_TOKEN": "token",
            "PAYMENT_LINK_MONTHLY": "https://buy.stripe.com/monthly",
            "PAYMENT_LINK_3_MONTHS": "https://buy.stripe.com/three",
            "PAYMENT_LINK_6_MONTHS": "https://buy.stripe.com/six",
            "PAYMENT_LINK_LIFETIME": "https://buy.stripe.com/lifetime",
            "STRIPE_WEBHOOK_SECRET": "secret",
            "GROUP_ID": "-1001234567890",
            "ACCESS_CHECK_INTERVAL_SECONDS": "600",
        }

        with patch.dict(bot_module.os.environ, env, clear=True):
            with self.assertLogs("bot", level="WARNING") as logs:
                settings = bot_module.load_settings()

        self.assertEqual(settings.telegram_group_id, -1001234567890)
        self.assertEqual(settings.access_check_interval_seconds, 600)
        self.assertIn("GROUP_ID is deprecated", "\n".join(logs.output))

    def test_telegram_group_id_takes_priority_over_group_id(self) -> None:
        env = {
            "BOT_TOKEN": "token",
            "PAYMENT_LINK_MONTHLY": "https://buy.stripe.com/monthly",
            "PAYMENT_LINK_3_MONTHS": "https://buy.stripe.com/three",
            "PAYMENT_LINK_6_MONTHS": "https://buy.stripe.com/six",
            "PAYMENT_LINK_LIFETIME": "https://buy.stripe.com/lifetime",
            "STRIPE_WEBHOOK_SECRET": "secret",
            "TELEGRAM_GROUP_ID": "-1009999999999",
            "GROUP_ID": "-1001234567890",
        }

        with patch.dict(bot_module.os.environ, env, clear=True):
            settings = bot_module.load_settings()

        self.assertEqual(settings.telegram_group_id, -1009999999999)

    def test_access_check_interval_rejects_too_small_value(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "60 to 86400"):
            bot_module.parse_positive_int("ACCESS_CHECK_INTERVAL_SECONDS", "59", 300)

    def test_access_check_interval_rejects_too_large_value(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "60 to 86400"):
            bot_module.parse_positive_int("ACCESS_CHECK_INTERVAL_SECONDS", "86401", 300)

    def test_access_check_interval_accepts_valid_value(self) -> None:
        self.assertEqual(
            bot_module.parse_positive_int("ACCESS_CHECK_INTERVAL_SECONDS", "60", 300),
            60,
        )


class MigrationSmokeTests(unittest.TestCase):
    def test_alembic_upgrade_and_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "migration-smoke.sqlite").replace(
                "\\",
                "/",
            )
            env = os.environ.copy()
            env["DATABASE_URL"] = f"sqlite+pysqlite:///{database_path}"
            env["PYTHONDONTWRITEBYTECODE"] = "1"

            upgrade = subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=os.getcwd(),
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(upgrade.returncode, 0, upgrade.stdout + upgrade.stderr)

            downgrade = subprocess.run(
                [sys.executable, "-m", "alembic", "downgrade", "base"],
                cwd=os.getcwd(),
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(downgrade.returncode, 0, downgrade.stdout + downgrade.stderr)


class HealthEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        for attr_name in ("polling_task", "access_scheduler_task"):
            task = getattr(app_module.app.state, attr_name, None)
            if task is not None and not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        app_module.app.state.polling_task = None
        app_module.app.state.access_scheduler_task = None

    async def test_health_returns_200_when_polling_and_scheduler_are_running(self) -> None:
        polling_task = asyncio.create_task(wait_forever())
        scheduler_task = asyncio.create_task(wait_forever())
        app_module.app.state.polling_task = polling_task
        app_module.app.state.access_scheduler_task = scheduler_task

        response = await app_module.health(FakeRequest())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response_json(response)["scheduler"], "running")

    async def test_health_returns_503_when_polling_task_finished(self) -> None:
        async def finished() -> None:
            return None

        polling_task = asyncio.create_task(finished())
        await polling_task
        app_module.app.state.polling_task = polling_task
        app_module.app.state.access_scheduler_task = asyncio.create_task(wait_forever())

        response = await app_module.health(FakeRequest())

        self.assertEqual(response.status_code, 503)

    async def test_health_returns_503_when_scheduler_task_finished(self) -> None:
        async def finished() -> None:
            return None

        scheduler_task = asyncio.create_task(finished())
        await scheduler_task
        app_module.app.state.polling_task = asyncio.create_task(wait_forever())
        app_module.app.state.access_scheduler_task = scheduler_task

        response = await app_module.health(FakeRequest())

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response_json(response)["scheduler"], "stopped")


class BotPollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_polling_clears_webhook_before_starting_polling(self) -> None:
        bot = AsyncMock()

        with patch.object(
            bot_module.dp,
            "start_polling",
            new_callable=AsyncMock,
        ) as start_polling:
            await bot_module.run_polling(bot)

        bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
        start_polling.assert_awaited_once_with(bot)


class TelegramAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_grant_access_detects_existing_group_member(self) -> None:
        bot = AsyncMock()
        bot.get_chat_member.return_value = fake_chat_member("member")

        result = await telegram_access_service.grant_group_access(
            bot,
            TEST_SETTINGS,
            telegram_id=123456,
        )

        self.assertEqual(result.status, "member")
        self.assertTrue(result.granted)
        bot.create_chat_invite_link.assert_not_called()
        bot.send_message.assert_not_called()

    async def test_grant_access_sends_invite_when_user_never_joined(self) -> None:
        bot = AsyncMock()
        bot.get_chat_member.return_value = fake_chat_member("left")
        bot.approve_chat_join_request.side_effect = telegram_bad_request(
            "Bad Request: HIDE_REQUESTER_MISSING"
        )
        bot.create_chat_invite_link.return_value = SimpleNamespace(
            invite_link="https://t.me/+test"
        )

        result = await telegram_access_service.grant_group_access(
            bot,
            TEST_SETTINGS,
            telegram_id=123456,
        )

        self.assertEqual(result.status, "invite_sent")
        self.assertFalse(result.granted)
        self.assertTrue(result.invite_sent)
        bot.create_chat_invite_link.assert_awaited_once()
        bot.send_message.assert_awaited_once()

    async def test_grant_access_handles_missing_bot_permissions(self) -> None:
        bot = AsyncMock()
        bot.get_chat_member.side_effect = telegram_bad_request(
            "Bad Request: not enough rights"
        )

        result = await telegram_access_service.grant_group_access(
            bot,
            TEST_SETTINGS,
            telegram_id=123456,
        )

        self.assertEqual(result.status, "missing_permissions")
        self.assertFalse(result.granted)
        bot.create_chat_invite_link.assert_not_called()

    async def test_grant_access_handles_user_blocking_bot(self) -> None:
        bot = AsyncMock()
        bot.get_chat_member.return_value = fake_chat_member("left")
        bot.approve_chat_join_request.side_effect = telegram_bad_request(
            "Bad Request: HIDE_REQUESTER_MISSING"
        )
        bot.create_chat_invite_link.return_value = SimpleNamespace(
            invite_link="https://t.me/+test"
        )
        bot.send_message.side_effect = telegram_forbidden(
            "Forbidden: bot was blocked by the user"
        )

        result = await telegram_access_service.grant_group_access(
            bot,
            TEST_SETTINGS,
            telegram_id=123456,
        )

        self.assertEqual(result.status, "bot_blocked")
        self.assertFalse(result.granted)
        self.assertTrue(result.retryable)

    async def test_grant_access_approves_pending_join_request(self) -> None:
        bot = AsyncMock()
        bot.get_chat_member.return_value = fake_chat_member("left")

        result = await telegram_access_service.grant_group_access(
            bot,
            TEST_SETTINGS,
            telegram_id=123456,
        )

        self.assertEqual(result.status, "join_request_approved")
        self.assertTrue(result.granted)
        bot.approve_chat_join_request.assert_awaited_once_with(
            chat_id=TEST_SETTINGS.telegram_group_id,
            user_id=123456,
        )
        bot.create_chat_invite_link.assert_not_called()

    async def test_revoke_access_removes_existing_group_member(self) -> None:
        bot = AsyncMock()
        bot.get_chat_member.return_value = fake_chat_member("member")

        result = await telegram_access_service.revoke_group_access(
            bot,
            TEST_SETTINGS,
            telegram_id=123456,
        )

        self.assertEqual(result.status, "revoked")
        self.assertTrue(result.revoked)
        bot.ban_chat_member.assert_awaited_once_with(
            chat_id=TEST_SETTINGS.telegram_group_id,
            user_id=123456,
        )
        bot.unban_chat_member.assert_awaited_once_with(
            chat_id=TEST_SETTINGS.telegram_group_id,
            user_id=123456,
            only_if_banned=True,
        )

    async def test_revoke_access_handles_user_who_never_joined(self) -> None:
        bot = AsyncMock()
        bot.get_chat_member.return_value = fake_chat_member("left")

        result = await telegram_access_service.revoke_group_access(
            bot,
            TEST_SETTINGS,
            telegram_id=123456,
        )

        self.assertEqual(result.status, "not_member")
        self.assertTrue(result.revoked)
        bot.ban_chat_member.assert_not_called()

    async def test_revoke_access_unban_pending_after_unban_failure_then_retry(self) -> None:
        bot = AsyncMock()
        bot.get_chat_member.return_value = fake_chat_member("member")
        bot.unban_chat_member.side_effect = [
            telegram_bad_request("Bad Request: not enough rights"),
            None,
        ]

        first_result = await telegram_access_service.revoke_group_access(
            bot,
            TEST_SETTINGS,
            telegram_id=123456,
        )
        self.assertEqual(first_result.status, "unban_pending")
        self.assertTrue(first_result.retryable)

        bot.get_chat_member.return_value = fake_chat_member("kicked")
        second_result = await telegram_access_service.revoke_group_access(
            bot,
            TEST_SETTINGS,
            telegram_id=123456,
        )
        self.assertEqual(second_result.status, "revoked")
        self.assertTrue(second_result.revoked)
        self.assertEqual(bot.unban_chat_member.await_count, 2)


class SubscriptionAccessSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )

    def tearDown(self) -> None:
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def create_subscription(
        self,
        db,
        telegram_id: int = 123456,
        suffix: str = "scheduler",
        status: str = "active",
        ends_at: datetime | None = None,
        telegram_access_granted_at: datetime | None = None,
        telegram_access_status: str | None = None,
        telegram_access_retry_at: datetime | None = None,
    ) -> Subscription:
        plan = Plan(code=f"monthly_{suffix}", name="Monthly VIP", is_active=True)
        user = User(telegram_id=telegram_id)
        subscription = Subscription(
            user=user,
            plan=plan,
            stripe_subscription_id=f"sub_{suffix}",
            stripe_checkout_session_id=f"cs_{suffix}",
            status=status,
            current_period_start=datetime(2026, 7, 1, tzinfo=UTC),
            current_period_end=ends_at,
            started_at=datetime(2026, 7, 1, tzinfo=UTC),
            ends_at=ends_at,
            telegram_access_granted_at=telegram_access_granted_at,
            telegram_access_status=telegram_access_status,
            telegram_access_retry_at=telegram_access_retry_at,
        )
        db.add(subscription)
        db.commit()
        return subscription

    async def test_scheduler_revokes_expired_subscription(self) -> None:
        now = datetime(2026, 7, 25, tzinfo=UTC)
        fake_bot = AsyncMock()
        fake_bot.get_chat_member.return_value = fake_chat_member("member")

        with self.session_factory() as db:
            self.create_subscription(
                db,
                ends_at=now - timedelta(days=1),
                telegram_access_granted_at=now - timedelta(days=20),
            )

            processed = await subscription_access_service.process_due_subscription_access(
                db,
                fake_bot,
                TEST_SETTINGS,
                now=now,
            )
            subscription = db.scalar(select(Subscription))

        self.assertEqual(processed, 1)
        fake_bot.ban_chat_member.assert_awaited_once()
        self.assertEqual(subscription.status, "expired")
        self.assertEqual(subscription.telegram_access_status, "revoked")
        self.assertIsNotNone(subscription.telegram_access_revoked_at)

    async def test_scheduler_does_not_revoke_lifetime_subscription(self) -> None:
        now = datetime(2026, 7, 25, tzinfo=UTC)
        fake_bot = AsyncMock()

        with self.session_factory() as db:
            self.create_subscription(
                db,
                ends_at=None,
                telegram_access_granted_at=now - timedelta(days=20),
            )

            processed = await subscription_access_service.process_due_subscription_access(
                db,
                fake_bot,
                TEST_SETTINGS,
                now=now,
            )

        self.assertEqual(processed, 0)
        fake_bot.ban_chat_member.assert_not_called()

    async def test_scheduler_retries_unban_pending_subscription(self) -> None:
        now = datetime(2026, 7, 25, tzinfo=UTC)
        fake_bot = AsyncMock()
        fake_bot.get_chat_member.return_value = fake_chat_member("kicked")

        with self.session_factory() as db:
            self.create_subscription(
                db,
                suffix="unban",
                status="canceled",
                ends_at=now - timedelta(days=1),
                telegram_access_granted_at=now - timedelta(days=20),
                telegram_access_status="unban_pending",
                telegram_access_retry_at=now - timedelta(minutes=1),
            )

            processed = await subscription_access_service.process_due_subscription_access(
                db,
                fake_bot,
                TEST_SETTINGS,
                now=now,
            )
            subscription = db.scalar(select(Subscription))

        self.assertEqual(processed, 1)
        fake_bot.unban_chat_member.assert_awaited_once()
        self.assertEqual(subscription.telegram_access_status, "revoked")

    async def test_scheduler_continues_after_one_user_raises(self) -> None:
        now = datetime(2026, 7, 25, tzinfo=UTC)
        fake_bot = AsyncMock()

        with self.session_factory() as db:
            self.create_subscription(
                db,
                telegram_id=111,
                suffix="first",
                ends_at=now - timedelta(days=1),
                telegram_access_granted_at=now - timedelta(days=20),
            )
            self.create_subscription(
                db,
                telegram_id=222,
                suffix="second",
                ends_at=now - timedelta(days=1),
                telegram_access_granted_at=now - timedelta(days=20),
            )

            with patch.object(
                subscription_access_service,
                "revoke_group_access",
                side_effect=[
                    RuntimeError("network down"),
                    telegram_access_service.TelegramAccessResult(
                        status="revoked",
                        revoked=True,
                    ),
                ],
            ):
                processed = await subscription_access_service.process_due_subscription_access(
                    db,
                    fake_bot,
                    TEST_SETTINGS,
                    now=now,
                )
            subscriptions = db.scalars(
                select(Subscription).order_by(Subscription.user_id)
            ).all()

        self.assertEqual(processed, 2)
        self.assertEqual(subscriptions[0].telegram_access_status, "failed")
        self.assertIsNotNone(subscriptions[0].telegram_access_retry_at)
        self.assertEqual(subscriptions[1].telegram_access_status, "revoked")

    async def test_claim_prevents_same_subscription_from_being_claimed_twice(self) -> None:
        now = datetime(2026, 7, 25, tzinfo=UTC)

        with self.session_factory() as db:
            subscription = self.create_subscription(
                db,
                suffix="claim",
                ends_at=now - timedelta(days=1),
                telegram_access_granted_at=now - timedelta(days=20),
            )

            first_claim = subscription_access_service.claim_subscription_access_action(
                db,
                subscription.id,
                "revoke",
                now,
            )
            second_claim = subscription_access_service.claim_subscription_access_action(
                db,
                subscription.id,
                "revoke",
                now,
            )

        self.assertTrue(first_claim)
        self.assertFalse(second_claim)


class StripeWebhookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.previous_settings = bot_module.settings
        self.previous_app_bot = getattr(app_module.app.state, "bot", None)
        bot_module.set_settings(TEST_SETTINGS)
        app_module.app.state.bot = None
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )

    def tearDown(self) -> None:
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()
        bot_module.settings = self.previous_settings
        app_module.app.state.bot = self.previous_app_bot

    def create_plan(
        self,
        db,
        code: str = "monthly",
        name: str = "Monthly VIP",
        stripe_price_id: str | None = None,
    ) -> Plan:
        plan = Plan(
            code=code,
            name=name,
            stripe_price_id=stripe_price_id,
            is_active=True,
        )
        db.add(plan)
        db.commit()
        return plan

    def checkout_event(
        self,
        event_id: str = "evt_test_123",
        session_id: str = "cs_test_123",
        client_reference_id: str | None = "123456",
        plan_code: str | None = "monthly",
        duration_days: str | None = "30",
        payment_status: str = "paid",
        payment_link: str | None = None,
        subscription: object | None = None,
        event_created: int = 1784937600,
        metadata_extra: dict | None = None,
    ) -> dict:
        metadata = {}
        if plan_code is not None:
            metadata["plan_code"] = plan_code
        if duration_days is not None:
            metadata["duration_days"] = duration_days
        if metadata_extra:
            metadata.update(metadata_extra)

        session = {
            "id": session_id,
            "customer": "cus_test_123",
            "payment_status": payment_status,
            "amount_total": 2900,
            "currency": "eur",
            "created": 1784937600,
            "metadata": metadata,
        }
        if client_reference_id is not None:
            session["client_reference_id"] = client_reference_id
        if payment_link is not None:
            session["payment_link"] = payment_link
        if subscription is not None:
            session["subscription"] = subscription

        return {
            "id": event_id,
            "type": "checkout.session.completed",
            "created": event_created,
            "api_version": "2026-07-12",
            "livemode": False,
            "data": {"object": session},
        }

    def subscription_event(
        self,
        event_id: str = "evt_subscription_deleted",
        event_type: str = "customer.subscription.deleted",
        stripe_subscription_id: str = "sub_test_123",
        status: str = "canceled",
        event_created: int = 1784937600,
    ) -> dict:
        return {
            "id": event_id,
            "type": event_type,
            "created": event_created,
            "api_version": "2026-07-12",
            "livemode": False,
            "data": {
                "object": {
                    "id": stripe_subscription_id,
                    "status": status,
                    "canceled_at": 1784937600,
                    "current_period_start": 1782345600,
                    "current_period_end": 1784937600,
                }
            },
        }

    async def send_event(self, db, event: dict):
        with patch.object(
            app_module.stripe.Webhook,
            "construct_event",
            return_value=event,
        ) as construct_event:
            response = await app_module.stripe_webhook(
                FakeRequest(body=json.dumps({"id": event["id"]}).encode()),
                stripe_signature="valid-signature",
                db=db,
            )

        construct_event.assert_called_once_with(
            json.dumps({"id": event["id"]}).encode(),
            "valid-signature",
            TEST_SETTINGS.stripe_webhook_secret,
        )
        return response

    async def test_webhook_without_signature_returns_400(self) -> None:
        response = await app_module.stripe_webhook(
            FakeRequest(body=b"{}"), stripe_signature=None
        )

        self.assertEqual(response.status_code, 400)

    async def test_webhook_with_invalid_signature_returns_400(self) -> None:
        with patch.object(
            app_module.stripe.Webhook,
            "construct_event",
            side_effect=stripe.error.SignatureVerificationError(
                "Invalid signature", "Stripe-Signature"
            ),
        ):
            response = await app_module.stripe_webhook(
                FakeRequest(body=b"{}"), stripe_signature="bad-signature"
            )

        self.assertEqual(response.status_code, 400)

    async def test_checkout_completed_creates_user_plan_subscription_and_event(
        self,
    ) -> None:
        event = self.checkout_event(
            subscription={
                "id": "sub_test_123",
                "status": "active",
                "current_period_start": 1784937600,
                "current_period_end": 1787529600,
                "start_date": 1784937600,
            },
        )

        with self.session_factory() as db:
            response = await self.send_event(db, event)

            stored_event = db.scalar(
                select(StripeEvent).where(
                    StripeEvent.stripe_event_id == "evt_test_123"
                )
            )
            user = db.scalar(select(User).where(User.telegram_id == 123456))
            plan = db.scalar(select(Plan).where(Plan.code == "monthly"))
            subscription = db.scalar(
                select(Subscription).where(
                    Subscription.stripe_checkout_session_id == "cs_test_123"
                )
            )

        body = response_json(response)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["processed"])
        self.assertIsNotNone(stored_event)
        self.assertIsNotNone(stored_event.processed_at)
        self.assertEqual(stored_event.payload["id"], "evt_test_123")
        self.assertIsNotNone(user)
        self.assertEqual(user.stripe_customer_id, "cus_test_123")
        self.assertIsNotNone(plan)
        self.assertEqual(plan.amount_cents, 2900)
        self.assertIsNotNone(subscription)
        self.assertEqual(subscription.status, "active")
        self.assertEqual(subscription.user_id, user.id)
        self.assertEqual(subscription.plan_id, plan.id)
        self.assertEqual(subscription.stripe_subscription_id, "sub_test_123")
        self.assertEqual(
            subscription.current_period_start,
            datetime(2026, 7, 25),
        )
        self.assertEqual(
            subscription.ends_at,
            datetime(2026, 8, 24),
        )

    async def test_checkout_completed_accepts_stripe_event_object(self) -> None:
        event = self.checkout_event(
            event_id="evt_test_stripe_object",
            session_id="cs_test_stripe_object",
            subscription={
                "id": "sub_test_stripe_object",
                "status": "active",
                "current_period_start": 1784937600,
                "current_period_end": 1787529600,
            },
        )
        stripe_event_object = stripe.Event.construct_from(event, "sk_test")

        with patch.object(
            app_module.stripe.Webhook,
            "construct_event",
            return_value=stripe_event_object,
        ):
            with self.session_factory() as db:
                response = await app_module.stripe_webhook(
                    FakeRequest(body=json.dumps({"id": event["id"]}).encode()),
                    stripe_signature="valid-signature",
                    db=db,
                )
                stored_event = db.scalar(
                    select(StripeEvent).where(
                        StripeEvent.stripe_event_id == "evt_test_stripe_object"
                    )
                )
                subscription = db.scalar(
                    select(Subscription).where(
                        Subscription.stripe_checkout_session_id
                        == "cs_test_stripe_object"
                    )
                )

        body = response_json(response)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["processed"])
        self.assertIsNotNone(stored_event)
        self.assertIsNotNone(stored_event.processed_at)
        self.assertEqual(stored_event.event_type, "checkout.session.completed")
        self.assertIsNotNone(subscription)
        self.assertEqual(subscription.stripe_subscription_id, "sub_test_stripe_object")

    async def test_checkout_completed_sends_group_invite_after_subscription_saved(
        self,
    ) -> None:
        fake_bot = AsyncMock()
        fake_bot.get_chat_member.return_value = fake_chat_member("left")
        fake_bot.approve_chat_join_request.side_effect = telegram_bad_request(
            "Bad Request: HIDE_REQUESTER_MISSING"
        )
        fake_bot.create_chat_invite_link.return_value = SimpleNamespace(
            invite_link="https://t.me/+test"
        )
        app_module.app.state.bot = fake_bot
        event = self.checkout_event(
            event_id="evt_test_access",
            session_id="cs_test_access",
            subscription={
                "id": "sub_test_access",
                "status": "active",
                "current_period_start": 1784937600,
                "current_period_end": 1787529600,
            },
        )

        with self.session_factory() as db:
            response = await self.send_event(db, event)
            subscription = db.scalar(
                select(Subscription).where(
                    Subscription.stripe_checkout_session_id == "cs_test_access"
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response_json(response)["processed"])
        fake_bot.send_message.assert_awaited_once()
        self.assertIsNotNone(subscription)
        self.assertEqual(subscription.telegram_access_status, "invite_sent")
        self.assertIsNone(subscription.telegram_access_granted_at)
        self.assertIsNotNone(subscription.telegram_invite_sent_at)

    async def test_checkout_completed_approves_pending_join_request_after_save(
        self,
    ) -> None:
        fake_bot = AsyncMock()
        fake_bot.get_chat_member.return_value = fake_chat_member("left")
        app_module.app.state.bot = fake_bot
        event = self.checkout_event(
            event_id="evt_test_join_request",
            session_id="cs_test_join_request",
            subscription={
                "id": "sub_test_join_request",
                "status": "active",
                "current_period_start": 1784937600,
                "current_period_end": 1787529600,
            },
        )

        with self.session_factory() as db:
            response = await self.send_event(db, event)
            subscription = db.scalar(
                select(Subscription).where(
                    Subscription.stripe_checkout_session_id == "cs_test_join_request"
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response_json(response)["processed"])
        fake_bot.approve_chat_join_request.assert_awaited_once_with(
            chat_id=TEST_SETTINGS.telegram_group_id,
            user_id=123456,
        )
        fake_bot.create_chat_invite_link.assert_not_called()
        self.assertEqual(subscription.telegram_access_status, "join_request_approved")
        self.assertIsNotNone(subscription.telegram_access_granted_at)

    async def test_checkout_completed_with_unpaid_status_is_not_fulfilled(self) -> None:
        event = self.checkout_event(
            event_id="evt_test_unpaid_checkout",
            session_id="cs_test_unpaid_checkout",
            payment_status="unpaid",
        )

        with self.session_factory() as db:
            response = await self.send_event(db, event)
            user_count = len(db.scalars(select(User)).all())
            subscription_count = len(db.scalars(select(Subscription)).all())

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response_json(response)["processed"])
        self.assertEqual(
            response_json(response)["reason"],
            "unsupported_payment_status",
        )
        self.assertEqual(user_count, 0)
        self.assertEqual(subscription_count, 0)

    async def test_checkout_completed_allows_explicit_no_payment_trial(self) -> None:
        event = self.checkout_event(
            event_id="evt_test_trial_checkout",
            session_id="cs_test_trial_checkout",
            payment_status="no_payment_required",
            metadata_extra={"allow_trial": "true"},
            subscription={
                "id": "sub_trial",
                "status": "trialing",
                "current_period_start": 1784937600,
                "current_period_end": 1787529600,
            },
        )

        with self.session_factory() as db:
            response = await self.send_event(db, event)
            subscription = db.scalar(
                select(Subscription).where(
                    Subscription.stripe_checkout_session_id == "cs_test_trial_checkout"
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response_json(response)["processed"])
        self.assertIsNotNone(subscription)
        self.assertEqual(subscription.status, "trialing")

    async def test_checkout_completed_rejects_no_payment_without_trial_flag(self) -> None:
        event = self.checkout_event(
            event_id="evt_test_no_payment_no_trial",
            session_id="cs_test_no_payment_no_trial",
            payment_status="no_payment_required",
            subscription={
                "id": "sub_no_trial",
                "status": "trialing",
            },
        )

        with self.session_factory() as db:
            response = await self.send_event(db, event)
            subscription_count = len(db.scalars(select(Subscription)).all())

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response_json(response)["processed"])
        self.assertEqual(subscription_count, 0)

    async def test_duplicate_webhook_does_not_send_second_invite(self) -> None:
        fake_bot = AsyncMock()
        fake_bot.get_chat_member.return_value = fake_chat_member("left")
        fake_bot.approve_chat_join_request.side_effect = telegram_bad_request(
            "Bad Request: HIDE_REQUESTER_MISSING"
        )
        fake_bot.create_chat_invite_link.return_value = SimpleNamespace(
            invite_link="https://t.me/+test"
        )
        app_module.app.state.bot = fake_bot
        event = self.checkout_event(
            event_id="evt_test_duplicate_invite",
            session_id="cs_test_duplicate_invite",
            subscription="sub_test_duplicate_invite",
        )

        with self.session_factory() as db:
            first_response = await self.send_event(db, event)
            second_response = await self.send_event(db, event)

        self.assertTrue(response_json(first_response)["processed"])
        self.assertFalse(response_json(second_response)["processed"])
        fake_bot.create_chat_invite_link.assert_awaited_once()

    async def test_second_checkout_event_for_same_session_does_not_send_valid_invite_again(
        self,
    ) -> None:
        fake_bot = AsyncMock()
        fake_bot.get_chat_member.return_value = fake_chat_member("left")
        fake_bot.approve_chat_join_request.side_effect = telegram_bad_request(
            "Bad Request: HIDE_REQUESTER_MISSING"
        )
        fake_bot.create_chat_invite_link.return_value = SimpleNamespace(
            invite_link="https://t.me/+test"
        )
        app_module.app.state.bot = fake_bot
        first_event = self.checkout_event(
            event_id="evt_test_invite_once_a",
            session_id="cs_test_invite_once",
            subscription="sub_test_invite_once",
            event_created=1784937600,
        )
        second_event = self.checkout_event(
            event_id="evt_test_invite_once_b",
            session_id="cs_test_invite_once",
            subscription="sub_test_invite_once",
            event_created=1784937601,
        )

        with self.session_factory() as db:
            await self.send_event(db, first_event)
            second_response = await self.send_event(db, second_event)

        self.assertTrue(response_json(second_response)["processed"])
        fake_bot.create_chat_invite_link.assert_awaited_once()

    async def test_repurchase_after_revoked_subscription_can_send_new_invite(
        self,
    ) -> None:
        fake_bot = AsyncMock()
        fake_bot.get_chat_member.return_value = fake_chat_member("left")
        fake_bot.approve_chat_join_request.side_effect = telegram_bad_request(
            "Bad Request: HIDE_REQUESTER_MISSING"
        )
        fake_bot.create_chat_invite_link.return_value = SimpleNamespace(
            invite_link="https://t.me/+test"
        )
        app_module.app.state.bot = fake_bot

        with self.session_factory() as db:
            plan = self.create_plan(db)
            user = User(telegram_id=123456)
            old_subscription = Subscription(
                user=user,
                plan=plan,
                stripe_subscription_id="sub_old_revoked",
                stripe_checkout_session_id="cs_old_revoked",
                status="expired",
                current_period_start=datetime(2026, 6, 1),
                current_period_end=datetime(2026, 7, 1),
                started_at=datetime(2026, 6, 1),
                ends_at=datetime(2026, 7, 1),
                telegram_access_granted_at=datetime(2026, 6, 1),
                telegram_access_revoked_at=datetime(2026, 7, 1),
            )
            db.add(old_subscription)
            db.commit()

            response = await self.send_event(
                db,
                self.checkout_event(
                    event_id="evt_test_repurchase",
                    session_id="cs_test_repurchase",
                    subscription="sub_test_repurchase",
                    event_created=1784937600,
                ),
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response_json(response)["processed"])
        fake_bot.create_chat_invite_link.assert_awaited_once()

    async def test_missing_client_reference_id_is_not_fulfilled(self) -> None:
        event = self.checkout_event(
            event_id="evt_test_missing_ref",
            session_id="cs_test_missing_ref",
            client_reference_id=None,
        )

        with self.session_factory() as db:
            response = await self.send_event(db, event)

            stored_event = db.scalar(
                select(StripeEvent).where(
                    StripeEvent.stripe_event_id == "evt_test_missing_ref"
                )
            )
            user_count = len(db.scalars(select(User)).all())
            subscription_count = len(db.scalars(select(Subscription)).all())

        body = response_json(response)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(body["processed"])
        self.assertEqual(body["reason"], "missing_telegram_id")
        self.assertIsNotNone(stored_event)
        self.assertIsNone(stored_event.processed_at)
        self.assertEqual(user_count, 0)
        self.assertEqual(subscription_count, 0)

    async def test_non_integer_client_reference_id_is_not_fulfilled(self) -> None:
        event = self.checkout_event(
            event_id="evt_test_bad_ref",
            session_id="cs_test_bad_ref",
            client_reference_id="not-a-number",
        )

        with self.session_factory() as db:
            response = await self.send_event(db, event)

            user_count = len(db.scalars(select(User)).all())
            subscription_count = len(db.scalars(select(Subscription)).all())

        body = response_json(response)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(body["processed"])
        self.assertEqual(body["reason"], "missing_telegram_id")
        self.assertEqual(user_count, 0)
        self.assertEqual(subscription_count, 0)

    async def test_missing_plan_code_without_mapping_is_not_fulfilled(self) -> None:
        event = self.checkout_event(
            event_id="evt_test_missing_plan",
            session_id="cs_test_missing_plan",
            plan_code=None,
            duration_days=None,
        )

        with self.session_factory() as db:
            response = await self.send_event(db, event)

            plan_count = len(db.scalars(select(Plan)).all())
            subscription_count = len(db.scalars(select(Subscription)).all())

        body = response_json(response)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(body["processed"])
        self.assertEqual(body["reason"], "unknown_plan")
        self.assertEqual(plan_count, 0)
        self.assertEqual(subscription_count, 0)

    async def test_unknown_plan_code_is_not_fulfilled(self) -> None:
        event = self.checkout_event(
            event_id="evt_test_unknown_plan",
            session_id="cs_test_unknown_plan",
            plan_code="vip_custom",
            duration_days=None,
        )

        with self.session_factory() as db:
            response = await self.send_event(db, event)

            user_count = len(db.scalars(select(User)).all())
            plan_count = len(db.scalars(select(Plan)).all())
            subscription_count = len(db.scalars(select(Subscription)).all())

        body = response_json(response)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(body["processed"])
        self.assertEqual(body["reason"], "unknown_plan")
        self.assertEqual(user_count, 0)
        self.assertEqual(plan_count, 0)
        self.assertEqual(subscription_count, 0)

    async def test_duplicate_event_id_does_not_create_duplicates(self) -> None:
        event = self.checkout_event(
            event_id="evt_test_retry",
            session_id="cs_test_retry",
            subscription="sub_test_retry",
        )

        with self.session_factory() as db:
            first_response = await self.send_event(db, event)
            second_response = await self.send_event(db, event)

            stripe_events = db.scalars(select(StripeEvent)).all()
            users = db.scalars(select(User)).all()
            plans = db.scalars(select(Plan)).all()
            subscriptions = db.scalars(select(Subscription)).all()

        first_body = response_json(first_response)
        second_body = response_json(second_response)

        self.assertEqual(first_response.status_code, 200)
        self.assertTrue(first_body["processed"])
        self.assertEqual(second_response.status_code, 200)
        self.assertFalse(second_body["processed"])
        self.assertEqual(second_body["reason"], "already_processed")
        self.assertEqual(len(stripe_events), 1)
        self.assertEqual(len(users), 1)
        self.assertEqual(len(plans), 1)
        self.assertEqual(len(subscriptions), 1)

    async def test_same_event_id_in_second_session_is_not_processed_again(self) -> None:
        event = self.checkout_event(
            event_id="evt_test_two_webhooks",
            session_id="cs_test_two_webhooks",
            subscription="sub_test_two_webhooks",
        )

        with self.session_factory() as db:
            first_response = await self.send_event(db, event)

        with self.session_factory() as db:
            second_response = await self.send_event(db, event)
            stripe_event_count = len(db.scalars(select(StripeEvent)).all())
            subscription_count = len(db.scalars(select(Subscription)).all())

        self.assertTrue(response_json(first_response)["processed"])
        self.assertFalse(response_json(second_response)["processed"])
        self.assertEqual(response_json(second_response)["reason"], "already_processed")
        self.assertEqual(stripe_event_count, 1)
        self.assertEqual(subscription_count, 1)

    async def test_lifetime_subscription_has_no_expires_at(self) -> None:
        event = self.checkout_event(
            event_id="evt_test_lifetime",
            session_id="cs_test_lifetime",
            plan_code="lifetime",
            duration_days=None,
            payment_link="https://buy.stripe.com/lifetime",
            subscription={
                "id": "sub_lifetime",
                "status": "active",
                "current_period_start": 1784937600,
                "current_period_end": 4102444800,
            },
        )

        with self.session_factory() as db:
            response = await self.send_event(db, event)
            subscription = db.scalar(
                select(Subscription).where(
                    Subscription.stripe_checkout_session_id == "cs_test_lifetime"
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response_json(response)["processed"])
        self.assertIsNotNone(subscription)
        self.assertIsNone(subscription.current_period_end)
        self.assertIsNone(subscription.ends_at)

    async def test_invalid_duration_days_rolls_no_business_rows(self) -> None:
        event = self.checkout_event(
            event_id="evt_test_invalid_duration",
            session_id="cs_test_invalid_duration",
            plan_code="monthly",
            duration_days="-1",
        )

        with self.session_factory() as db:
            response = await self.send_event(db, event)

            stored_event = db.scalar(
                select(StripeEvent).where(
                    StripeEvent.stripe_event_id == "evt_test_invalid_duration"
                )
            )
            user_count = len(db.scalars(select(User)).all())
            plan_count = len(db.scalars(select(Plan)).all())
            subscription_count = len(db.scalars(select(Subscription)).all())

        body = response_json(response)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(body["processed"])
        self.assertEqual(body["reason"], "invalid_duration_days")
        self.assertIsNotNone(stored_event)
        self.assertIsNone(stored_event.processed_at)
        self.assertEqual(user_count, 0)
        self.assertEqual(plan_count, 0)
        self.assertEqual(subscription_count, 0)

    async def test_subscription_save_error_rolls_back_whole_transaction(self) -> None:
        event = self.checkout_event(
            event_id="evt_test_rollback",
            session_id="cs_test_rollback",
        )

        with self.session_factory() as db:
            with patch.object(
                subscription_service,
                "upsert_subscription_from_checkout_session",
                side_effect=SQLAlchemyError("boom"),
            ):
                response = await self.send_event(db, event)

            stripe_event_count = len(db.scalars(select(StripeEvent)).all())
            user_count = len(db.scalars(select(User)).all())
            plan_count = len(db.scalars(select(Plan)).all())
            subscription_count = len(db.scalars(select(Subscription)).all())

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response_json(response)["detail"], "Could not process Stripe event.")
        self.assertEqual(stripe_event_count, 0)
        self.assertEqual(user_count, 0)
        self.assertEqual(plan_count, 0)
        self.assertEqual(subscription_count, 0)

    async def test_event_is_marked_processed_only_after_success(self) -> None:
        event = self.checkout_event(
            event_id="evt_test_processed_after_success",
            session_id="cs_test_processed_after_success",
        )

        with self.session_factory() as db:
            response = await self.send_event(db, event)
            stored_event = db.scalar(
                select(StripeEvent).where(
                    StripeEvent.stripe_event_id
                    == "evt_test_processed_after_success"
                )
            )
            subscription = db.scalar(
                select(Subscription).where(
                    Subscription.stripe_checkout_session_id
                    == "cs_test_processed_after_success"
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response_json(response)["processed"])
        self.assertIsNotNone(subscription)
        self.assertIsNotNone(stored_event)
        self.assertIsNotNone(stored_event.processed_at)

    async def test_plan_can_be_resolved_from_known_payment_link(self) -> None:
        event = self.checkout_event(
            event_id="evt_test_payment_link_plan",
            session_id="cs_test_payment_link_plan",
            plan_code=None,
            duration_days=None,
            payment_link="https://buy.stripe.com/three",
        )

        with self.session_factory() as db:
            response = await self.send_event(db, event)

            plan = db.scalar(select(Plan).where(Plan.code == "3_months"))
            subscription = db.scalar(
                select(Subscription).where(
                    Subscription.stripe_checkout_session_id
                    == "cs_test_payment_link_plan"
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response_json(response)["processed"])
        self.assertIsNotNone(plan)
        self.assertIsNotNone(subscription)
        self.assertEqual(subscription.plan_id, plan.id)

    async def test_plan_can_be_resolved_from_payment_link_id_and_configured_url(
        self,
    ) -> None:
        payment_link_id = "plink_1MonthlyABC123"
        bot_module.set_settings(
            replace(
                TEST_SETTINGS,
                payment_link_monthly=f"https://buy.stripe.com/{payment_link_id}",
            )
        )
        event = self.checkout_event(
            event_id="evt_test_payment_link_id_url_config",
            session_id="cs_test_payment_link_id_url_config",
            plan_code=None,
            duration_days=None,
            payment_link=payment_link_id,
        )

        with self.session_factory() as db:
            response = await self.send_event(db, event)

            plan = db.scalar(select(Plan).where(Plan.code == "monthly"))
            subscription = db.scalar(
                select(Subscription).where(
                    Subscription.stripe_checkout_session_id
                    == "cs_test_payment_link_id_url_config"
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response_json(response)["processed"])
        self.assertIsNotNone(plan)
        self.assertIsNotNone(subscription)
        self.assertEqual(subscription.plan_id, plan.id)

    async def test_plan_can_be_resolved_from_payment_link_id_config(self) -> None:
        payment_link_id = "plink_1MonthlyXYZ789"
        bot_module.set_settings(
            replace(TEST_SETTINGS, payment_link_monthly=payment_link_id)
        )
        event = self.checkout_event(
            event_id="evt_test_payment_link_id_config",
            session_id="cs_test_payment_link_id_config",
            plan_code=None,
            duration_days=None,
            payment_link=payment_link_id,
        )

        with self.session_factory() as db:
            response = await self.send_event(db, event)

            plan = db.scalar(select(Plan).where(Plan.code == "monthly"))
            subscription = db.scalar(
                select(Subscription).where(
                    Subscription.stripe_checkout_session_id
                    == "cs_test_payment_link_id_config"
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response_json(response)["processed"])
        self.assertIsNotNone(plan)
        self.assertIsNotNone(subscription)
        self.assertEqual(subscription.plan_id, plan.id)

    async def test_plan_can_be_resolved_from_existing_stripe_price(self) -> None:
        event = self.checkout_event(
            event_id="evt_test_price_plan",
            session_id="cs_test_price_plan",
            plan_code=None,
            duration_days=None,
        )
        event["data"]["object"]["line_items"] = {
            "data": [{"price": {"id": "price_6_months"}}]
        }

        with self.session_factory() as db:
            expected_plan = self.create_plan(
                db,
                code="6_months",
                name="6 Months VIP",
                stripe_price_id="price_6_months",
            )
            response = await self.send_event(db, event)

            subscription = db.scalar(
                select(Subscription).where(
                    Subscription.stripe_checkout_session_id == "cs_test_price_plan"
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response_json(response)["processed"])
        self.assertIsNotNone(subscription)
        self.assertEqual(subscription.plan_id, expected_plan.id)

    async def test_subscription_deleted_revokes_group_access(self) -> None:
        fake_bot = AsyncMock()
        fake_bot.get_chat_member.return_value = fake_chat_member("member")
        app_module.app.state.bot = fake_bot

        with self.session_factory() as db:
            plan = self.create_plan(db)
            user = User(telegram_id=123456)
            subscription = Subscription(
                user=user,
                plan=plan,
                stripe_subscription_id="sub_cancel_me",
                stripe_checkout_session_id="cs_cancel_me",
                status="active",
                current_period_start=datetime(2026, 7, 25),
                current_period_end=datetime(2026, 8, 24),
                started_at=datetime(2026, 7, 25),
                ends_at=datetime(2026, 8, 24),
                telegram_access_status="invite_sent",
                telegram_access_granted_at=datetime(2026, 7, 25),
            )
            db.add(subscription)
            db.commit()

            response = await self.send_event(
                db,
                self.subscription_event(
                    event_id="evt_subscription_deleted",
                    stripe_subscription_id="sub_cancel_me",
                    status="active",
                ),
            )
            stored_subscription = db.scalar(
                select(Subscription).where(
                    Subscription.stripe_subscription_id == "sub_cancel_me"
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response_json(response)["processed"])
        fake_bot.ban_chat_member.assert_awaited_once_with(
            chat_id=TEST_SETTINGS.telegram_group_id,
            user_id=123456,
        )
        fake_bot.unban_chat_member.assert_awaited_once()
        self.assertEqual(stored_subscription.status, "canceled")
        self.assertEqual(stored_subscription.telegram_access_status, "revoked")
        self.assertIsNotNone(stored_subscription.telegram_access_revoked_at)

    async def test_out_of_order_subscription_event_does_not_overwrite_newer_state(
        self,
    ) -> None:
        fake_bot = AsyncMock()
        app_module.app.state.bot = fake_bot

        with self.session_factory() as db:
            plan = self.create_plan(db)
            user = User(telegram_id=123456)
            subscription = Subscription(
                user=user,
                plan=plan,
                stripe_subscription_id="sub_ordered",
                stripe_checkout_session_id="cs_ordered",
                status="active",
                current_period_start=datetime(2026, 7, 25),
                current_period_end=datetime(2026, 8, 24),
                started_at=datetime(2026, 7, 25),
                ends_at=datetime(2026, 8, 24),
                telegram_access_granted_at=datetime(2026, 7, 25),
                last_stripe_event_created=datetime(2026, 7, 26, tzinfo=UTC),
            )
            db.add(subscription)
            db.commit()

            response = await self.send_event(
                db,
                self.subscription_event(
                    event_id="evt_subscription_old_update",
                    event_type="customer.subscription.updated",
                    stripe_subscription_id="sub_ordered",
                    status="unpaid",
                    event_created=1784937600,
                ),
            )
            stored_subscription = db.scalar(
                select(Subscription).where(
                    Subscription.stripe_subscription_id == "sub_ordered"
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response_json(response)["processed"])
        self.assertEqual(response_json(response)["reason"], "out_of_order_event")
        self.assertEqual(stored_subscription.status, "active")
        fake_bot.ban_chat_member.assert_not_called()

    async def test_subscription_updated_unpaid_revokes_access(self) -> None:
        fake_bot = AsyncMock()
        fake_bot.get_chat_member.return_value = fake_chat_member("member")
        app_module.app.state.bot = fake_bot

        with self.session_factory() as db:
            plan = self.create_plan(db)
            user = User(telegram_id=123456)
            subscription = Subscription(
                user=user,
                plan=plan,
                stripe_subscription_id="sub_unpaid",
                stripe_checkout_session_id="cs_unpaid",
                status="active",
                current_period_start=datetime(2026, 7, 25),
                current_period_end=datetime(2026, 8, 24),
                started_at=datetime(2026, 7, 25),
                ends_at=datetime(2026, 8, 24),
                telegram_access_granted_at=datetime(2026, 7, 25),
            )
            db.add(subscription)
            db.commit()

            response = await self.send_event(
                db,
                self.subscription_event(
                    event_id="evt_subscription_unpaid",
                    event_type="customer.subscription.updated",
                    stripe_subscription_id="sub_unpaid",
                    status="unpaid",
                    event_created=1785024000,
                ),
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response_json(response)["processed"])
        fake_bot.ban_chat_member.assert_awaited_once()

    async def test_subscription_updated_past_due_and_incomplete_are_grace_noops(
        self,
    ) -> None:
        for index, status in enumerate(("past_due", "incomplete"), start=1):
            fake_bot = AsyncMock()
            app_module.app.state.bot = fake_bot

            with self.session_factory() as db:
                plan = self.create_plan(db, code=f"{status}_plan")
                user = User(telegram_id=123456 + index)
                subscription = Subscription(
                    user=user,
                    plan=plan,
                    stripe_subscription_id=f"sub_{status}",
                    stripe_checkout_session_id=f"cs_{status}",
                    status="active",
                    current_period_start=datetime(2026, 7, 25),
                    current_period_end=datetime(2026, 8, 24),
                    started_at=datetime(2026, 7, 25),
                    ends_at=datetime(2026, 8, 24),
                    telegram_access_granted_at=datetime(2026, 7, 25),
                )
                db.add(subscription)
                db.commit()

                response = await self.send_event(
                    db,
                    self.subscription_event(
                        event_id=f"evt_subscription_{status}",
                        event_type="customer.subscription.updated",
                        stripe_subscription_id=f"sub_{status}",
                        status=status,
                        event_created=1785024000,
                    ),
                )
                stored_subscription = db.scalar(
                    select(Subscription).where(
                        Subscription.stripe_subscription_id == f"sub_{status}"
                    )
                )

            self.assertEqual(response.status_code, 200)
            self.assertTrue(response_json(response)["processed"])
            self.assertEqual(stored_subscription.status, status)
            fake_bot.ban_chat_member.assert_not_called()


if __name__ == "__main__":
    unittest.main()
