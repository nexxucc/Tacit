"""Initial core metadata plus immutable-record protection.

This scaffold migration freezes a copy of the initial metadata, avoiding future
model changes silently altering an already released revision.
"""

from alembic import op

from migrations.schema_v1 import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())
    op.execute("""
        CREATE FUNCTION tacit_reject_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'Tacit immutable record'; END;
        $$ LANGUAGE plpgsql
    """)
    for table in ("decision_revisions", "snapshots", "snapshot_members", "audit_events"):
        op.execute(
            f"CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION tacit_reject_mutation()"
        )


def downgrade() -> None:
    op.execute("DROP FUNCTION tacit_reject_mutation() CASCADE")
    Base.metadata.drop_all(bind=op.get_bind())
