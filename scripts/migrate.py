"""Apply Frontdesk's reviewed Alembic migrations."""

import argparse
import os
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, pool, text

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "0015_cv_json_matching"


def upgrade_database(revision: str = "head") -> None:
    config = Config(str(ROOT / "alembic.ini"))
    if revision not in {"head", BASELINE}:
        command.upgrade(config, revision)
        return
    engine = create_engine(
        os.environ["RECRUITMENT_MIGRATION_DATABASE_URL"],
        poolclass=pool.NullPool,
        hide_parameters=True,
    )
    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(13068, 15)"))
            versions = inspect(connection).has_table("alembic_version", schema="public")
            if not versions:
                occupied = connection.scalar(
                    text("""
                    SELECT EXISTS (
                        SELECT 1 FROM pg_class WHERE relnamespace='public'::regnamespace
                    ) OR EXISTS (
                        SELECT 1 FROM pg_proc WHERE pronamespace='public'::regnamespace
                    )
                """)
                )
                if occupied:
                    raise RuntimeError("The database contains an unversioned public schema.")
                fresh = Config(str(ROOT / "alembic.ini"))
                fresh.set_main_option("version_locations", str(ROOT / "alembic" / "bootstrap"))
                fresh.attributes["connection"] = connection
                command.upgrade(fresh, BASELINE)
            config.attributes["connection"] = connection
            command.upgrade(config, revision)
    finally:
        engine.dispose()


def main() -> None:
    from _environment import load_local_environment

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("upgrade", "downgrade", "check"))
    parser.add_argument("revision", nargs="?", default="head")
    parser.add_argument("--allow-data-loss", action="store_true")
    args = parser.parse_args()
    load_local_environment()
    config = Config(str(ROOT / "alembic.ini"))
    if args.action == "upgrade":
        upgrade_database(args.revision)
    elif args.action == "check":
        command.check(config)
    else:
        if not args.allow_data_loss:
            parser.error("Downgrade can destroy data; --allow-data-loss is required.")
        command.downgrade(config, args.revision)
    print(f"Alembic {args.action} completed.")


if __name__ == "__main__":
    main()
