"""rename matches gate columns to qualification labels

Revision ID: c9d0e1f2a3b4
Revises: b7c8d9e0f1a2
Create Date: 2026-08-19 12:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: str | Sequence[str] | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "matches",
        "gate_verdict",
        new_column_name="qualification_label",
        existing_type=sa.Text(),
        existing_nullable=True,
    )
    op.alter_column(
        "matches",
        "gate_reason",
        new_column_name="screen_reason",
        existing_type=sa.Text(),
        existing_nullable=True,
    )
    op.execute(
        sa.text(
            """
            UPDATE matches
            SET qualification_label = CASE qualification_label
                WHEN 'pass' THEN 'potentially_qualified'
                WHEN 'reject' THEN 'unqualified'
                ELSE qualification_label
            END
            WHERE qualification_label IN ('pass', 'reject')
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE matches
            SET qualification_label = CASE qualification_label
                WHEN 'potentially_qualified' THEN 'pass'
                WHEN 'clearly_qualified' THEN 'pass'
                WHEN 'overqualified' THEN 'pass'
                WHEN 'minimally_qualified' THEN 'reject'
                WHEN 'unqualified' THEN 'reject'
                ELSE qualification_label
            END
            WHERE qualification_label IS NOT NULL
            """
        )
    )
    op.alter_column(
        "matches",
        "qualification_label",
        new_column_name="gate_verdict",
        existing_type=sa.Text(),
        existing_nullable=True,
    )
    op.alter_column(
        "matches",
        "screen_reason",
        new_column_name="gate_reason",
        existing_type=sa.Text(),
        existing_nullable=True,
    )
