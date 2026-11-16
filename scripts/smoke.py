"""Check the actual API and worker entry points against the disposable database."""

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from _environment import ROOT, load_local_environment


def main() -> None:
    load_local_environment()
    if os.environ.get("RECRUITMENT_ENVIRONMENT") != "test":
        raise SystemExit("Smoke checks require the disposable test environment.")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "recruitment.api.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        cwd=ROOT,
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ready = False
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError("The API process exited before readiness.")
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health/ready", timeout=1
                ) as reply:
                    ready = reply.status == 200
                if ready:
                    break
            except urllib.error.URLError:
                time.sleep(0.25)
        if not ready:
            raise RuntimeError("API readiness timed out.")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/openapi.json", timeout=2) as reply:
            if reply.status != 200:
                raise RuntimeError("OpenAPI endpoint failed.")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    subprocess.run(
        [sys.executable, "-c", "from recruitment.cli import worker; worker()", "--once"],
        cwd=ROOT,
        env=os.environ.copy(),
        timeout=30,
        check=True,
    )
    print("API readiness, generated OpenAPI and worker --once passed.")


if __name__ == "__main__":
    main()
