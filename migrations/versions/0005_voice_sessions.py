"""Recoverable encrypted audio uploads and explicit transcription lifecycle."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "voice_sessions",
        sa.Column("workspace_id", sa.String(64), primary_key=True),
        sa.Column("repository_id", sa.String(64), primary_key=True),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("author_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("operation_id", sa.String(128), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("chunks", sa.Integer(), nullable=False),
        sa.Column("pcm_bytes", sa.Integer(), nullable=False),
        sa.Column("capture_complete", sa.Boolean(), nullable=False),
        sa.Column("audio_sha256", sa.String(64)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "repository_id", "author_id", "operation_id", name="voice_session_operation"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "repository_id"], ["repositories.workspace_id", "repositories.id"]
        ),
    )
    op.create_table(
        "voice_chunks",
        sa.Column("workspace_id", sa.String(64), primary_key=True),
        sa.Column("repository_id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(64), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("encrypted_pcm", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "repository_id", "session_id"],
            ["voice_sessions.workspace_id", "voice_sessions.repository_id", "voice_sessions.id"],
        ),
    )


def downgrade() -> None:
    op.drop_table("voice_chunks")
    op.drop_table("voice_sessions")
