"""Repository publication quorum and exact-draft human approvals."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "publication_policies",
        sa.Column("workspace_id", sa.String(64), primary_key=True),
        sa.Column("repository_id", sa.String(64), primary_key=True),
        sa.Column("required_maintainers", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "repository_id"], ["repositories.workspace_id", "repositories.id"]
        ),
        sa.CheckConstraint("required_maintainers BETWEEN 1 AND 5", name="quorum_size"),
        sa.CheckConstraint("version > 0", name="quorum_policy_version"),
    )
    op.create_table(
        "draft_approvals",
        sa.Column("workspace_id", sa.String(64), primary_key=True),
        sa.Column("repository_id", sa.String(64), primary_key=True),
        sa.Column("draft_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column(
            "credential_hash",
            sa.String(64),
            sa.ForeignKey("credentials.token_hash"),
            nullable=False,
        ),
        sa.Column("review_digest", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "repository_id", "draft_id"],
            ["teaching_drafts.workspace_id", "teaching_drafts.repository_id", "teaching_drafts.id"],
        ),
        sa.CheckConstraint("policy_version >= 0", name="approval_policy_version"),
    )


def downgrade():
    op.drop_table("draft_approvals")
    op.drop_table("publication_policies")
