"""Application configuration, loaded from environment variables.

python-dotenv reads a local .env file (never committed) into the environment;
real environment variables take precedence over the file.
"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    app_name: str
    log_level: str


def get_settings() -> Settings:
    return Settings(
        app_name=os.getenv("APP_NAME", "FlowOps AI"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
