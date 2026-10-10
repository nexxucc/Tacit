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
