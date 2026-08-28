"""replace the flat skills table with the canonical skill knowledge graph

Revision ID: f3a4b5c6d7e8
Revises: e7f8a9b0c1d2
Create Date: 2026-08-26 21:25:00.000000

"""

from collections.abc import Sequence

import pgvector
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f3a4b5c6d7e8"
down_revision: str | Sequence[str] | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "concept",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("concept_type", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column(
            "embedding",
            pgvector.sqlalchemy.vector.VECTOR(dim=768),
            nullable=True,
        ),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_concept_normalized_name", "concept", ["normalized_name"])
    op.create_index(
        "ix_concept_embedding_hnsw",
        "concept",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_where=sa.text("embedding IS NOT NULL"),
    )

    op.create_table(
        "source_concept",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_version", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column(
            "raw_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source",
            "source_version",
            "external_id",
            name="uq_source_concept_source_version_external_id",
        ),
    )

    op.create_table(
        "concept_alias",
        sa.Column("concept_id", sa.UUID(), nullable=False),
        sa.Column("normalized_alias", sa.Text(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=16), server_default="en", nullable=False),
        sa.Column("alias_type", sa.String(length=32), nullable=False),
        sa.Column(
            "provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["concept_id"], ["concept.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("concept_id", "normalized_alias"),
    )
    op.create_index(
        "ix_concept_alias_normalized_alias",
        "concept_alias",
        ["normalized_alias"],
    )
    op.create_index(
        "ix_concept_alias_normalized_alias_trgm",
        "concept_alias",
        ["normalized_alias"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"normalized_alias": "gin_trgm_ops"},
    )

    op.create_table(
        "concept_edge",
        sa.Column("subject_id", sa.UUID(), nullable=False),
        sa.Column("predicate", sa.String(length=64), nullable=False),
        sa.Column("object_id", sa.UUID(), nullable=False),
        sa.Column("confidence", sa.Float(), server_default="1.0", nullable=False),
        sa.Column(
            "provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["object_id"], ["concept.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subject_id"], ["concept.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("subject_id", "predicate", "object_id"),
    )
    op.create_index(
        "ix_concept_edge_object_predicate",
        "concept_edge",
        ["object_id", "predicate"],
    )

    op.create_table(
        "source_mapping",
        sa.Column("source_concept_id", sa.UUID(), nullable=False),
        sa.Column("concept_id", sa.UUID(), nullable=False),
        sa.Column("mapping_type", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("mapping_method", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["concept_id"], ["concept.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_concept_id"],
            ["source_concept.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("source_concept_id", "concept_id"),
    )
    op.create_index(
        "ix_source_mapping_concept_id",
        "source_mapping",
        ["concept_id"],
    )

    op.create_table(
        "source_edge",
        sa.Column("subject_id", sa.UUID(), nullable=False),
        sa.Column("predicate", sa.String(length=64), nullable=False),
        sa.Column("object_id", sa.UUID(), nullable=False),
        sa.Column("confidence", sa.Float(), server_default="1.0", nullable=False),
        sa.Column(
            "raw_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["object_id"],
            ["source_concept.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["source_concept.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("subject_id", "predicate", "object_id"),
    )
    op.create_index(
        "ix_source_edge_object_predicate",
        "source_edge",
        ["object_id", "predicate"],
    )

    op.drop_index("ix_skills_canonical_label", table_name="skills")
    op.drop_table("skills")


def downgrade() -> None:
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
        sa.Column(
            "embedding",
            pgvector.sqlalchemy.vector.VECTOR(dim=768),
            nullable=True,
        ),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_skills_canonical_label", "skills", ["canonical_label"])

    op.drop_index("ix_source_edge_object_predicate", table_name="source_edge")
    op.drop_table("source_edge")
    op.drop_index("ix_source_mapping_concept_id", table_name="source_mapping")
    op.drop_table("source_mapping")
    op.drop_index("ix_concept_edge_object_predicate", table_name="concept_edge")
    op.drop_table("concept_edge")
    op.drop_index(
        "ix_concept_alias_normalized_alias_trgm",
        table_name="concept_alias",
        postgresql_using="gin",
    )
    op.drop_index(
        "ix_concept_alias_normalized_alias",
        table_name="concept_alias",
    )
    op.drop_table("concept_alias")
    op.drop_table("source_concept")
    op.drop_index(
        "ix_concept_embedding_hnsw",
        table_name="concept",
        postgresql_using="hnsw",
    )
    op.drop_index("ix_concept_normalized_name", table_name="concept")
    op.drop_table("concept")

    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
