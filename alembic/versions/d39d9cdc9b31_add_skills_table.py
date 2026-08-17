"""add_skills_table

Revision ID: d39d9cdc9b31
Revises: 2231fc28883f
Create Date: 2026-08-17 04:47:29.027712

"""

from collections.abc import Sequence

import pgvector
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d39d9cdc9b31"
down_revision: str | Sequence[str] | None = "2231fc28883f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "skills",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("canonical_label", sa.Text(), nullable=False),
        sa.Column(
            "alt_labels",
            postgresql.ARRAY(sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(dim=768), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_skills_canonical_label", "skills", ["canonical_label"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_skills_canonical_label", table_name="skills")
    op.drop_table("skills")
