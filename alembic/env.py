import os

from sqlalchemy import Connection, create_engine, pool

from alembic import context
from recruitment.adapters.db import (
    better_auth,  # noqa: F401 - register auth table metadata
    matching,  # noqa: F401 - register evaluation metadata
    openings,  # noqa: F401 - register opening table metadata
    phase2,  # noqa: F401 - register intake table metadata
)
from recruitment.adapters.db.models import Base

url = os.environ.get("RECRUITMENT_MIGRATION_DATABASE_URL")
if not url:
    raise RuntimeError("RECRUITMENT_MIGRATION_DATABASE_URL is required.")
if context.is_offline_mode():
    raise RuntimeError("Use an online migration to validate roles and PostgreSQL extensions.")


def migrate(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    migrate(connection)
else:
    engine = create_engine(url, poolclass=pool.NullPool, hide_parameters=True)
    try:
        with engine.connect() as connection:
            migrate(connection)
    finally:
        engine.dispose()
