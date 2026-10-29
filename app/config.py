"""Application configuration, loaded from environment variables.

python-dotenv reads a local .env file (never committed) into the environment;
real environment variables take precedence over the file.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
# Absolute, so the same database is used no matter which directory a command runs from.
DEFAULT_DATABASE_URL = f"sqlite:///{(BASE_DIR / 'flowops.db').as_posix()}"


@dataclass(frozen=True)
class Settings:
    app_name: str
    log_level: str
    database_url: str
    # Shared secret n8n sends in X-FlowOps-Token. The app refuses to start without it.
    ingest_token: str = ""
    # "text" (readable) or "json" (one object per line). Validated at startup.
    log_format: str = "text"


def get_settings() -> Settings:
    return Settings(
        app_name=os.getenv("APP_NAME", "FlowOps AI"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        database_url=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL),
        ingest_token=os.getenv("FLOWOPS_INGEST_TOKEN", "").strip(),
        log_format=os.getenv("LOG_FORMAT", "text"),
    )
