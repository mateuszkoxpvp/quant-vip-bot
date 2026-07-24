from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping
from urllib.parse import urlparse, urlunparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from bot import Settings
from models import Plan, StripeEvent, Subscription, User

logger = logging.getLogger(__name__)

TELEGRAM_ID_MAX = 9_223_372_036_854_775_807

PLAN_DURATIONS = {
    "monthly": 30,
    "3_months": 90,
    "6_months": 180,
    "lifetime": None,
}

DEFAULT_PLAN_NAMES = {
    "monthly": "Monthly VIP",
    "3_months": "3 Months VIP",
    "6_months": "6 Months VIP",
    "lifetime": "Lifetime VIP",
}

PLAN_CODE_ALIASES = {
    "month": "monthly",
    "3_month": "3_months",
    "3_months": "3_months",
    "3-months": "3_months",
    "3months": "3_months",
    "three": "3_months",
    "three_months": "3_months",
    "6_month": "6_months",
    "6_months": "6_months",
    "6-months": "6_months",
    "6months": "6_months",
    "six": "6_months",
    "six_months": "6_months",
}

PLAN_PAYMENT_LINK_SETTINGS = {
    "monthly": "payment_link_monthly",
    "3_months": "payment_link_3_months",
    "6_months": "payment_link_6_months",
    "lifetime": "payment_link_lifetime",
}


@dataclass(frozen=True)
class FulfillmentResult:
    processed: bool
    telegram_id: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class PlanDuration:
    days: int | None
    reason: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.reason is None


def as_plain_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}

    if hasattr(value, "to_dict_recursive"):
        return value.to_dict_recursive()

    if isinstance(value, Mapping):
        return dict(value)

    return {}


def object_id(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, str):
        return value

    if isinstance(value, Mapping):
        nested_id = value.get("id")
        return str(nested_id) if nested_id else None

    return str(value)


def normalize_plan_code(value: Any) -> str:
    code = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", str(value).strip().lower()).strip("_")
    return code[:64] or "checkout"


def canonical_plan_code(value: Any) -> str:
    code = normalize_plan_code(value)
    return PLAN_CODE_ALIASES.get(code, code)


def checkout_session_from_event(event: Mapping[str, Any]) -> dict[str, Any]:
    data = as_plain_mapping(event.get("data"))
    return as_plain_mapping(data.get("object"))


def parse_telegram_id(session: Mapping[str, Any]) -> int | None:
    raw_reference = session.get("client_reference_id")
    if raw_reference is None:
        return None

    raw_reference = str(raw_reference).strip()
    if not raw_reference.isdigit():
        return None

    telegram_id = int(raw_reference)
    if telegram_id <= 0 or telegram_id > TELEGRAM_ID_MAX:
        return None

    return telegram_id


def payment_link_candidates(value: Any) -> set[str]:
    if value is None:
        return set()

    raw_values = [value]
    if isinstance(value, Mapping):
        raw_values = [value.get("id"), value.get("url")]

    candidates: set[str] = set()
    for raw_value in raw_values:
        if raw_value is None:
            continue

        value_text = str(raw_value).strip()
        if not value_text:
            continue

        candidates.add(value_text.lower())
        parsed_url = urlparse(value_text)
        if parsed_url.scheme and parsed_url.netloc:
            normalized_url = urlunparse(
                parsed_url._replace(query="", fragment="")
            ).rstrip("/")
            candidates.add(normalized_url.lower())

            path_leaf = parsed_url.path.strip("/").split("/")[-1]
            if path_leaf:
                candidates.add(path_leaf.lower())

    return candidates


def plan_code_from_payment_link(
    session: Mapping[str, Any],
    settings: Settings,
) -> str | None:
    metadata = as_plain_mapping(session.get("metadata"))
    session_candidates: set[str] = set()
    for payment_link_value in (
        session.get("payment_link"),
        metadata.get("payment_link"),
        metadata.get("payment_link_id"),
        metadata.get("payment_link_url"),
    ):
        session_candidates.update(payment_link_candidates(payment_link_value))

    if not session_candidates:
        return None

    for plan_code, setting_name in PLAN_PAYMENT_LINK_SETTINGS.items():
        configured_candidates = payment_link_candidates(getattr(settings, setting_name))
        if session_candidates & configured_candidates:
            return plan_code

    return None


def extract_price_ids_from_items(items: Any) -> set[str]:
    if items is None:
        return set()

    item_data = items.get("data", items) if isinstance(items, Mapping) else items
    if not isinstance(item_data, list):
        return set()

    price_ids: set[str] = set()
    for item in item_data:
        if not isinstance(item, Mapping):
            continue

        price_id = object_id(item.get("price"))
        if price_id:
            price_ids.add(price_id)

    return price_ids


