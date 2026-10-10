import os

from alembic import context
from sqlalchemy import create_engine, pool
from tacit_api.db import Base

url = os.environ.get("TACIT_DATABASE_URL")
if not url:
    raise RuntimeError("TACIT_DATABASE_URL is required; migrations never guess a database")

if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
