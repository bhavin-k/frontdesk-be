import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_local_environment() -> None:
    state = ROOT / ".local" / "test-database.json"
    if state.exists() and os.environ.get("RECRUITMENT_ENVIRONMENT") != "production":
        for key, value in json.loads(state.read_text("utf-8"))["environment"].items():
            os.environ.setdefault(key, value)
