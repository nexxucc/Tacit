"""Grounded teaching, idempotent model requests, artifacts and outbox projections."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    def scoped():
        return [
            sa.Column("workspace_id", sa.String(64), primary_key=True),
            sa.Column("repository_id", sa.String(64), primary_key=True),
        ]

    def scope_fk():
        return sa.ForeignKeyConstraint(
            ["workspace_id", "repository_id"], ["repositories.workspace_id", "repositories.id"]
        )

    op.create_table(
        "teaching_runs",
        *scoped(),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("author_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("operation_id", sa.String(128), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("(CURRENT_TIMESTAMP + INTERVAL '1 day')"),
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "repository_id", "author_id", "operation_id", name="teaching_run_operation"
        ),
        scope_fk(),
    )
    op.create_table(
        "model_operations",
        *scoped(),
        sa.Column("author_id", sa.String(64), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("operation_id", sa.String(128), primary_key=True),
        sa.Column("kind", sa.String(32), primary_key=True),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        scope_fk(),
    )
    op.create_table(
        "artifacts",
        *scoped(),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("author_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        scope_fk(),
    )
    op.create_table(
        "publication_index",
        *scoped(),
        sa.Column("revision_id", sa.String(64), primary_key=True),
        sa.Column("statement", sa.String(8000), nullable=False),
        sa.Column("source_commit", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "repository_id", "revision_id"],
            [
                "decision_revisions.workspace_id",
                "decision_revisions.repository_id",
                "decision_revisions.id",
            ],
        ),
    )
    op.add_column(
        "credentials",
        sa.Column(
            "next_reconcile_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.add_column(
        "outbox_events", sa.Column("attempts", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "outbox_events",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.add_column("outbox_events", sa.Column("error_code", sa.String(64)))
    op.create_index("outbox_pending", "outbox_events", ["processed_at", "next_attempt_at"])
    op.create_index("voice_retention", "voice_sessions", ["expires_at", "status"])
    op.create_index("artifacts_retention", "artifacts", ["expires_at", "deleted_at"])


def downgrade() -> None:
    op.drop_column("credentials", "next_reconcile_at")
    op.drop_index("artifacts_retention", table_name="artifacts")
    op.drop_index("voice_retention", table_name="voice_sessions")
    op.drop_index("outbox_pending", table_name="outbox_events")
    for col in ("error_code", "next_attempt_at", "attempts"):
        op.drop_column("outbox_events", col)
    for table in ("publication_index", "artifacts", "model_operations", "teaching_runs"):
        op.drop_table(table)
