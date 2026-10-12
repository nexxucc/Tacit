"""Persist installations, memberships, device pairing and revocable credentials."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("repository_global_id", "repositories", ["id"])
    op.add_column(
        "memberships", sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true())
    )
    op.add_column(
        "memberships", sa.Column("invited", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.create_table(
        "installations",
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("github_id", sa.String(32), nullable=False, unique=True),
        sa.Column("owner_login", sa.String(255), nullable=False),
        sa.Column("owner_type", sa.String(32), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "credentials",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("agent", sa.Boolean(), nullable=False),
        sa.Column("parent_hash", sa.String(64), sa.ForeignKey("credentials.token_hash")),
        sa.Column("repository_ids", sa.JSON(), nullable=False),
        sa.Column("provider_token", sa.String(4096)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "device_pairings",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("device_code", sa.String(4096), nullable=False),
        sa.Column("interval", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "invitations",
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("github_id", sa.String(32), primary_key=True),
        sa.Column("repository_ids", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("body_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_constraint("repository_global_id", "repositories", type_="unique")
    for table in (
        "webhook_deliveries",
        "invitations",
        "device_pairings",
        "credentials",
        "installations",
    ):
        op.drop_table(table)
    op.drop_column("memberships", "invited")
    op.drop_column("memberships", "active")
