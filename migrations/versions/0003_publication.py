"""Immutable teaching drafts, confirmations and source-bound snapshot lineage."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("decision_revisions", sa.Column("parent_revision_id", sa.String(64)))
    op.create_foreign_key(
        "revision_parent",
        "decision_revisions",
        "decision_revisions",
        ["workspace_id", "repository_id", "parent_revision_id"],
        ["workspace_id", "repository_id", "id"],
    )
    op.add_column("snapshots", sa.Column("parent_snapshot_id", sa.String(64)))
    op.create_foreign_key(
        "snapshot_parent",
        "snapshots",
        "snapshots",
        ["workspace_id", "repository_id", "parent_snapshot_id"],
        ["workspace_id", "repository_id", "id"],
    )
    op.create_table(
        "teaching_drafts",
        sa.Column("workspace_id", sa.String(64), primary_key=True),
        sa.Column("repository_id", sa.String(64), primary_key=True),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("author_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("operation_id", sa.String(128), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("repository_id", "author_id", "operation_id", name="draft_operation"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "repository_id"], ["repositories.workspace_id", "repositories.id"]
        ),
    )
    op.create_table(
        "publication_receipts",
        sa.Column("workspace_id", sa.String(64), primary_key=True),
        sa.Column("repository_id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(64), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("operation_id", sa.String(128), primary_key=True),
        sa.Column("draft_id", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("repository_id", "draft_id", name="draft_published_once"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "repository_id", "draft_id"],
            ["teaching_drafts.workspace_id", "teaching_drafts.repository_id", "teaching_drafts.id"],
        ),
    )
    for table in ("teaching_drafts", "publication_receipts"):
        op.execute(
            f"CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION tacit_reject_mutation()"
        )


def downgrade() -> None:
    op.drop_table("publication_receipts")
    op.drop_table("teaching_drafts")
    op.drop_constraint("snapshot_parent", "snapshots", type_="foreignkey")
    op.drop_column("snapshots", "parent_snapshot_id")
    op.drop_constraint("revision_parent", "decision_revisions", type_="foreignkey")
    op.drop_column("decision_revisions", "parent_revision_id")
