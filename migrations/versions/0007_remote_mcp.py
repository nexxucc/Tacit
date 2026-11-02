"""Durable, audience-bound MCP OAuth grants."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_grants",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column("client_id", sa.String(128), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("cookie_hash", sa.String(64)),
        sa.Column("code_hash", sa.String(64), unique=True),
        sa.Column("token_hash", sa.String(64), unique=True),
        sa.Column("delegated_token", sa.String(4096)),
        sa.Column("credential_hash", sa.String(64), sa.ForeignKey("credentials.token_hash")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mcp_grants_expires_at", "mcp_grants", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_mcp_grants_expires_at", table_name="mcp_grants")
    op.drop_table("mcp_grants")
