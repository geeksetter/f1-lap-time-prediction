import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


def load_config():
    load_dotenv(ROOT / ".env", override=False)


def database_url():
    load_config()
    value = os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL is missing; configure .env (see .env.example)")
    return value
