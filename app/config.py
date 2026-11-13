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
    # Incident analysis (V2-M3). The app starts without a key; only POST .../analyze needs it.
    openai_api_key: str = field(default="", repr=False)
    openai_model: str = "gpt-6-luna"
    # "" = the model's own default (medium for gpt-6-luna). See OPENAI_REASONING_EFFORTS.
    openai_reasoning_effort: str = ""
    # Reasoning tokens count against this budget, so keep it generous (16000 is ~1 cent at most).
    openai_max_output_tokens: int = 16000
    openai_timeout_seconds: float = 120.0


OPENAI_REASONING_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh", "max"})


def validate_openai_settings(settings: Settings) -> None:
    """Raise ValueError for a bad OPENAI_* value (called at startup, like LOG_LEVEL)."""
    if settings.openai_reasoning_effort and settings.openai_reasoning_effort not in OPENAI_REASONING_EFFORTS:
        raise ValueError(
            f"OPENAI_REASONING_EFFORT must be empty or one of {sorted(OPENAI_REASONING_EFFORTS)}"
        )
    if not settings.openai_model:
        raise ValueError("OPENAI_MODEL must not be empty")
    if settings.openai_max_output_tokens < 1:
        raise ValueError("OPENAI_MAX_OUTPUT_TOKENS must be a positive integer")
    if settings.openai_timeout_seconds <= 0:
        raise ValueError("OPENAI_TIMEOUT_SECONDS must be positive")


def _number(name: str, default, cast):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


def get_settings() -> Settings:
    return Settings(
        app_name=os.getenv("APP_NAME", "FlowOps AI"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        database_url=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL),
        ingest_token=os.getenv("FLOWOPS_INGEST_TOKEN", "").strip(),
        log_format=os.getenv("LOG_FORMAT", "text"),
        n8n_base_url=os.getenv("N8N_BASE_URL", "").strip(),
        n8n_api_key=os.getenv("N8N_API_KEY", "").strip(),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-6-luna").strip(),
        openai_reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "").strip().lower(),
        openai_max_output_tokens=_number("OPENAI_MAX_OUTPUT_TOKENS", 16000, int),
        openai_timeout_seconds=_number("OPENAI_TIMEOUT_SECONDS", 120.0, float),
    )
