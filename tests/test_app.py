import asyncio
import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

import stripe
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

import app as app_module
import bot as bot_module
import subscription_service
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


class HealthEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        polling_task = getattr(app_module.app.state, "polling_task", None)
        if polling_task is not None and not polling_task.done():
            polling_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await polling_task
        app_module.app.state.polling_task = None

    async def test_health_returns_200_when_polling_task_is_running(self) -> None:
        polling_task = asyncio.create_task(wait_forever())
        app_module.app.state.polling_task = polling_task

        response = await app_module.health(FakeRequest())

        self.assertEqual(response.status_code, 200)

    async def test_health_returns_503_when_polling_task_finished(self) -> None:
        async def finished() -> None:
            return None

        polling_task = asyncio.create_task(finished())
        await polling_task
        app_module.app.state.polling_task = polling_task

        response = await app_module.health(FakeRequest())

        self.assertEqual(response.status_code, 503)


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


class StripeWebhookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.previous_settings = bot_module.settings
        bot_module.set_settings(TEST_SETTINGS)
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
        payment_link: str | None = None,
        subscription: object | None = None,
    ) -> dict:
        metadata = {}
        if plan_code is not None:
            metadata["plan_code"] = plan_code
        if duration_days is not None:
            metadata["duration_days"] = duration_days

        session = {
            "id": session_id,
            "customer": "cus_test_123",
            "payment_status": "paid",
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
            "api_version": "2026-07-12",
            "livemode": False,
            "data": {"object": session},
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


if __name__ == "__main__":
    unittest.main()
