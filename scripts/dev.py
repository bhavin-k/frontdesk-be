"""Run a local API or worker with the disposable test environment."""

import argparse
import os
import sys

from _environment import load_local_environment

from recruitment.cli import api, worker


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=("api", "worker"))
    args = parser.parse_args()
    load_local_environment()
    os.environ["RECRUITMENT_ENVIRONMENT"] = "development"
    print(
        "Local development only. Provider mode:",
        os.environ.get("RECRUITMENT_PROVIDER_MODE", "real"),
    )
    sys.argv = [sys.argv[0]]
    api() if args.service == "api" else worker()


if __name__ == "__main__":
    main()
