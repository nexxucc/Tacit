"""Scoped custom roles and GitHub team mappings; built-in admin invariants stay unchanged."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "custom_roles",
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("description", sa.String(512), nullable=False),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.CheckConstraint("version > 0", name="custom_role_version"),
    )

    def scope(required=False):
        return [
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("workspace_id", sa.String(64), nullable=False),
            sa.Column("custom_role_id", sa.String(64), nullable=not required),
            sa.Column("repository_id", sa.String(64)),
        ]

    def custom_fk():
        return sa.ForeignKeyConstraint(
            ["workspace_id", "custom_role_id"], ["custom_roles.workspace_id", "custom_roles.id"]
        )

    def repo_fk():
        return sa.ForeignKeyConstraint(
            ["workspace_id", "repository_id"], ["repositories.workspace_id", "repositories.id"]
        )

    op.create_table(
        "custom_role_assignments",
        *scope(True),
        sa.Column("user_id", sa.String(64), nullable=False),
        custom_fk(),
        repo_fk(),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"], ["memberships.workspace_id", "memberships.user_id"]
        ),
    )
    op.create_table(
        "team_role_mappings",
        *scope(),
        sa.Column("github_team_id", sa.String(32), nullable=False),
        sa.Column("team_slug", sa.String(100), nullable=False),
        sa.Column("role", sa.String(32)),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.CheckConstraint(
            "(role IS NOT NULL AND custom_role_id IS NULL) OR (role IS NULL AND custom_role_id IS NOT NULL)",
            name="team_one_role",
        ),
        sa.CheckConstraint(
            "role IS NULL OR role IN ('learner', 'maintainer')", name="team_content_roles_only"
        ),
        custom_fk(),
        repo_fk(),
    )


def downgrade() -> None:
    for table in ("team_role_mappings", "custom_role_assignments", "custom_roles"):
        op.drop_table(table)
