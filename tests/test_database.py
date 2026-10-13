import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tacit_api.db import Base, Membership, Repository, RoleAssignment, User, Workspace


def test_database_rejects_cross_workspace_repository_grant() -> None:
    # SQLite verifies portable relational FKs only; PostgreSQL trigger runtime is a separate gate.
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def foreign_keys(connection: object, record: object) -> None:
        connection.execute("PRAGMA foreign_keys=ON")  # type: ignore[attr-defined]

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                User(id="user", github_id="1"),
                Workspace(id="a", github_owner_id="2"),
                Workspace(id="b", github_owner_id="3"),
            ]
        )
        session.commit()
        session.add_all(
            [
                Membership(workspace_id="a", user_id="user"),
                Repository(workspace_id="b", id="repo", github_id="4", full_name="b/repo"),
            ]
        )
        session.commit()
        session.add(
            RoleAssignment(
                id="grant",
                workspace_id="a",
                user_id="user",
                repository_id="repo",
                role="maintainer",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize("revision", ["base"])
def test_postgres_migration_roundtrip_and_runtime_columns(revision):
    import os
    import subprocess
    from uuid import uuid4

    from sqlalchemy import inspect, text
    from sqlalchemy.engine import make_url

    url = os.environ.get("TACIT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires disposable PostgreSQL database")
    schema = "test_" + uuid4().hex
    admin = create_engine(url)
    scoped = make_url(url).update_query_dict({"options": f"-csearch_path={schema}"})
    env = {**os.environ, "TACIT_DATABASE_URL": scoped.render_as_string(hide_password=False)}
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        for target in ("head", revision, "head"):
            result = subprocess.run(
                [
                    "uv",
                    "run",
                    "--no-sync",
                    "alembic",
                    "downgrade" if target == revision else "upgrade",
                    target,
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr
        engine = create_engine(scoped)
        try:
            inspector = inspect(engine)
            assert set(inspector.get_table_names()) == set(Base.metadata.tables) | {
                "alembic_version"
            }
            for name, table in Base.metadata.tables.items():
                assert {c["name"] for c in inspector.get_columns(name)} == set(table.columns.keys())
        finally:
            engine.dispose()
    finally:
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
