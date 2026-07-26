"""add telegram access fields

Revision ID: 202607250001
Revises: 202607120001
Create Date: 2026-07-25 02:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "202607250001"
down_revision: Union[str, Sequence[str], None] = "202607120001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("last_stripe_event_created", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("telegram_access_status", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("telegram_access_error", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("telegram_access_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("telegram_access_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("telegram_invite_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("telegram_access_granted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("telegram_access_revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_subscriptions_ends_at",
        "subscriptions",
        ["ends_at"],
        unique=False,
    )
    op.create_index(
        "ix_subscriptions_access_grant_scan",
        "subscriptions",
        ["status", "telegram_access_retry_at"],
        unique=False,
    )
    op.create_index(
        "ix_subscriptions_access_revoke_scan",
        "subscriptions",
        ["telegram_access_revoked_at", "status", "ends_at"],
        unique=False,
    )
    op.create_index(
        "ix_subscriptions_telegram_access_revoked_at",
        "subscriptions",
        ["telegram_access_revoked_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_subscriptions_telegram_access_revoked_at",
        table_name="subscriptions",
    )
    op.drop_index("ix_subscriptions_access_revoke_scan", table_name="subscriptions")
    op.drop_index("ix_subscriptions_access_grant_scan", table_name="subscriptions")
    op.drop_index("ix_subscriptions_ends_at", table_name="subscriptions")
    op.drop_column("subscriptions", "telegram_access_revoked_at")
    op.drop_column("subscriptions", "telegram_access_granted_at")
    op.drop_column("subscriptions", "telegram_invite_sent_at")
    op.drop_column("subscriptions", "telegram_access_retry_at")
    op.drop_column("subscriptions", "telegram_access_checked_at")
    op.drop_column("subscriptions", "telegram_access_error")
    op.drop_column("subscriptions", "telegram_access_status")
    op.drop_column("subscriptions", "last_stripe_event_created")
