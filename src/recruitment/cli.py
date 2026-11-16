import argparse
import logging
import time

import uvicorn

from recruitment.adapters.db.session import Database
from recruitment.adapters.providers import Providers
from recruitment.adapters.queue import PostgresJobQueue
from recruitment.core.config import Settings
from recruitment.core.logging import configure_provider_logging
from recruitment.jobs.runner import Worker


def api() -> None:
    uvicorn.run(
        "recruitment.api.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=8000,
        access_log=False,
    )


def worker() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    configure_provider_logging()
    settings = Settings()  # type: ignore[call-arg]
    db = Database(settings)
    providers = Providers.from_settings(settings)
    runner = Worker(PostgresJobQueue(db, settings), providers)
    try:
        while True:
            busy = runner.run_once()
            if args.once:
                break
            if not busy:
                time.sleep(settings.worker_poll_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        providers.close()
        db.dispose()
