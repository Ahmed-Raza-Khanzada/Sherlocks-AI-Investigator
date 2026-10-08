"""case board stored on its own, with a version guard

Revision ID: 9c4e1a7b2d30
Revises: 7b1d2c9e4f10
Create Date: 2026-10-06 16:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9c4e1a7b2d30"
down_revision: str | None = "7b1d2c9e4f10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "sherlocks"


def upgrade() -> None:
    op.create_table(
        "case_board",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("board", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("case_board", schema=SCHEMA)
