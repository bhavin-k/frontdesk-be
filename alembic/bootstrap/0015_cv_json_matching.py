"""Create revision 0015 on an empty database without the retired extension."""

import os
from pathlib import Path

import psycopg
from psycopg import sql

from alembic import op

revision = "0015_cv_json_matching"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    raw = connection.connection.driver_connection
    assert isinstance(raw, psycopg.Connection)
    for name in ("auth", "api", "worker", "dispatcher"):
        password = os.environ.get(f"RECRUITMENT_DB_{name.upper()}_PASSWORD")
        if not password or len(password) < 24:
            raise RuntimeError(f"Provide a generated RECRUITMENT_DB_{name.upper()}_PASSWORD.")
        raw.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}"
            ).format(sql.Identifier(f"recruitment_{name}"), sql.Literal(password))
        )
    schema = Path(__file__).with_name("schema_0015.sql").read_text(encoding="utf-8")
    connection.exec_driver_sql("SET LOCAL check_function_bodies = false")
    connection.exec_driver_sql(schema)


def downgrade() -> None:
    raise RuntimeError("Revision 0015 initializes an empty database.")
