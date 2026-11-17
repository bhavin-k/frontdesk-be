"""Run static checks, migrations and the complete offline-provider test suite."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    environment = os.environ.copy()
    state = ROOT / ".local" / "test-database.json"
    if state.exists():
        for key, value in json.loads(state.read_text("utf-8"))["environment"].items():
            environment.setdefault(key, value)
    if not environment.get("RECRUITMENT_TEST_ADMIN_URL"):
        raise SystemExit("Start the dedicated test database before running the complete checks.")
    commands = [
        ["ruff", "check", "src", "tests", "scripts", "alembic"],
        ["ruff", "format", "--check", "src", "tests", "scripts", "alembic"],
        ["mypy", "src/recruitment"],
        ["pytest", "--cov=recruitment", "--cov-report=term-missing"],
        ["compileall", "-q", "src"],
    ]
    for command in commands:
        print(f"Checking: {' '.join(command)}", flush=True)
        subprocess.run([sys.executable, "-m", *command], cwd=ROOT, env=environment, check=True)


if __name__ == "__main__":
    main()
