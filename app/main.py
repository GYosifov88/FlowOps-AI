from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings, validate_openai_settings
from app.database import init_db
from app.logging_config import configure_logging
from app.middleware import RequestContextMiddleware
from app.routers import events, incidents, pages, workflows

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    current = get_settings()
    # Raises on an invalid LOG_LEVEL / LOG_FORMAT, so a typo stops startup.
    configure_logging(current.log_level, current.log_format)
    # Fail fast: never run with an unauthenticated ingest endpoint.
    if not current.ingest_token:
        raise RuntimeError(
            "FLOWOPS_INGEST_TOKEN is not set. Add it to .env (see .env.example)."
        )
    # A typo in an OPENAI_* value stops startup, like LOG_LEVEL. A missing key is fine.
    validate_openai_settings(current)
    # Create any missing tables. Idempotent: never drops, alters or seeds anything, so an
    # existing database is untouched (a changed schema still needs scripts.reset_db).
    init_db()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(RequestContextMiddleware)
app.include_router(workflows.router)
app.include_router(events.router)
app.include_router(incidents.router)
app.include_router(pages.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