def extract_price_ids(session: Mapping[str, Any]) -> set[str]:
    metadata = as_plain_mapping(session.get("metadata"))
    price_ids = {
        str(price_id).strip()
        for price_id in (metadata.get("stripe_price_id"), metadata.get("price_id"))
        if price_id not in (None, "")
    }

    price_ids.update(extract_price_ids_from_items(session.get("line_items")))

    subscription = as_plain_mapping(session.get("subscription"))
    price_ids.update(extract_price_ids_from_items(subscription.get("items")))

    return price_ids


def plan_code_from_session(
    session: Mapping[str, Any],
    settings: Settings,
) -> str | None:
    metadata = as_plain_mapping(session.get("metadata"))

    metadata_code = metadata.get("plan_code")
    if metadata_code:
        return canonical_plan_code(metadata_code)

    return plan_code_from_payment_link(session, settings)


def apply_plan_details(
    plan: Plan,
    session: Mapping[str, Any],
    metadata: Mapping[str, Any],
    price_ids: set[str],
) -> None:
    plan_name = metadata.get("plan_name")
    if plan_name:
        plan.name = str(plan_name)

    stripe_price_id = object_id(metadata.get("stripe_price_id") or metadata.get("price_id"))
    if stripe_price_id is None and len(price_ids) == 1:
        stripe_price_id = next(iter(price_ids))
    if stripe_price_id is not None:
        plan.stripe_price_id = stripe_price_id

    if metadata.get("interval"):
        plan.interval = str(metadata.get("interval"))

    if session.get("amount_total") is not None:
        plan.amount_cents = int(session["amount_total"])

    if session.get("currency"):
        plan.currency = str(session["currency"]).upper()


def resolve_plan(
    db: Session,
    session: Mapping[str, Any],
    settings: Settings,
) -> Plan | None:
    metadata = as_plain_mapping(session.get("metadata"))
    price_ids = extract_price_ids(session)
    plan_code = plan_code_from_session(session, settings)

    if plan_code is not None:
        plan = db.scalar(select(Plan).where(Plan.code == plan_code))
        if plan is not None:
            apply_plan_details(plan, session, metadata, price_ids)
            return plan

        if plan_code not in DEFAULT_PLAN_NAMES:
            return None

        plan = Plan(
            code=plan_code,
            name=str(metadata.get("plan_name") or DEFAULT_PLAN_NAMES[plan_code]),
            is_active=True,
        )
        apply_plan_details(plan, session, metadata, price_ids)
        db.add(plan)
        db.flush()
        return plan

    if price_ids:
        plan = db.scalar(
            select(Plan)
            .where(
                Plan.stripe_price_id.in_(price_ids),
                Plan.is_active.is_(True),
            )
            .limit(1)
        )
        if plan is not None:
            apply_plan_details(plan, session, metadata, price_ids)
            return plan

    return None


def parse_duration_days(raw_duration: Any) -> int | None:
    if isinstance(raw_duration, bool):
        return None

    try:
        return int(str(raw_duration).strip())
    except (TypeError, ValueError):
        return None


def resolve_plan_duration(
    plan_code: str,
    metadata: Mapping[str, Any],
) -> PlanDuration:
    canonical_code = canonical_plan_code(plan_code)
    raw_duration = metadata.get("duration_days")

    if canonical_code == "lifetime":
        if raw_duration in (None, "", "0", 0):
            return PlanDuration(days=None)
        return PlanDuration(days=None, reason="invalid_duration_days")

    if raw_duration not in (None, ""):
        duration_days = parse_duration_days(raw_duration)
        if duration_days is None or duration_days <= 0:
            return PlanDuration(days=None, reason="invalid_duration_days")
        return PlanDuration(days=duration_days)

    if canonical_code in PLAN_DURATIONS:
        return PlanDuration(days=PLAN_DURATIONS[canonical_code])

    return PlanDuration(days=None, reason="unknown_plan")


def stripe_datetime(value: Any) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        return value

    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)

    value_text = str(value).strip()
    if not value_text:
        return None

    if value_text.isdigit():
        return datetime.fromtimestamp(int(value_text), tz=UTC)

    try:
        return datetime.fromisoformat(value_text.replace("Z", "+00:00"))
    except ValueError:
        return None


def checkout_status(session: Mapping[str, Any]) -> str:
    subscription = as_plain_mapping(session.get("subscription"))
    subscription_status = subscription.get("status")
    if subscription_status:
        return str(subscription_status)

    payment_status = session.get("payment_status")
    if payment_status == "paid":
        return "active"

    return str(session.get("status") or payment_status or "pending")


