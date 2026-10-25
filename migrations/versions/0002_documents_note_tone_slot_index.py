"""documents, booking note, bot tone, partial slot index

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-24
"""
import sqlalchemy as sa
from alembic import op

import app.models

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("source", sa.String(length=500), nullable=False),
        sa.Column("created_at", app.models.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_documents_business_id"), "documents", ["business_id"], unique=False)

    # server defaults: the tables already hold rows when this runs in production
    op.add_column("bookings", sa.Column("note", sa.Text(), nullable=False, server_default=""))
    op.add_column("businesses", sa.Column("tone", sa.String(length=10), nullable=False, server_default="friendly"))

    op.add_column("kb_items", sa.Column("document_id", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_kb_items_document_id"), "kb_items", ["document_id"], unique=False)
    op.create_foreign_key(
        "fk_kb_items_document_id_documents", "kb_items", "documents", ["document_id"], ["id"], ondelete="CASCADE"
    )

    # only live bookings own a slot: a cancelled one must not block re-booking the same time
    op.drop_constraint("uq_booking_resource_start", "bookings", type_="unique")
    op.create_index(
        "uq_booking_active_slot",
        "bookings",
        ["resource_id", "start_utc"],
        unique=True,
        postgresql_where=sa.text("status != 'cancelled'"),
        sqlite_where=sa.text("status != 'cancelled'"),
    )


def downgrade() -> None:
    # NOTE: restoring the strict unique constraint fails if a slot was cancelled and re-booked
    # in the meantime; clean those rows first if you really need to go back.
    op.drop_index(
        "uq_booking_active_slot",
        table_name="bookings",
        postgresql_where=sa.text("status != 'cancelled'"),
        sqlite_where=sa.text("status != 'cancelled'"),
    )
    op.create_unique_constraint("uq_booking_resource_start", "bookings", ["resource_id", "start_utc"])
    op.drop_constraint("fk_kb_items_document_id_documents", "kb_items", type_="foreignkey")
    op.drop_index(op.f("ix_kb_items_document_id"), table_name="kb_items")
    op.drop_column("kb_items", "document_id")
    op.drop_column("businesses", "tone")
    op.drop_column("bookings", "note")
    op.drop_index(op.f("ix_documents_business_id"), table_name="documents")
    op.drop_table("documents")
