"""Apply reviewed Alembic revisions, using local test settings when present."""

import argparse
import os

from _environment import ROOT, load_local_environment
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("upgrade", "downgrade", "reset-test", "check"))
    parser.add_argument("revision", nargs="?", default="head")
    parser.add_argument("--allow-data-loss", action="store_true")
    args = parser.parse_args()
    load_local_environment()
    config = Config(str(ROOT / "alembic.ini"))
    if args.action == "upgrade":
        command.upgrade(config, args.revision)
    elif args.action == "check":
        command.check(config)
    elif args.action == "downgrade":
        if not args.allow_data_loss:
            parser.error("Downgrade can destroy data; --allow-data-loss is required.")
        command.downgrade(config, args.revision)
    else:
        url = os.environ.get("RECRUITMENT_MIGRATION_DATABASE_URL", "")
        if make_url(url).database != "recruitment_phase1_test":
            parser.error("reset-test is restricted to recruitment_phase1_test.")
        command.downgrade(config, "base")
        command.upgrade(config, "head")
    print(f"Alembic {args.action} completed.")


if __name__ == "__main__":
    main()
