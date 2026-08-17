"""add jobs.url and jobs.employment_type for ingest

Revision ID: a1b2c3d4e5f6
Revises: 2231fc28883f
Create Date: 2026-08-17 04:50:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "2231fc28883f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("url", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("employment_type", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "employment_type")
    op.drop_column("jobs", "url")
