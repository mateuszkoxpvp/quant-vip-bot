from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from bot import Settings
from models import StripeEvent
from subscription_service import (
    fulfill_checkout_session_completed,
    fulfill_stripe_subscription_event,
)

logger = logging.getLogger(__name__)


class InvalidStripeEventError(ValueError):
    pass


@dataclass(frozen=True)
class StripeProcessResult:
    processed: bool
    telegram_id: int | None = None
    subscription_id: int | None = None
    subscription_status: str | None = None
    access_action: str | None = None
    reason: str | None = None


def stripe_value_to_plain(value: Any) -> Any:
    to_dict_recursive = getattr(value, "to_dict_recursive", None)
    if callable(to_dict_recursive):
        return to_dict_recursive()

    private_to_dict_recursive = getattr(value, "_to_dict_recursive", None)
    if callable(private_to_dict_recursive):
        return private_to_dict_recursive()

    if isinstance(value, Mapping):
        return {key: stripe_value_to_plain(item) for key, item in value.items()}

    if isinstance(value, list):
        return [stripe_value_to_plain(item) for item in value]

    return value


def normalize_stripe_event(event: Mapping[str, Any] | Any) -> dict[str, Any]:
    event_data = stripe_value_to_plain(event)
    if not isinstance(event_data, Mapping):
        raise InvalidStripeEventError("Verified Stripe event must be a mapping.")

    return dict(event_data)


def serialize_stripe_event(event: Mapping[str, Any] | Any) -> dict[str, Any]:
    return normalize_stripe_event(event)


def build_stripe_event(event: Mapping[str, Any] | Any) -> StripeEvent:
    event = normalize_stripe_event(event)
    event_id = event.get("id")
    event_type = event.get("type")

    if not event_id or not event_type:
        raise InvalidStripeEventError("Verified Stripe event is missing id or type.")

    return StripeEvent(
        stripe_event_id=str(event_id),
        event_type=str(event_type),
        api_version=event.get("api_version"),
        livemode=bool(event.get("livemode", False)),
        payload=serialize_stripe_event(event),
    )


def duplicate_event_result(db: Session, event_id: str) -> StripeProcessResult:
    existing_event = db.scalar(
        select(StripeEvent).where(StripeEvent.stripe_event_id == event_id)
    )

    if existing_event is None:
        raise RuntimeError("Duplicate Stripe event was not found after IntegrityError.")

    reason = "already_processed" if existing_event.processed_at else "already_received"
    return StripeProcessResult(processed=False, reason=reason)


def process_verified_event(
    db: Session,
    event: Mapping[str, Any] | Any,
    settings: Settings,
) -> StripeProcessResult:
    event = normalize_stripe_event(event)
    stripe_event = build_stripe_event(event)
    event_id = stripe_event.stripe_event_id
    event_type = stripe_event.event_type

    try:
        with db.begin():
            db.add(stripe_event)
            db.flush()

            if event_type == "checkout.session.completed":
                fulfillment_result = fulfill_checkout_session_completed(
                    db=db,
                    event=event,
                    stripe_event=stripe_event,
                    settings=settings,
                )
                return StripeProcessResult(
                    processed=fulfillment_result.processed,
                    telegram_id=fulfillment_result.telegram_id,
                    subscription_id=fulfillment_result.subscription_id,
                    subscription_status=fulfillment_result.subscription_status,
                    access_action=fulfillment_result.access_action,
                    reason=fulfillment_result.reason,
                )

            if event_type in {
                "customer.subscription.deleted",
                "customer.subscription.updated",
            }:
                fulfillment_result = fulfill_stripe_subscription_event(
                    db=db,
                    event=event,
                    stripe_event=stripe_event,
                )
                return StripeProcessResult(
                    processed=fulfillment_result.processed,
                    telegram_id=fulfillment_result.telegram_id,
                    subscription_id=fulfillment_result.subscription_id,
                    subscription_status=fulfillment_result.subscription_status,
                    access_action=fulfillment_result.access_action,
                    reason=fulfillment_result.reason,
                )

            if event_type != "checkout.session.completed":
                return StripeProcessResult(processed=False, reason="ignored_event_type")
    except IntegrityError as error:
        db.rollback()
        logger.info("Stripe event id=%s was already stored.", event_id)
        try:
            return duplicate_event_result(db, event_id)
        except RuntimeError:
            raise error
