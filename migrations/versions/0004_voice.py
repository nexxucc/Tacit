"""Durable at-most-once voice transcription reservations."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "voice_attempts",
        sa.Column("workspace_id", sa.String(64), primary_key=True),
        sa.Column("repository_id", sa.String(64), primary_key=True),
        sa.Column("author_id", sa.String(64), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("operation_id", sa.String(128), primary_key=True),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "repository_id"], ["repositories.workspace_id", "repositories.id"]
        ),
    )
    op.execute(
        "CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON voice_attempts FOR EACH ROW EXECUTE FUNCTION tacit_reject_mutation()"
    )
    op.create_table(
        "voice_transcripts",
        sa.Column("workspace_id", sa.String(64), primary_key=True),
        sa.Column("repository_id", sa.String(64), primary_key=True),
        sa.Column("author_id", sa.String(64), primary_key=True),
        sa.Column("operation_id", sa.String(128), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "repository_id", "author_id", "operation_id"],
            [
                "voice_attempts.workspace_id",
                "voice_attempts.repository_id",
                "voice_attempts.author_id",
                "voice_attempts.operation_id",
            ],
        ),
    )
    op.execute(
        "CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON voice_transcripts FOR EACH ROW EXECUTE FUNCTION tacit_reject_mutation()"
    )


def downgrade() -> None:
    op.drop_table("voice_transcripts")
    op.drop_table("voice_attempts")
