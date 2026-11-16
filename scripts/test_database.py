"""Create or stop only this project's disposable PostgreSQL test container."""

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import psycopg

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "pgvector/pgvector@sha256:0dd3149a06e4579a0d70d8fef9945636cd514005e7237deb27f9b67886f8dc4f"
PROJECT = hashlib.sha256(str(ROOT).encode()).hexdigest()[:10]
NAME = f"recruitment-phase1-test-{PROJECT}"
STATE = ROOT / ".local" / "test-database.json"


def docker(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True).strip()


def start() -> None:
    if STATE.exists():
        state = json.loads(STATE.read_text("utf-8"))
        if state["container"] != NAME:
            raise RuntimeError("Test container ownership mismatch.")
        if docker("inspect", "--format", "{{.State.Running}}", NAME) == "true":
            print("The project test database is already running.")
            return
        raise RuntimeError("The saved test container stopped. Run stop before starting another.")
    STATE.parent.mkdir(exist_ok=True)
    admin_password = secrets.token_urlsafe(36)
    role_passwords = {
        name: secrets.token_urlsafe(36) for name in ("api", "auth", "worker", "dispatcher")
    }
    env_file = STATE.parent / "postgres.env"
    env_file.write_text(
        f"POSTGRES_USER=recruitment_migrator\nPOSTGRES_PASSWORD={admin_password}\n"
        "POSTGRES_DB=recruitment_phase1_test\n",
        encoding="utf-8",
    )
    docker(
        "run",
        "--detach",
        "--name",
        NAME,
        "--pull",
        "never",
        "--label",
        f"recruitment.project={PROJECT}",
        "--label",
        "recruitment.purpose=phase1-tests",
        "--env-file",
        str(env_file),
        "--publish",
        "127.0.0.1::5432",
        "--tmpfs",
        "/var/lib/postgresql/data",
        IMAGE,
    )
    port = docker("port", NAME, "5432/tcp").splitlines()[0].rsplit(":", 1)[1]
    suffix = f"@127.0.0.1:{port}/recruitment_phase1_test"
    admin_url = f"postgresql://recruitment_migrator:{quote(admin_password)}{suffix}"
    for attempt in range(60):
        try:
            with psycopg.connect(admin_url, connect_timeout=2):
                break
        except psycopg.OperationalError:
            if attempt == 59:
                raise RuntimeError("The dedicated test database did not become ready.") from None
            time.sleep(0.5)
    env = {
        "RECRUITMENT_TEST_ADMIN_URL": admin_url,
        "RECRUITMENT_MIGRATION_DATABASE_URL": admin_url.replace(
            "postgresql:", "postgresql+psycopg:"
        ),
        "RECRUITMENT_ENVIRONMENT": "test",
        "RECRUITMENT_PROVIDER_MODE": "fake",
    }
    for role, password in role_passwords.items():
        env[f"RECRUITMENT_DB_{role.upper()}_PASSWORD"] = password
        field = "DATABASE_URL" if role == "api" else f"{role.upper()}_DATABASE_URL"
        env[f"RECRUITMENT_{field}"] = (
            f"postgresql+psycopg://recruitment_{role}:{quote(password)}{suffix}"
        )
    STATE.write_text(
        json.dumps({"container": NAME, "environment": env}, indent=2), encoding="utf-8"
    )
    if os.name != "nt":
        STATE.chmod(0o600)
        env_file.chmod(0o600)
    print(
        f"Dedicated PostgreSQL test database ready on localhost:{port}. "
        "Credentials stay in .local/."
    )


def stop() -> None:
    if not STATE.exists():
        print("No project test database state exists.")
        return
    state = json.loads(STATE.read_text("utf-8"))
    if state["container"] != NAME:
        raise RuntimeError("Refusing to stop an unrelated container.")
    label = docker("inspect", "--format", '{{index .Config.Labels "recruitment.project"}}', NAME)
    if label != PROJECT:
        raise RuntimeError("Refusing to remove a container without this project's ownership label.")
    docker("rm", "--force", NAME)
    STATE.unlink()
    (STATE.parent / "postgres.env").unlink(missing_ok=True)
    print("Only the disposable project test container was removed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop"))
    args = parser.parse_args()
    start() if args.action == "start" else stop()