def upsert_user_from_checkout_session(
    db: Session,
    session: Mapping[str, Any],
    telegram_id: int,
) -> User:
    user = db.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(telegram_id=telegram_id)
        db.add(user)

    stripe_customer_id = object_id(session.get("customer"))
    if stripe_customer_id:
        user.stripe_customer_id = stripe_customer_id

    db.flush()
    return user


def upsert_subscription_from_checkout_session(
    db: Session,
    session: Mapping[str, Any],
    user: User,
    plan: Plan,
    now: datetime,
    duration: PlanDuration,
) -> Subscription:
    checkout_session_id = object_id(session.get("id"))
    stripe_subscription_id = object_id(session.get("subscription"))

    subscription = None
    if checkout_session_id:
        subscription = db.scalar(
            select(Subscription).where(
                Subscription.stripe_checkout_session_id == checkout_session_id
            )
        )

    if subscription is None and stripe_subscription_id:
        subscription = db.scalar(
            select(Subscription).where(
                Subscription.stripe_subscription_id == stripe_subscription_id
            )
        )

    if subscription is None:
        subscription = Subscription(
            user=user,
            plan=plan,
            stripe_checkout_session_id=checkout_session_id,
            stripe_subscription_id=stripe_subscription_id,
        )
        db.add(subscription)
    else:
        subscription.user = user
        subscription.plan = plan

    subscription_payload = as_plain_mapping(session.get("subscription"))
    current_period_start = (
        stripe_datetime(subscription_payload.get("current_period_start"))
        or stripe_datetime(session.get("created"))
        or now
    )
    if duration.days is None:
        current_period_end = None
        ends_at = None
    else:
        current_period_end = current_period_start + timedelta(days=duration.days)
        ends_at = current_period_end

    subscription.status = checkout_status(session)
    subscription.stripe_checkout_session_id = checkout_session_id
    subscription.stripe_subscription_id = stripe_subscription_id
    subscription.current_period_start = current_period_start
    subscription.current_period_end = current_period_end
    subscription.started_at = (
        stripe_datetime(subscription_payload.get("start_date"))
        or current_period_start
    )
    subscription.ends_at = ends_at
    subscription.canceled_at = stripe_datetime(subscription_payload.get("canceled_at"))

    db.flush()
    return subscription


def fulfill_checkout_session_completed(
    db: Session,
    event: Mapping[str, Any],
    stripe_event: StripeEvent,
    settings: Settings,
) -> FulfillmentResult:
    session = checkout_session_from_event(event)
    if not session:
        logger.warning("checkout.session.completed missing data.object.")
        return FulfillmentResult(processed=False, reason="missing_checkout_session")

    telegram_id = parse_telegram_id(session)
    if telegram_id is None:
        logger.warning(
            "checkout.session.completed id=%s missing valid client_reference_id.",
            session.get("id"),
        )
        return FulfillmentResult(processed=False, reason="missing_telegram_id")

    metadata = as_plain_mapping(session.get("metadata"))
    plan_code = plan_code_from_session(session, settings)
    duration = (
        resolve_plan_duration(plan_code, metadata)
        if plan_code is not None
        else None
    )
    if duration is not None and not duration.is_valid:
        if duration.reason == "unknown_plan":
            logger.warning(
                "checkout.session.completed id=%s has an unknown plan.",
                session.get("id"),
            )
            return FulfillmentResult(processed=False, reason=duration.reason)

        logger.warning(
            "checkout.session.completed id=%s has invalid duration_days.",
            session.get("id"),
        )
        return FulfillmentResult(processed=False, reason=duration.reason)

    plan = resolve_plan(db, session, settings)
    if plan is None:
        logger.warning(
            "checkout.session.completed id=%s has an unknown plan.",
            session.get("id"),
        )
        return FulfillmentResult(processed=False, reason="unknown_plan")

    if duration is None:
        duration = resolve_plan_duration(plan.code, metadata)
    if not duration.is_valid:
        logger.warning(
            "checkout.session.completed id=%s has invalid duration_days.",
            session.get("id"),
        )
        return FulfillmentResult(processed=False, reason=duration.reason)

    now = datetime.now(UTC)
    user = upsert_user_from_checkout_session(db, session, telegram_id)
    subscription = upsert_subscription_from_checkout_session(
        db=db,
        session=session,
        user=user,
        plan=plan,
        now=now,
        duration=duration,
    )

    stripe_event.processed_at = now
    db.flush()

    logger.info(
        "Fulfilled checkout session id=%s for telegram_id=%s subscription_id=%s.",
        session.get("id"),
        telegram_id,
        subscription.id,
    )
    return FulfillmentResult(processed=True, telegram_id=telegram_id)
