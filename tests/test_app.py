import asyncio
import unittest
from unittest.mock import patch

import stripe

import app as app_module
import bot as bot_module
from bot import Settings


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


class StripeWebhookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.previous_settings = bot_module.settings
        bot_module.set_settings(TEST_SETTINGS)

    def tearDown(self) -> None:
        bot_module.settings = self.previous_settings

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

    async def test_webhook_with_verified_event_returns_200(self) -> None:
        event = {"id": "evt_test_123", "type": "checkout.session.completed"}

        with patch.object(
            app_module.stripe.Webhook,
            "construct_event",
            return_value=event,
        ) as construct_event:
            response = await app_module.stripe_webhook(
                FakeRequest(body=b'{"id":"evt_test_123"}'),
                stripe_signature="valid-signature",
            )

        self.assertEqual(response.status_code, 200)
        construct_event.assert_called_once_with(
            b'{"id":"evt_test_123"}',
            "valid-signature",
            TEST_SETTINGS.stripe_webhook_secret,
        )


if __name__ == "__main__":
    unittest.main()
