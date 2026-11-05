"""Application configuration, loaded from environment variables.

python-dotenv reads a local .env file (never committed) into the environment;
real environment variables take precedence over the file.
"""
import os
from dataclasses import dataclass, field
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
    ingest_token: str = field(default="", repr=False)
    # "text" (readable) or "json" (one object per line). Validated at startup.
    log_format: str = "text"
    # n8n pull sync only (scripts.sync_n8n); the web app does not need them.
    n8n_base_url: str = ""
    n8n_api_key: str = field(default="", repr=False)


def get_settings() -> Settings:
    return Settings(
        app_name=os.getenv("APP_NAME", "FlowOps AI"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        database_url=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL),
        ingest_token=os.getenv("FLOWOPS_INGEST_TOKEN", "").strip(),
        log_format=os.getenv("LOG_FORMAT", "text"),
        n8n_base_url=os.getenv("N8N_BASE_URL", "").strip(),
        n8n_api_key=os.getenv("N8N_API_KEY", "").strip(),
    )
