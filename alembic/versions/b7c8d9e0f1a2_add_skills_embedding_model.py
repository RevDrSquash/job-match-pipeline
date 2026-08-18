"""add skills.embedding_model so runtime knows which model produced stored vectors

Revision ID: b7c8d9e0f1a2
Revises: f1a2b3c4d5e6
Create Date: 2026-08-18 11:41:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7c8d9e0f1a2"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("skills", sa.Column("embedding_model", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("skills", "embedding_model")
