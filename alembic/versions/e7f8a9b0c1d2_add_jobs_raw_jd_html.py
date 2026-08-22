"""add jobs.raw_jd_html for sanitized JD display

Revision ID: e7f8a9b0c1d2
Revises: c9d0e1f2a3b4
Create Date: 2026-08-22 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e7f8a9b0c1d2"
down_revision: str | Sequence[str] | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("raw_jd_html", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "raw_jd_html")
