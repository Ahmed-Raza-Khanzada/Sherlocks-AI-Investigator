"""link graph runs and provider cache

Revision ID: 7b1d2c9e4f10
Revises: 54460060a12f
Create Date: 2026-09-11 13:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7b1d2c9e4f10"
down_revision: str | None = "54460060a12f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "sherlocks"


def upgrade() -> None:
    op.create_table(
        "graph_run",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("seed_label", sa.String(length=255), nullable=True),
        sa.Column("backend", sa.String(length=16), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("progress_pct", sa.Integer(), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=True),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("graph", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("events", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )
    op.create_index("ix_sherlocks_graph_run_created_at", "graph_run", ["created_at"], unique=False, schema=SCHEMA)
    op.create_table(
        "provider_cache",
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("system", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
        schema=SCHEMA,
    )
    op.create_index("ix_sherlocks_provider_cache_expires_at", "provider_cache", ["expires_at"], unique=False, schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_sherlocks_provider_cache_expires_at", table_name="provider_cache", schema=SCHEMA)
    op.drop_table("provider_cache", schema=SCHEMA)
    op.drop_index("ix_sherlocks_graph_run_created_at", table_name="graph_run", schema=SCHEMA)
    op.drop_table("graph_run", schema=SCHEMA)
